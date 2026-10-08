"""Точка входа. Связывает LCU -> скоринг -> оверлей и держит хоткей.

Обычный запуск:       python -m draft
Синхронизация:        python -m draft --sync
Разовая выдача в консоль: python -m draft --once
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
import time

from . import champions as ch
from . import trace
from .cache import Cache
from .hotkey import HotkeyListener
from .lcu import LcuUnavailable, fetch_session
from .scoring import evaluate
from .settings import (APP_DIR, DEFAULT_DB, ensure_dirs, load_config)
from .sync import sync

log = logging.getLogger("draft")

# Как часто опрашивать драфт, пока оверлей виден. Пользователь просил
# «1–2 секунды»: чаще — лишние запросы в LCU, реже — ощутимая задержка
# после чужого бана или пре-пика. 1.5 с попадает в середину.
POLL_SECONDS = 1.5


def setup_logging() -> None:
    """В .exe консоли нет, поэтому весь вывод идёт в файл рядом с данными."""
    ensure_dirs()
    _force_utf8_console()
    handler = logging.FileHandler(APP_DIR / "app.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.propagate = False


def _force_utf8_console() -> None:
    """Переводим stdout/stderr в UTF-8 с заменой непечатных символов.

    Консоль Windows по умолчанию cp1251/cp866, и служебный текст с ⚙, ▸, ~
    или кириллицей на ней падает с UnicodeEncodeError — причём падает уже
    после старта, когда пользователь думает, что всё работает. В .exe потоков
    нет, поэтому проверка тут же и выходит.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:                           # noqa: BLE001
            pass


def _has_console() -> bool:
    """Есть ли вообще stdout. У собранного --windowed .exe он равен None.
    Именно isatty() НЕ подходит: перенаправление в файл — это тоже
    нормальный вывод, который нужно сохранить."""
    return getattr(sys, "stdout", None) is not None


def acquire_single_instance() -> bool:
    """Не даём запустить вторую копию: два оверлея перекрываются, а глобальный
    хоткей забирает первая копия. Windows-мьютекс — просто и надёжно."""
    global _mutex_handle
    import ctypes
    ERROR_ALREADY_EXISTS = 183
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Global\\LoLDraftAssistant_SingleInstance")
    return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS


_mutex_handle = None


def say(text: str) -> None:
    """Вывод пользователю: консоль (python -m draft) + всегда в app.log
    (в .exe консоли нет)."""
    log.info("%s", text)
    if _has_console():
        print(text)


def notify(text: str, *, title: str = "LoL Draft Assistant",
           error: bool = False) -> None:
    """В консольном режиме — печать, в .exe — диалоговое окно."""
    log.info("%s: %s", title, text)
    if _has_console():
        print(text)
        return
    try:
        from tkinter import messagebox
        (messagebox.showerror if error else messagebox.showinfo)(
            title, text)
    except Exception:                          # noqa: BLE001
        pass


def ready_message(err: str) -> tuple[bool, str]:
    """Обычное ожидание или настоящая поломка?

    «Lockfile не найден» (клиент не запущен), «Сейчас не драфт» (сидим в
    лобби) и звонок к клиенту «ещё думает» — не ошибки приложения, а
    состояния готовности. Показывать их красным «ошибкой» — значит внушать,
    что программа сломалась, и отбивать желание идти искать игру.
    Возвращает (calm, text): calm=True — нейтральная строка готовности,
    calm=False — реальная беда, её можно рисовать красным.
    """
    # Строки выводятся в шапку оверлея (ширина ~250 px) и должны влезать
    # в одну строку — иначе сообщение разъезжается на несколько рядов.
    if err.startswith("Lockfile не найден"):
        return True, "клиент не запущен — жду запуска игры"
    if err.startswith("Сейчас не драфт"):
        return True, "готов: жду драфт"
    if any(t in err for t in ("не удалось достучаться", "InProgress",
                              "404", "GameFlow")):
        return True, "клиент ещё созревает"
    return False, f"проблема с клиентом: {err}"


def build_pools(config, cache: Cache):
    """Мейны, разложенные по ролям: {"jungle": [Champion, ...], ...}.

    Раньше был один плоский пул, но в драфте роль меняется: попросят
    саппортом — а пул лесной, и считать нечего. Плюс роль нужна самой
    оценке (матчапы и синергии берутся по ней).
    """
    champs, _ = ch.load_champions()
    if not champs:
        champs = {}
        for row in cache.conn.execute("SELECT * FROM champions"):
            champs[row["cid"]] = ch.Champion(cid=row["cid"],
                                             name=row["name"] or "?",
                                             icon=row["icon"] or "",
                                             tags=tuple(
                                                 filter(None, (row["tags"] or "").split(","))),
                                             damage=row["damage"] or "")
    index = ch.build_index(champs)
    pools: dict[str, list] = {}
    missing: list[str] = []
    for role, names in config.pools.items():
        got = []
        for entry in names:
            name = entry if isinstance(entry, str) else entry.get("name", "")
            cid = ch.resolve(name, index)
            if cid and cid in champs:
                got.append(champs[cid])
            else:
                missing.append(str(name))
        if got:
            pools[role] = got
    return champs, pools, missing


