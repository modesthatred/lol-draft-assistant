"""Синхронизация данных: тянем с OP.GG только по твоим мейнам.

Это осознанное решение: полная матрица матчапов — это ~170 запросов и
~150 МБ HTML, ради 5-6 чемпионов. Мы тянем по одной странице контрпиков и
одной странице синергий на мейна — итого 6-12 запросов вместо 340.

Синхронизация запускается:
  - один раз при первом запуске (или когда база пустая/устарела),
  - вручную по --sync,
  - по кнопке в интерфейсе.
Во время драфта сеть не используется.

Отдельно тянется расширенный набор — контрпики всех чемпионов активной
роли (блок «вне пула»). Это сотни запросов, поэтому у него свой распорядок,
внешняя стадия (sync_role_candidates), которую можно отключить в конфиге.
"""
from __future__ import annotations

import time

from . import champions as ch
from . import opgg
from .cache import Cache
from .settings import ALL_ROLES, Config


class SyncReport:
    def __init__(self) -> None:
        self.ok: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.skipped: list[str] = []
        self.warnings: list[str] = []
        self.patch: str = ""

    @property
    def total(self) -> int:
        return len(self.ok) + len(self.failed) + len(self.skipped)

    def summary(self) -> str:
        parts = [f"обновлено: {len(self.ok)}"]
        if self.failed:
            parts.append(f"ошибок: {len(self.failed)}")
        if self.skipped:
            parts.append(f"пропущено: {len(self.skipped)}")
        if self.patch:
            parts.append(f"патч: {self.patch}")
        return ", ".join(parts)


def sync(config: Config, cache: Cache, *, force: bool = False,
         progress=None, want_items: bool = True,
         draft_active=None) -> SyncReport:
    """Синхронизация данных с OP.GG.

    draft_active — вызываемый () -> bool: идёт ли сейчас драфт. Расширенный
    проход (сотни запросов) в это время недопустим, его прерывают/пропускают
    и догружают при следующем запуске.
    """
    report = SyncReport()
    champs, ver = ch.load_champions()
    if champs:
        cache.save_champions(champs)
    report.patch = ver
    index = ch.build_index(champs)

    role = config.role
    extra_roles = [r for r in (config.sync.get("synergy_roles") or [role])
                   if r != role]
    max_age = float(config.sync.get("max_age_hours", 24))

    pool_total = len(config.pool)
    for idx, entry in enumerate(config.pool, start=1):
        name = entry if isinstance(entry, str) else entry.get("name", "")
        cid = ch.resolve(name, index)
        if cid is None or cid not in champs:
            report.failed.append((str(name), "чемпион не найден в справочнике"))
            continue
        champ = champs[cid]
        if progress:
            progress(f"[{idx}/{pool_total}] {champ.name}: контрпики…")

        # --- контрпики ---
        # Роли пробуем по очереди: у чемпионов типа Lee Sin страницы для
        # чужой роли просто не существует (OP.GG отдаёт пустую страницу),
        # поэтому берём ту роль, где данные реально есть. Твоя роль идёт
        # первой — обычно она и есть правильная.
        role_order = [role, *[r for r in ALL_ROLES if r != role]]
        # фактическая роль для контрпиков: из кэша, если не качаем заново
        counter_role_used = cache.counter_role(cid) or role
        if force or cache.is_stale(cid, max_age):
            picked_role = None
            for r in role_order:
                try:
                    raw, summary, slug = opgg.fetch_counters(champ.name, r)
                except Exception:               # noqa: BLE001
                    continue
                mapped: dict[int, dict] = {}
                for opp_name, stats in raw.items():
                    opp_cid = ch.resolve(opp_name, index)
                    if opp_cid:
                        mapped[opp_cid] = stats
                cache.save_matchups(cid, mapped)
                if summary:
                    # метрики пишем под фактически найденной ролью r,
                    # иначе champ_meta для фолбэк-роли лежит в пустоте
                    cache.save_champ_meta(cid, r, summary)
                cache.set_slug(cid, slug)
                cache.set_counter_role(cid, r)
                picked_role = r
                break
            if picked_role is None:
                report.failed.append(
                    (champ.name, "нет контрпиков ни для одной роли"))
                continue
            counter_role_used = picked_role
            if picked_role != role:
                report.ok.append(f"{champ.name} (роль {picked_role})")
            else:
                report.ok.append(champ.name)
        else:
            report.skipped.append(champ.name)
            report.ok.append(champ.name)

        # --- синергии (по роли) ---
        if progress:
            progress(f"[{idx}/{pool_total}] {champ.name}: синергии…")
        for r in role_order:
            if not force and cache.has_role(r) and not cache.is_stale(
                    cid, max_age):
                continue
            try:
                sraw, sslug = opgg.fetch_synergies(champ.name, r)
            except Exception as e:                # noqa: BLE001
                continue
            smapped: dict[int, dict] = {}
            for partner_name, stats in sraw.items():
                pid = ch.resolve(partner_name, index)
                if pid:
                    smapped[pid] = stats
            cache.save_synergies(cid, r, smapped)
            break

        cache.mark_updated(cid, counter_role_used)

        # --- предметы ---
        # Страница отдаёт патч в URL иконок — заодно проверяем, не сменился
        # ли он: после патча старые винрейты и билды нельзя оставлять.
        if want_items:
            if force or cache.items_stale(cid, counter_role_used, max_age, ver):
                if progress:
                    progress(f"[{idx}/{pool_total}] {champ.name}: "
                             f"предметы…")
                try:
                    idata, _slug = opgg.fetch_items(champ.name,
                                                    counter_role_used)
                except opgg.SourceError as e:
                    report.warnings.append(f"{champ.name}: {e}")
                else:
                    cache.save_items(cid, counter_role_used,
                                     idata["path"], idata["boots"],
                                     idata["patch"])
                    report.patch = idata["patch"] or report.patch

        time.sleep(0.4)      # вежливо к сайту, последовательно, не пачкой

    # --- вне пула: все чемпионы активной роли ---
    # Отдельная, редкая (раз в неделю по умолчанию) стадия: 100+ запросов
    # каждый запуск там не нужны. Принудительный --sync перезаливает их лишь
    # вместе со сменой патча — почти всегда это просто пропуск.
    ttl_all = float(config.sync.get("all_champions_ttl_hours", 168))
    patch_changed = bool(cache.get_state("patch_seen")
                         and cache.get_state("patch")
                         and cache.get_state("patch") !=
                         cache.get_state("patch_seen"))
    want_all = config.sync.get("all_champions", True)
    if want_all and (cache.all_meta_stale(role, ttl_all) or
                     (force and patch_changed)):
        drafting = draft_active() if draft_active is not None else False
        if drafting:
            # Сотни запросов посреди драфта недопустимы: оверлей и так
            # занят пересчётом, а набор чемпионов роли догонится позже.
            report.warnings.append(
                "расширенный набор пропущен: идёт драфт")
            if progress:
                progress("чемпионы роли пропущены: идёт драфт")
        else:
            if progress:
                progress(f"чемпионы роли {role}: вне пула…")
            names, no_role, aborted = sync_role_candidates(
                config, cache, champs, index, role=role,
                progress=progress, abort_if=draft_active)
            if names:
                report.ok.append(f"вне пула: {len(names)} чемпионов {role}")
                report.patch = (cache.get_state("patch_seen") or
                                report.patch)
            if no_role:
                report.warnings.append(
                    f"без данных роли {role}: {len(no_role)} чемпионов")
            if aborted:
                # НЕ mark_all_meta: сборочная часть сохранена, маркер не
                # ставим, чтобы следующий запуск догрузил остальное.
                report.warnings.append(
                    "расширенный набор не закончен: начался драфт — "
                    "догрузим при следующем запуске")
            elif len(names) + len(no_role) == 0:
                report.warnings.append(f"вне пула не собрано ({role})")

    if report.patch:
        cache.set_state("patch", report.patch)
    return report


def sync_role_candidates(config: Config, cache: Cache, champs: dict,
                         index, role: str, *, progress=None,
                         abort_if=None) -> tuple[list[str], list[str], bool]:
    """Расширенный проход: данные всех чемпионов активной роли.

    Для блока «вне пула» нужны матчапы чемпионов помимо мейнов — по одним
    только мейнам пула оценить сторонний пик под текущих врагов нельзя.
    Тянем ту же страницу контрпиков (там же живёт и мета чемпиона) для
    каждого чемпиона, который на этой роли вообще растёт. Чемпионы без
    страницы роли (OP.GG отдаёт пустоту) просто пропускаются: это не ошибка,
    а ожидаемый фильтр «кто играет на роли».

    abort_if — вызываемый () -> bool: прервать посреди прохода (начался
    драфт). Собранное сохраняется, но маркер роли НЕ ставится, чтобы
    следующий запуск догрузил недостающее.

    Возвращает (ok_names, без-данных-роли, aborted): второй список для
    сводки, чтобы не молчать о том, что часть ростера недоступна.
    """
    pool_cids = {cid for m in config.pool
                 if (cid := ch.resolve(m, index)) is not None}
    total = len(champs)
    ok: list[str] = []
    no_role: list[str] = []
    aborted = False
    done = 0
    for cid, champ in champs.items():
        if cid in pool_cids:
            continue
        if abort_if is not None and done and done % 10 == 0 and abort_if():
            aborted = True
            break
        done += 1
        if progress and done % 10 == 0:
            progress(f"чемпионы роли {role}: {done}/{total}…")
        try:
            rows, summary, slug = opgg.fetch_counters(champ.name, role)
        except Exception:                       # noqa: BLE001
            no_role.append(champ.name)
            continue
        mapped: dict[int, dict] = {}
        for opp_name, stats in rows.items():
            opp_cid = ch.resolve(opp_name, index)
            if opp_cid:
                mapped[opp_cid] = stats
        cache.save_matchups(cid, mapped)
        if summary:
            cache.save_champ_meta(cid, role, summary)
        cache.set_slug(cid, slug)
        cache.set_counter_role(cid, role)
        ok.append(champ.name)
        time.sleep(0.4)     # вежливо к сайту — запросов здесь сотни

    if not aborted:
        cache.mark_all_meta(role)
    return ok, no_role, aborted