DD_ITEM = "https://ddragon.leagueoflegends.com/cdn/{v}/img/item/{i}.png"
# как часто спрашивать у Data Dragon текущий патч (раз в 6 часов достаточно:
# патч всё равно не выходит среди ночи за 5 минут)
PATCH_CHECK_EVERY = 6 * 3600


def build_for_pick(draft, cache, config, champs) -> dict | None:
    """Билд для пика, который уйдёт в игру.

    Берём сначала закреплённый пик, иначе того, кого навели прямо сейчас:
    и то и другое отвечает на вопрос «что мне собирать».
    """
    if draft is None:
        return None
    if not config.items.get("enabled", True):
        return None
    cid = getattr(draft, "my_champion", 0) or getattr(draft, "hovered_champion", 0)
    if not cid:
        return None
    role = cache.counter_role(cid) or config.role
    items = cache.items(cid, role, limit=config.items_count)
    if not items:
        # роль пика может не совпасть с ролью в конфиге (пил top-чемпиона при
        # пуле на mid) — тогда берём любую роль, для которой билд есть,
        # вместо того чтобы молча показать пустоту
        for alt in cache.item_roles(cid):
            if alt == role:
                continue
            candidate = cache.items(cid, alt, limit=config.items_count)
            if candidate:
                role, items = alt, candidate
                break
    boots = cache.boots(cid, role) if config.items.get("show_boots", True) \
        else None
    if not items:
        return None
    patch = (items[0].get("patch") or cache.get_state("patch") or "").strip()
    if not patch:
        return None

    def entry(row: dict) -> dict:
        iid = row.get("item_id") or 0
        return {"item_id": iid, "name": row.get("name") or "",
                "win_rate": row.get("win_rate") or 0.0,
                "url": DD_ITEM.format(v=patch, i=iid) if iid else ""}

    champ = champs.get(cid) if isinstance(champs, dict) else None
    build = {
        "cid": cid,
        "name": getattr(champ, "name", "") or f"чемпион #{cid}",
        "locked": bool(getattr(draft, "my_champion_locked", False)),
        "role": role,
        "patch": patch,
        "items": [entry(r) for r in items],
        "boots": entry(boots) if boots else None,
    }
    # Ситуативные предметы под видимых врагов: оп.гг-порядок не трогаем, но
    # подсказка «антихил/щиты/МР/армор» должна быть привязана к драфту, а не
    # висеть статично при каждом пике.
    enemies = getattr(draft, "enemies", None)
    if enemies and isinstance(champs, dict):
        from .situational import situational
        build["situational"] = situational(enemies, champs)
    return build


def render_header(draft, config, n, role: str = "") -> str:
    """Заголовок оверлея.

    n — словарь чемпионов из кэша. Раньше третьим аргументом передавали
    len(picks), и строка «враги: ...» падала с AttributeError, как только
    в драфте появлялся хоть один враг, — то есть почти всегда.
    """
    from .lcu import ROLE_RU

    role = role or config.role
    if draft is None:
        return f"роль: {ROLE_RU.get(role, role)}  ·  нет данных драфта"
    bits = [f"роль: {ROLE_RU.get(role, role)}"]
    if draft.enemies:
        bits.append("враги: " + ", ".join(
            getattr(n.get(e), "name", "") or f"#{e}" for e in draft.enemies))
    elif draft.enemy_bans:
        bits.append(f"баны врагов: {len(draft.enemy_bans)}")
    if draft.is_my_turn:
        bits.append("твой ход" if not draft.is_ban_phase else "твой бан")
    bits.append(f"{draft.timer_seconds:.0f}с" if draft.timer_seconds else "")
    return "  ·  ".join(x for x in bits if x)


def render_extra(draft, champs, config, role: str, status: dict | None,
                 pool: list, cache: Cache) -> dict:
    """Всё, что оверлей рисует помимо списка мейнов.

    Собирается здесь, а не в UI, чтобы вёрстка не лезла в кэш и LCU:
    подписывающий слой — это просто словарь.
    """

    def name(cid: int) -> str:
        c = champs.get(cid)
        return getattr(c, "name", "") or f"#{cid}"

    def icon(cid: int) -> str:
        return getattr(champs.get(cid), "icon", "") or ""

    extra: dict = {
        "role": role,
        "role_auto": bool(getattr(draft, "my_role", "")) and
                      config.active_role(draft.my_role) == role,
        "role_manual": config.role,
        "status": status or {},
    }
    if draft is None:
        return extra

    extra["board"] = {
        "my_cell": draft.my_cell,
        "allies": [(s.cid and name(s.cid) or "", s.role, s.is_prepick,
                    s.cell == draft.my_cell)
                   for s in draft.ally_slots],
        "enemies": [(s.cid and name(s.cid) or "", s.role, s.is_prepick, False)
                    for s in draft.enemy_slots],
    }
    extra["bans"] = {
        "ally": [name(c) for c in dict.fromkeys(draft.ally_bans)],
        "enemy": [name(c) for c in dict.fromkeys(draft.enemy_bans)],
    }
    # Совет по бану считаем только когда бан действительно наш ход: в фазах
    # пика это лишние пересчёты и шум в интерфейсе.
    if draft.is_ban_phase and pool:
        from .scoring import ban_advice

        extra["ban_advice"] = [
            {"cid": p.cid, "name": p.name, "icon": icon(p.cid),
             "note": " · ".join(p.notes[:2])}
            for p in ban_advice(pool, draft, champs, cache, config,
                                role=role)
        ]
    # Блок «вне пула»: топ пиков помимо мейнов для активной роли. Data есть
    # только после расширенного синка, поэтому молча пустой, пока она не
    # собрана (см. sync_role_candidates).
    if pool:
        from .scoring import top_off_pool

        extra["off_pool"] = top_off_pool(pool, draft, champs, cache, config,
                                         role=role)
    return extra


class App:
    def __init__(self, args):
        ensure_dirs()
        self.config = load_config(args.config)
        self.cache = Cache(args.db or DEFAULT_DB)
        self.listener = None

        self.champs, self.pools, self.missing = build_pools(self.config,
                                                            self.cache)
        self.last_sync_summary = ""
        self.last_sync_failed: list = []
        # живое состояние синка — его читает нижний бар оверлея. Пишется из
        # фонового потока, читается из главного: словарь небольшой, обновление
        # целиком перезаписью словаря (update) атомарно под GIL.
        self._sync_state: dict = {
            "running": False, "stage": "", "done": 0, "total": 0, "last": "",
            "net": False, "eta": 0, "start": None,
        }
        self._force_sync = False
        self._hotkey_in_use = self.config.hotkey
        self.refresh_fn = None
        self._alive = True
        self._overlay = None
        self._workers: list[threading.Thread] = []
        # подпись последнего показанного драфта: перерисовываем, только когда
        # состав или фаза действительно изменились
        self._last_sig: tuple = ()
        self.tray = None
        log.info("config=%s db=%s", args.config or "по умолчанию",
                 self.cache.path)
        if self.missing:
            say("не найдены чемпионы: " + ", ".join(self.missing))
        # пустой пул — не ошибка: значит, пользователь ещё не выбрал мейнов.
        # В GUI мы просто откроем окно настроек, в --once/--sync скажем об этом.
        self.needs_setup = not self.pools
        if self.needs_setup:
            say("пул пуст — откроются настройки, выбери 5-6 мейнов")
        else:
            for role, pool in self.pools.items():
                say(f"пул {role}: " + ", ".join(p.name for p in pool))

    # ---------- данные ----------
    @property
    def pool(self) -> list:
        """Мейны ручной роли — для --once, --sync и проверки устаревания."""
        return self.pools.get(self.config.role) or next(
            iter(self.pools.values()), [])

    def pool_for(self, role: str) -> list:
        """Мейны роли из драфта. Если под неё пул не заполнен, берём ручной —
        пустой оверлей в драфте хуже, чем список с чужой ролью."""
        return self.pools.get(role) or self.pool

    def active_role(self, draft=None) -> str:
        detected = getattr(draft, "my_role", "") if draft else ""
        role = self.config.active_role(detected)
        if role not in self.pools:
            # роль без пула: пробуем доп. роль (автофилл), затем основную
            for cand in (self.config.role2, self.config.role):
                if cand in self.pools:
                    role = cand
                    break
            else:
                role = next(iter(self.pools), role)
        return role