def sync_personal(config: Config, cache: Cache, *, progress=None,
                  force: bool = False) -> tuple[list[str], list[str]]:
    """Персональные данные игрока (u.gg) + мета-предметы (lolalytics).

    Возвращает (готово, ошибки) человекочитаемо для статуса.

    Учитываем:
      - настройку источников (sources): без флага u.gg личную историю не
        тянем, без lolalytics — второй билд;
      - прокси/VPN (config.proxy) — часть сайтов отвечает только через него;
      - TTL: перечитываем не чаще personal_ttl_hours часов (по умолчанию 12),
        чтобы каждый запуск не долбил внешние сайты.
    """
    ok: list[str] = []
    failed: list[str] = []
    accounts = config.accounts
    ttl = float(config.sync.get("personal_ttl_hours", 12))

    want_ugg = config.source_on("ugg") and bool(accounts) and (
        force or cache.player_data_stale(ttl))
    want_lol = config.source_on("lolalytics")

    if want_ugg and progress:
        progress(f"личная история: {len(accounts)} акк…")
    for acc in accounts:
        if not want_ugg:
            break
        try:
            from . import ugg
            check = ugg.check_profile(acc["region"], acc["name"], acc["tag"])
            if not check.get("ok"):
                cache.save_account({**acc, "ok": False,
                                    "error": check.get("error", "")})
                failed.append(f"{acc['name']}: {check.get('error', '?')}")
                continue
            rows = ugg.matches(acc["region"], acc["name"], acc["tag"],
                               pages=int(config.sync.get(
                                   "personal_pages", 3)))
            cache.save_account({**acc, "ok": True,
                                "games": len(rows),
                                "url": ugg.profile_url(acc["region"],
                                                       acc["name"],
                                                       acc["tag"])})
            cache.save_player_matches(acc, rows)
            # личные предметы: агрегируем выигранные сборки по чемпиону/роли
            for champ_id, pack in ugg.personal_items(rows).items():
                role = ugg.UGG_TO_ROLE.get(pack["role"], "")
                if role:
                    cache.save_player_items(
                        champ_id, role,
                        [{"item_id": i, "cnt": n} for i, n in pack["items"]])
            # сводка «сколько игр/побед» по чемпионам профиля
            try:
                stats = ugg.champion_stats(acc["region"], acc["name"],
                                           acc["tag"])
                cache.save_player_champ(stats)
            except Exception:                        # noqa: BLE001
                pass
            ok.append(f"{acc['name']}: {len(rows)} матчей")
        except Exception as e:                       # noqa: BLE001
            failed.append(f"{acc['name']}: {e}")
        time.sleep(0.4)

    if want_lol and progress:
        progress("мета-предметы lolalytics…")
    from . import champions as ch_mod
    from . import lolalytics
    champs, ver = ch_mod.load_champions()
    index = ch_mod.build_index(champs)
    for entry in config.pool:
        name = entry if isinstance(entry, str) else entry.get("name", "")
        cid = ch_mod.resolve(name, index)
        if cid is None:
            continue
        role = cache.counter_role(cid) or config.role
        if cache.meta_items_stale(cid, role, ttl, ver):
            try:
                build = lolalytics.fetch_build(name, role,
                                               opgg.slug_candidates)
            except Exception as e:                   # noqa: BLE001
                failed.append(f"{name}: lolalytics: {e}")
                continue
            cache.save_meta_items(cid, role, build["core"], build["patch"])
            ok.append(f"{name}: билд {role}")
            time.sleep(0.3)
        # главная руна по линии — «одна большая», к предметам липнет
        if cache.rune(cid, role) is None:
            try:
                rune = lolalytics.fetch_runes(name, role,
                                              opgg.slug_candidates)
                cache.save_rune(cid, role, rune)
                ok.append(f"{name}: руна {rune['name']}")
            except Exception as e:                   # noqa: BLE001
                failed.append(f"{name}: руна lolalytics: {e}")
            time.sleep(0.3)
    return ok, failed