# ---------- патч-осведомлённое обновление ----------
    # Данные OP.GG после патча меняются заметно, но по времени они ещё
    # «свежие»: если считать только max_age_hours, то после патча приложение
    # до суток будет показывать винрейты и билды, которые уже неактуальны.

    def refresh_patch(self, force: bool = False) -> str:
        """Узнаёт текущий патч, но не чаще раза в 6 часов.

        Храним два значения: patch_seen — что сейчас на сервере, patch — под
        какой патч собраны наши данные. Разошлись — пора перекачивать.
        """
        now = time.time()
        if not force:
            last = self.cache.get_state("patch_checked_at")
            try:
                if last and now - float(last) < PATCH_CHECK_EVERY:
                    return self.cache.get_state("patch_seen") or ""
            except (TypeError, ValueError):
                pass
        ver = ch.current_patch()
        self.cache.set_state("patch_checked_at", str(now))
        if ver:
            self.cache.set_state("patch_seen", ver)
        return ver

    def patch_changed(self) -> bool:
        """Вышел ли патч, которого ещё нет в нашей базе."""
        seen = self.cache.get_state("patch_seen")
        known = self.cache.get_state("patch")
        return bool(seen and known and seen != known)

    def needs_sync(self) -> bool:
        # устаревание проверяем по всем пулам: переключился на другую роль —
        # а статистики для неё ещё нет, и оверлей покажет пустоту
        every = [p for pool in self.pools.values() for p in pool]
        for p in every:
            if self.cache.is_stale(p.cid,
                                   float(self.config.sync.get(
                                       "max_age_hours", 24))):
                return True
        # патч мог смениться, а по времени база ещё свежая
        try:
            self.refresh_patch()
        except Exception:                             # noqa: BLE001
            log.exception("patch check failed")
        if self.patch_changed():
            log.info("patch changed: %s -> %s", self.cache.get_state("patch"),
                     self.cache.get_state("patch_seen"))
            return True
        if self.config.items.get("enabled", True):
            for p in every:
                role = self.cache.counter_role(p.cid) or self.config.role
                if self.cache.items_stale(
                        p.cid, role,
                        float(self.config.sync.get("max_age_hours", 24)),
                        self.cache.get_state("patch_seen") or ""):
                    return True
        return False

    def _draft_active(self) -> bool:
        """Идёт ли сейчас пик-бан — в это время расширенный набор не качаем."""
        try:
            fetch_session(timeout=1.0)
            return True
        except LcuUnavailable:
            return False

    # Честный прогресс-бар: "чемпионы роли jungle: 40/170..." — это done/total.
    _PROG_RE = re.compile(r"(\d+)/(\d+)")

    def _parse_progress(self, msg: str) -> dict:
        """Достаём из строки синка стадию, счётчики, сеть/локально и ETA.

        msg — строка из sync(): «[3/6] Vi: контрпики…» и т.п. ETA считаем от
        фактической скорости (сделанного за время), а не от константы.
        """
        m = self._PROG_RE.search(msg)
        state: dict = {"stage": msg, "net": True}
        if m:
            state["done"] = int(m.group(1))
            state["total"] = int(m.group(2))
        else:
            state["done"] = state["total"] = 0
        if "пропущ" in msg:
            # пропуск (например «идёт драфт») — это не работа с сетью
            state["net"] = False
        now = time.time()
        start = self._sync_state.get("start") or now
        done, total = state["done"], state["total"]
        if done and total and done < total and now > start:
            state["eta"] = int((now - start) / done * (total - done))
        return state

    def _stale_reason(self, role: str, data_patch: str,
                      league_patch: str) -> str | None:
        """Почему статистике нужен апдейт — без сети, только по кэшу.

        Это строка футера, её пересчитывают при каждой перерисовке драфта:
        сеть здесь недопустима. Патч и метки свежести уже лежат в базе.
        """
        if data_patch and league_patch and data_patch != league_patch:
            return f"данные за {data_patch} — вышло {league_patch}"
        max_age = float(self.config.sync.get("max_age_hours", 24))
        for p in [p for pool in self.pools.values() for p in pool]:
            if self.cache.is_stale(p.cid, max_age):
                return "мейны без статистики — обнови"
        if self.config.sync.get("all_champions", True):
            ttl = float(self.config.sync.get(
                "all_champions_ttl_hours", 168))
            if self.cache.all_meta_stale(role, ttl):
                return "вне пула: набор чемпионов роли устарел"
        if not data_patch:
            return "статистика ещё не собрана"
        return None

    def _footer_data(self, role: str) -> dict:
        """Свежесть данных + живой прогресс синка для нижнего бара оверлея."""
        data_patch = self.cache.get_state("patch") or ""
        league_patch = self.cache.get_state("patch_seen") or ""
        info = {
            "data_patch": data_patch,
            "league_patch": league_patch,
            "stale_reason": self._stale_reason(role, data_patch,
                                               league_patch),
        }
        info.update(dict(self._sync_state))
        return info

    def do_sync(self, force=True, progress=None) -> None:
        def default(msg):
            say("   " + msg)
        report = sync(self.config, self.cache, force=force,
                      progress=progress or default,
                      draft_active=self._draft_active)
        self.last_sync_summary = report.summary()
        say("Готово: " + self.last_sync_summary)
        self.last_sync_failed = report.failed
        for name, err in report.failed:
            log.warning("sync fail %s: %s", name, err)
            say(f"  ! {name}: {str(err)[:120]}")

    def auto_sync_if_needed(self, on_status=None, on_done=None) -> None:
        """Первый запуск / устаревший кэш — обновляем сами, в фоне,
        чтобы окно не ждало сеть."""
        if not self.needs_sync() and not self._force_sync:
            return None

        def worker():
            try:
                self._sync_state.update({
                    "running": True, "stage": "", "done": 0, "total": 0,
                    "last": "", "net": True, "eta": 0, "start": time.time(),
                })
                if on_status:
                    on_status("обновляю статистику с OP.GG…")

                def progress(msg):
                    self._sync_state.update(self._parse_progress(msg))
                    if on_status:
                        on_status(msg)

                self.do_sync(force=True, progress=progress)
                log.info("auto-sync finished")
                summary = self.last_sync_summary or ""
                if on_status:
                    on_status("статистика обновлена: " + summary
                              if summary else "статистика обновлена")
            except Exception as e:                # noqa: BLE001
                log.exception("auto-sync failed")
                if on_status:
                    on_status(f"не удалось обновить: {e}")
            finally:
                self._sync_state.update(
                    {"running": False, "net": False, "eta": 0,
                     "last": self.last_sync_summary or ""})
                # перечитываем пул: после синка могли появиться иконки/метрики
                try:
                    self.champs, self.pools, self.missing = build_pools(
                        self.config, self.cache)
                except Exception:                   # noqa: BLE001
                    pass
                if on_done:
                    on_done()

        t = threading.Thread(target=worker, daemon=True)
        self._workers.append(t)
        t.start()
        return t

    def _start_worker(self, target) -> threading.Thread:
        """Запуск короткого фонового воркера с учётом в списке.

        Воркер обязательно попадает в self._workers: teardown() дожидается их
        перед сносом Tk, иначе поток переживёт окно.
        """
        t = threading.Thread(target=target, daemon=True)
        self._workers.append(t)
        t.start()
        return t

    def _join_workers(self, timeout: float = 3.0) -> None:
        """Дожидаемся фоновых синков перед тем, как сносить Tk.

        Воркер может стоять на after() из своего потока; если в этот момент
        окно уничтожить, Tcl роняет весь интерпретатор. Поэтому выход идёт
        через join, а не через «авось успеет».
        """
        alive = [t for t in self._workers if t.is_alive()]
        for t in alive:
            t.join(timeout)
        still = [t for t in alive if t.is_alive()]
        if still:
            log.warning("%d sync thread(s) still running after join",
                        len(still))
        self._workers = [t for t in self._workers if t not in still]

    def _status_sink(self):
        """Куда писать статус синка. Поток синка не главный, поэтому
        текст уходит в очередь оверлея, а не трогает Tk напрямую."""
        return getattr(self, "_status_cb", None)

    def apply_settings(self, refresh=None) -> None:
        """После сохранения настроек перечитываем пул и обновляем оверлей."""
        old_hotkey = self._hotkey_in_use
        self.champs, self.pools, self.missing = build_pools(self.config,
                                                            self.cache)
        self.needs_setup = not self.pools
        if self.missing:
            say("не найдены чемпионы: " + ", ".join(self.missing))
        for role, pool in self.pools.items():
            say(f"пул {role}: " + ", ".join(p.name for p in pool))
        if self.pools:
            role_txt = "авто (по драфту)" if self.config.auto_role \
                else self.config.role
            say(f"роль: {role_txt}, хоткей: {self.config.hotkey}")

        # хоткей меняем на лету: иначе пришлось бы перезапускать приложение
        if self.listener is not None and old_hotkey and \
                old_hotkey != self.config.hotkey:
            self.listener.stop()
            self.listener = HotkeyListener(self.config.hotkey, self.refresh_fn)
            if not self.listener.start():
                say(f"! хоткей не сработал: {self.listener.error}")
            else:
                say(f"хоткей обновлён: {self.config.hotkey}")
        self._hotkey_in_use = self.config.hotkey

        # первый пул только что выбран — статистики для него ещё нет,
        # поэтому запускаем синхронизацию, иначе оверлей останется пустым
        done = refresh or self.refresh_fn
        if self.pools and done:
            self.auto_sync_if_needed(on_status=self._status_sink(),
                                     on_done=self._main_thread(done))
        if refresh:
            refresh()

    def _main_thread(self, fn):
        """Оборачивает вызов так, чтобы он пришёлся на главный поток Tk.

        Синк крутится в отдельном потоке, а трогать виджеты из него нельзя.
        Если приложение уже закрыто — не делаем ничего: после destroy() любое
        касание Tk из этого потока роняет интерпретатор.
        """
        root = getattr(self, "_root", None)
        if root is None:
            return fn

        def safe():
            if not getattr(self, "_alive", True):
                return
            try:
                root.after(0, fn)
            except Exception:                     # noqa: BLE001
                log.debug("gui gone, callback dropped")

        return safe

    def evaluate_now(self):
        try:
            draft = fetch_session()
        except LcuUnavailable as e:
            return None, str(e)
        role = self.active_role(draft)
        pool = self.pool_for(role)
        picks = evaluate(pool, draft, self.champs, self.cache, self.config,
                         role=role)
        return (picks, draft), None

    def once(self) -> int:
        if self.needs_setup:
            say("Пул пуст — выбери мейнов в настройках (⚙ в оверлее).")
            return 2
        result, err = self.evaluate_now()
        if err:
            say(f"Нет драфта: {err}")
            return 1
        picks, draft = result
        say(render_header(draft, self.config, self.champs,
                          self.active_role(draft)))
        for i, p in enumerate(picks, 1):
            wr = "—" if not p.data_ok else f"{p.est_winrate:.1f}%"
            mark = " [забанен]" if p.banned_by_enemy else ""
            say(f"  {i:>2}. {wr:>6}  {p.name}{mark}")
            if p.notes:
                say(f"      {'; '.join(p.notes)}")
        return 0

    # ---------- оверлей ----------
    def _draft_signature(self, draft) -> tuple:
        """Что именно показываем оверлею — без плавающих секунд.

        Опрос идёт раз в POLL_SECONDS, а пересчитывать имеет смысл, только
        если состав или фаза изменились: за 2 секунды таймер отсчёта меняет
        цифру, но не меняет список мейнов. Без такой проверки список дёргался
        бы и перерисовывался бы на ровном месте.
        """
        if draft is None:
            return ()
        # hovered_champion тоже в подписи: блок предметов строится по нему,
        # и без него билд зависал на первом наведённом чемпе до конца драфта.
        return (
            draft.phase, draft.action_type, draft.is_my_turn,
            draft.is_ban_phase,
            tuple(draft.ally_bans), tuple(draft.enemy_bans),
            tuple(draft.ally_picks), tuple(draft.ally_prepicks),
            tuple(draft.enemies), draft.enemy_hovered,
            tuple((s.cid, s.is_prepick, s.role) for s in draft.ally_slots),
            tuple((s.cid, s.is_prepick, s.role) for s in draft.enemy_slots),
            draft.my_champion, draft.my_role, draft.hovered_champion,
            self.active_role(draft),
        )

    def _poll_draft(self, overlay) -> None:
        """Пересчёт драфта по таймеру, запуск — из главного потока.

        Опрос намеренно НЕ крутится в отдельном постоянном потоке: такой
        поток периодически берёт сильную ссылку на оверлей, и если главный
        поток в этот момент сносит окно, последним владельцем Tk-объектов
        становится он, а сборщик мусора удаляет их не из главного потока —
        «Tcl_AsyncDelete: async handler deleted by the wrong thread».

        Здесь же применяется уже проверенный приём синхронизации: таймер
        только запускает разовый воркер, воркер кладёт результат в очередь
        оверлея, рисование делает главный поток в _pump(). Воркер короткий,
        и teardown() его дожидается.
        """
        if not self._alive or getattr(overlay, "dead", False):
            return

        def schedule_poll():
            """Планирует следующий тик. Вызывается только из главного потока."""
            root = getattr(self, "_root", None)
            if root is None or not self._alive:
                return
            try:
                root.after(int(POLL_SECONDS * 1000), self._poll_draft,
                           overlay)
            except Exception:                       # noqa: BLE001
                log.debug("таймер опроса не установлен")

        def worker():
            # Ни одного вызова Tk: только чтение флага и queue.put внутри
            # _post_view. Следующий тик уже запланирован выше — из главного
            # потока, из воркера root.after() вызывать нельзя.
            if not self._alive:
                return
            if overlay.visible and not self.needs_setup:
                result, err = self.evaluate_now()
                draft = result[1] if result else None
                sig = self._draft_signature(draft)
                if sig and sig != self._last_sig:
                    self._last_sig = sig
                    # Пишем только на смену состояния: за драфт это десятки
                    # строк, а не тысячи. Нужен факт «что в тот момент видел
                    # клиент и что показало окно», а не каждый тик.
                    if err:
                        s = str(err)
                        trace.event("poll", ok=False, err=s)
                        # Временные состояния (переходы в LCU, драфт в прогрессе)
                        # не считаем ошибкой отрисовки: оставляем предыдущее
                        # состояние, чтобы окно не моргало при каждом тике.
                        transient = ("InProgress", "404", "не удалось достучаться",
                                     "GameFlow")
                        if any(t in s for t in transient):
                            schedule_poll()
                            self._start_worker(worker)
                            return
                    else:
                        picks, _ = result
                        trace.event(
                            "poll", ok=True,
                            state=trace.draft_snapshot(draft, self.champs),
                            allies=[trace.describe(c, self.champs)
                                    for c in draft.allies],
                            enemies=[trace.describe(c, self.champs)
                                     for c in draft.enemies],
                            top=[p.name for p in picks[:3]])
                    self._post_view(overlay, result, err, self.config,
                                    self.champs, self.cache)

        schedule_poll()
        self._start_worker(worker)

    def _post_view(self, overlay, result, err, config, champs, cache) -> None:
        """Разбор результата опроса и отправка в очередь оверлея.

        Только сбор словарей и queue.put — ни одного вызова Tk.
        """
        if err:
            # Транзиентные уже отсеяны в воркере — сюда попадает настоящий
            # статус: нет драфта / клиент не запущен / поломка.
            calm, text = ready_message(str(err))
            overlay.post_status(text) if calm else overlay.post_error(text)
            return
        picks, draft = result
        role = self.active_role(draft)
        header = render_header(draft, config, champs, role)
        extra = render_extra(draft, champs, config, role,
                             self.current_status(), self.pool_for(role),
                             cache)
        extra["footer"] = self._footer_data(role)
        # Подсказку по бану рисуем только в фазе бана, иначе она пустая.
        overlay.post(picks, header, build_for_pick(draft, cache, config,
                                                   champs), extra)

    def current_status(self) -> dict:
        """Состояние клиента для строки статуса: запущен ли и в драфте ли."""
        from .lcu import client_status

        try:
            return client_status()
        except Exception:                           # noqa: BLE001
            return {"level": "warn", "detail": "клиент недоступен"}

    def run_ui(self, force_sync=False):
        from .ui import DraftOverlay

        # on_refresh назначаем ниже: замыкания должны быть определены раньше,
        # чем оверлей начнёт по ним обращаться.
        overlay = DraftOverlay(self.config)
        def refresh():
            t0 = time.time()
            trace.event("hotkey", hotkey=self.config.hotkey)
            result, err = self.evaluate_now()
            ms = (time.time() - t0) * 1000
            if err:
                s = str(err)
                trace.event("hotkey_result", ok=False, err=s, ms=round(ms))
                transient = ("InProgress", "404", "не удалось достучаться",
                             "GameFlow")
                if any(t in s for t in transient):
                    overlay.show()
                    return
                # «нет драфта» и «клиент не запущен» — не поломка, а ожидание:
                # прятать окно нельзя (жмыханье F8 выглядит мёртвым), а рисовать
                # это красной ошибкой — внушать, что приложение сломалось.
                calm, text = ready_message(s)
                overlay.show()
                overlay.post_status(text) if calm else overlay.post_error(text)
                return
            picks, draft = result
            role = self.active_role(draft)
            header = render_header(draft, self.config, self.champs, role)
            header += f"  ·  {ms:.0f} мс"
            extra = render_extra(draft, self.champs, self.config, role,
                                 self.current_status(), self.pool_for(role),
                                 self.cache)
            extra["footer"] = self._footer_data(role)
            build = build_for_pick(draft, self.cache, self.config, self.champs)
            overlay.post(picks, header, build, extra)
            overlay.show()
            # Что показали окну — целиком. Это и есть разбор жалобы: видно и
            # состав, и подсказку по бану, и её отсутствие.
            trace.event("hotkey_result", ok=True, ms=round(ms),
                        state=trace.draft_snapshot(draft, self.champs),
                        role=role,
                        header=header,
                        picks=[[p.name, p.est_winrate, p.notes]
                               for p in picks],
                        build={k: build[k] for k in ("name", "locked")
                               if build and k in build},
                        bans=[[trace.describe(c, self.champs)
                               for c in draft.ally_bans],
                              [trace.describe(c, self.champs)
                               for c in draft.enemy_bans]],
                        allies=[trace.describe(c, self.champs)
                                for c in draft.allies],
                        enemies=[trace.describe(c, self.champs)
                                 for c in draft.enemies],
                        ban_advice=[a.get("name", "")
                                    for a in extra.get("ban_advice", [])],
                        extra_keys=sorted(extra))

        def refresh_quiet():
            """Перерисовка только если окно видно — иначе зачем читать LCU."""
            if not overlay.visible:
                return
            result, err = self.evaluate_now()
            if err:
                return
            picks, draft = result
            role = self.active_role(draft)
            extra = render_extra(draft, self.champs, self.config, role,
                                 self.current_status(),
                                 self.pool_for(role), self.cache)
            extra["footer"] = self._footer_data(role)
            overlay.post(picks,
                         render_header(draft, self.config, self.champs,
                                       role),
                         build_for_pick(draft, self.cache, self.config,
                                        self.champs),
                         extra)

        settings_wnd = {"w": None}

        def open_settings():
            # один экземпляр: левый клик по трею / ⚙ несколько раз подряд
            # не должны штабелировать окна. Повторный вызов поднимает уже
            # открытое окно.
            from .settings_ui import SettingsWindow

            w = settings_wnd["w"]
            if w is not None:
                try:
                    if w.top.winfo_exists():
                        w.top.lift()
                        w.top.focus_force()
                        return
                except Exception:                   # noqa: BLE001
                    pass
            w = SettingsWindow(overlay.root, self.config, self.cache,
                               self.champs, on_save=self.apply_settings)
            settings_wnd["w"] = w

        quitting = {"v": False}

        def teardown():
            """Гасим всё, что живёт вне Tk: хоткей и фоновые колбэки.

            Вызывается и из quit_app, и из on_gone — окно могут снести мимо
            нас (диспетчер задач, перезагрузка), а хоткей тогда остался бы
            висеть в системе и держать процесс.
            """
            if quitting["v"]:
                return
            quitting["v"] = True
            self._alive = False
            if self.tray is not None:
                try:
                    self.tray.stop()
                except Exception:                   # noqa: BLE001
                    log.exception("tray stop failed")
                self.tray = None
            if self.listener is not None:
                try:
                    self.listener.stop()
                except Exception:                   # noqa: BLE001
                    log.exception("listener stop failed")
                self.listener = None
            # ждём синк ДО destroy(): иначе он дёрнет after() в мёртвый Tk
            self._join_workers()
            # Рвём циклы App <-> overlay. Иначе сборщик мусора выбирает их
            # в фоновом потоке синка, а удалять Tk-объекты можно только из
            # главного: иначе Tcl падает с Tcl_AsyncDelete.
            overlay.on_refresh = None
            overlay.on_settings = None
            overlay.on_quit = None
            overlay.on_gone = None
            # refresh_fn замыкает на overlay, а его держит и фоновый поток
            self.refresh_fn = None
            self._status_cb = None
            self._root = None
            self._overlay = None

        def quit_app():
            """×, ПКМ→Выход и Q.

            Важно: здесь не зовём overlay.quit() — тот сам вызывает on_quit,
            и получилась бы бесконечная рекурсия. Гасим ссылку и сносим
            окно напрямую, заодно освобождая хоткей.
            """
            teardown()
            overlay.on_quit = None
            overlay.on_gone = None
            try:
                overlay.root.destroy()
            except Exception:                       # noqa: BLE001
                log.exception("root destroy failed")

        overlay.on_refresh = refresh_quiet
        overlay.on_settings = open_settings
        overlay.on_quit = quit_app
        overlay.on_gone = teardown
        self._status_cb = overlay.post_status
        self._root = overlay.root
        # сильная ссылка: пока жив App, жив и Tk-объект, иначе gc может
        # удалить его из фонового потока (см. teardown)
        self._overlay = overlay

        self.refresh_fn = refresh
        # синк идёт в отдельном потоке, а трогать Tk можно только из
        # главного — возвращаемся в него через after()
        def after_startup_sync():
            """Стартовый синк дошёл до конца — показываем готовность.

            Раньше окно после синка исчезало молча через пару секунд:
            «обновлено» на миг — и пропало. Молчаливое исчезновение выглядело
            как поломка, хотя приложение как раз готово ждать драфт. Теперь
            окно остаётся на экране и явно говорит, что ждёт; убрать его можно
            Esc или треем. Если драфт уже идёт — рисуем состав.
            """
            try:
                fetch_session()
            except LcuUnavailable as e:
                draft_ok = False
                _, waiting = ready_message(str(e))
            else:
                draft_ok = True
                waiting = "готов: жду драфт"
            if draft_ok:
                refresh()
                return
            # Итог синка (сколько обновилось, сколько «появится позже») не
            # примешиваем — строка обязана оставаться однострочной. Он в логе,
            # а футер покажет, когда данные реально устареют.
            overlay.post_status(waiting)

        def sync_then_refresh():
            self._force_sync = force_sync
            # обновление пойдёт — показываем окно сразу, иначе запуск выглядит
            # как «молчащий» экзешник: никакого бара, никаких дат, и неясно,
            # делает ли фон что-то вообще
            if self.needs_sync() or self._force_sync:
                overlay.post_status("обновляю статистику с OP.GG…")
                overlay.show()
            self.auto_sync_if_needed(on_status=overlay.post_status,
                                     on_done=self._main_thread(
                                         after_startup_sync))
        # Значок в трее: оверлей без заголовка и без кнопки закрытия, поэтому
        # «обратный адрес» к приложению должен быть всегда.
        try:
            from .tray import TrayIcon
            from .version import APP_NAME, VERSION

            tray = TrayIcon(f"{APP_NAME} {VERSION}",
                            on_restore=lambda: overlay.show(),
                            on_settings=open_settings,
                            on_quit=quit_app)
            if tray.start():
                self.tray = tray
        except Exception:                           # noqa: BLE001
            log.exception("tray init failed — закрыть можно только хоткеем")
            self.tray = None

        # Опрос драфта по таймеру: чужие баны и пре-пики меняют расчёт, а
        # ждать следующего нажатия хоткея нельзя.
        self._last_sig = ()
        self._poll_draft(overlay)

        self.listener = HotkeyListener(self.config.hotkey, refresh)
        self._hotkey_in_use = self.config.hotkey
        if not self.listener.start():
            say(f"! {self.listener.error}")
            say("  запасной вариант: держи окно клиента и жми Пробел.")

        say(f"Готово. Хоткей: {self.config.hotkey}. "
            f"Настройки — ⚙ или ПКМ по окну. Выход — × или Q.")

        # первый запуск / устаревший кэш: обновляем сами в фоне
        self._force_sync = force_sync
        sync_then_refresh()

        if self.needs_setup:
            # пустое окно бесполезно — сразу спрашиваем мейнов
            overlay.post_status("выбери роль и мейнов")
            overlay.root.after(150, open_settings)
            overlay.root.mainloop()
            return overlay

        refresh()          # первый показ сразу
        overlay.root.mainloop()
        return overlay      # возвращаем для тестов


def main(argv=None) -> int:
    setup_logging()
    ap = argparse.ArgumentParser(prog="draft",
                                 description="LoL Draft Assistant")
    ap.add_argument("--sync", action="store_true",
                    help="обновить статистику с OP.GG и выйти")
    ap.add_argument("--force", action="store_true",
                    help="обновить статистику при запуске, даже если кэш свежий")
    ap.add_argument("--once", action="store_true",
                    help="разовый расчёт в консоль")
    ap.add_argument("--config", help="путь к config.json")
    ap.add_argument("--db", help="путь к sqlite-базе")
    args = ap.parse_args(argv)

    try:
        if not acquire_single_instance():
            # Второй экземпляр не молчит и не просто ругается: он поднимает
            # уже запущенное окно. Раньше здесь был диалог, после которого
            # живой оверлей всё равно оставался не там, где его ищут.
            from .tray import surface_existing
            from .version import APP_NAME, VERSION

            if surface_existing(f"{APP_NAME} {VERSION}"):
                return 0
            notify("Приложение уже запущено — значок в трее, "
                   "ПКМ по нему → Показать оверлей или Выход.")
            return 0
        app = App(args)
    except SystemExit:
        raise
    except Exception as e:                     # noqa: BLE001
        log.exception("startup failed")
        notify(f"Не удалось запустить:\n{e}", error=True)
        return 1

    if args.sync:
        app.do_sync(force=True)
        notify("Синхронизация завершена:\n" + app.last_sync_summary)
        return 0
    if args.once:
        return app.once()
    app.run_ui(force_sync=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
