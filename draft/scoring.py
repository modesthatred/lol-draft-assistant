"""Скоринг: ранжирование твоих мейнов под конкретный драфт.

Что учитываем и в каких пропорциях (веса настраиваются в конфиге):

1. Контрпик (вес ~55%). Среднее отклонение винрейта твоего мейна против
   каждого пика врага от 50%. Дополнительно подмешиваем худший матчап —
   фарм одного плохого врага ломает игру сильнее, чем средний итог.

2. Синергия с алли (вес ~30%). Среднее отклонение винрейта мейна в паре с
   каждым уже выбранным союзником от 50%. Это то, что отличает сборку от
   простого контрпика: один и тот же чемп хорош против врагов, но плохо
   вписывается в конкретную команду.

3. Состав команды (вес ~15%). Баланс урона AD/AP, наличие танка/фронтлайна,
   роль. Определяется по тегам чемпионов из ddragon.

Итоговая цифра, которую показываем под иконкой — оценочный винрейт в этой
конкретной игре, а не винрейт мейна в патче. Это честная оценка: при
отсутствии данных части матчапа она приблизительная, поэтому рядом с
оценкой показывается уверенность (сколько матчапов и синергий учтено).
"""
from __future__ import annotations

from dataclasses import dataclass, field

NEUTRAL = 50.0
MIN_WR, MAX_WR = 25.0, 78.0


@dataclass
class Pick:
    cid: int
    name: str
    icon: str
    est_winrate: float = NEUTRAL
    counter_dev: float = 0.0
    synergy_dev: float = 0.0
    comp_bonus: float = 0.0
    counters_used: int = 0
    synergy_used: int = 0
    tags: tuple[str, ...] = ()
    damage_class: str = "AD"
    banned_by_enemy: bool = False
    banned_by_ally: bool = False
    ban_risk: float = 0.0
    notes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    data_ok: bool = True

    @property
    def confidence_pct(self) -> int:
        return int(round(self.confidence * 100))


def _clamp(v: float, lo: float = MIN_WR, hi: float = MAX_WR) -> float:
    return max(lo, min(hi, v))


def _mean_dev(pairs: list[tuple[float, float]]) -> float:
    """pairs: [(win_rate, weight)] -> среднее отклонение от 50%."""
    num = sum((wr - NEUTRAL) * w for wr, w in pairs)
    den = sum(w for _, w in pairs)
    return num / den if den else 0.0


def _comp_bonus(main_tags: tuple[str, ...], main_damage: str,
                allies: list, enemies: list) -> tuple[float, list[str]]:
    """Оценка того, насколько чемп нужен именно этой команде.

    allies/enemies — списки объектов Champion (у нас есть теги и тип урона).
    Возвращает бонус в процентных пунктах и список пояснений.
    """
    notes: list[str] = []
    bonus = 0.0
    if not allies:
        return 0.0, notes

    ally_ad = sum(1 for c in allies if c.damage_class == "AD")
    ally_ap = len(allies) - ally_ad
    ally_tanks = sum(1 for c in allies if c.is_tank)
    ally_mages = sum(1 for c in allies if c.is_mage)

    is_tank = "Tank" in main_tags
    is_ad = main_damage == "AD"

    # Не хватает фронтлайна — танк ценен
    if is_tank and ally_tanks == 0:
        bonus += 1.2
        notes.append("нет танка в команде")

    # Дисбаланс урона
    if is_ad and ally_ad >= 3 and ally_ap == 0:
        bonus -= 1.0
        notes.append("команда вся на физическом уроне")
    if main_damage == "AP" and ally_ap == 0 and ally_ad >= 2:
        bonus += 0.8
        notes.append("добавит магический урон в команду")
    if is_ad and ally_ad == 0:
        bonus += 0.6
        notes.append("добавит физический урон в команду")

    # Две и более маг-поддержки/мага — AP может быть лишним
    if main_damage == "AP" and ally_mages >= 2:
        bonus -= 0.5
        notes.append("в команде уже два мага")

    # Вражеский фронтлайн: танк/контрпик-герой полезнее
    enemy_tanks = sum(1 for c in enemies if c.is_tank)
    if is_tank and enemy_tanks >= 2:
        bonus += 0.4
        notes.append("нужен фронтлайн против танков")

    return bonus, notes


def evaluate(pool: list, draft, champs: dict, cache, config,
             role: str | None = None) -> list[Pick]:
    """Ранжирует пул против снимка драфта. Возвращает список от лучшего.

    role — позиция, по которой считаем синергии и метрики. Обычно это роль
    из драфта, а не ручная: в мультипуле счи��ать лесные синергии для
    саппорт-пика бессмысленно.
    """
    w = config.weights
    wc = float(w.get("counter", 0.55))
    ws = float(w.get("synergy", 0.30))
    wcomp = float(w.get("comp", 0.15))
    worst_share = float(w.get("worst_case", 0.35))

    # Пре-пик — это уже выбор команды: с ним считаются и контрпик, и
    # синергия. Иначе рекомендация пересчитывалась бы в момент, когда
    # союзник только навёл чемпа, и была бы случайной.
    enemy_ids = list(dict.fromkeys(draft.enemies))
    ally_ids = [c for c in dict.fromkeys(draft.allies)]
    # Для синергии свой чемпион — не союзник: пары «Шен с Шеном» в данных
    # нет, и такая пара портила бы оценку сильнее, чем помогала.
    synergy_ids = [c for c in ally_ids if c != draft.my_champion]
    ally_pool = [champs[c] for c in ally_ids if c in champs]
    enemy_pool = [champs[c] for c in enemy_ids if c in champs]
    enemy_bans = set(draft.enemy_bans)
    ally_bans = set(draft.ally_bans)
    role = role or getattr(draft, "my_role", "") or config.role

    picks: list[Pick] = []
    for main in pool:
        cid = main.cid
        mu = cache.matchups(cid)
        sy = cache.synergies(cid, role)

        # --- контрпик против врагов ---
        c_pairs: list[tuple[float, float]] = []
        devs: list[float] = []
        for e in enemy_ids:
            d = mu.get(e)
            if d and d.get("win_rate") is not None:
                c_pairs.append((d["win_rate"], 1.0))
                devs.append(d["win_rate"] - NEUTRAL)
        counter_mean = _mean_dev(c_pairs)
        counter_worst = min(devs) if devs else 0.0
        counter_dev = (1 - worst_share) * counter_mean + worst_share * counter_worst \
            if devs else 0.0

        # --- синергия с союзниками ---
        s_pairs: list[tuple[float, float]] = []
        for a in synergy_ids:
            d = sy.get(a)
            if d and d.get("win_rate") is not None:
                s_pairs.append((d["win_rate"], 1.0))
        synergy_dev = _mean_dev(s_pairs)

        # --- состав ---
        comp, notes = _comp_bonus(main.tags, main.damage_class,
                                  ally_pool, enemy_pool)

        est = _clamp(NEUTRAL + wc * counter_dev + ws * synergy_dev
                     + wcomp * comp)

        p = Pick(cid=cid, name=main.name, icon=main.icon,
                 est_winrate=round(est, 1),
                 counter_dev=round(counter_dev, 2),
                 synergy_dev=round(synergy_dev, 2),
                 comp_bonus=round(comp, 2),
                 counters_used=len(c_pairs),
                 synergy_used=len(s_pairs),
                 tags=main.tags,
                 damage_class=main.damage_class,
                 banned_by_enemy=cid in enemy_bans,
                 banned_by_ally=cid in ally_bans,
                 ban_risk=float(cache.champ_meta(cid, role).get("ban_rate")
                                or 0.0),
                 notes=list(notes))

        total_possible = len(enemy_ids) + len(synergy_ids)
        p.confidence = (p.counters_used + p.synergy_used) / total_possible \
            if total_possible else 1.0

        # человекочитаемые пояснения по вкладу каждого фактора
        if devs:
            worst_vs = min((mu[e]["win_rate"], e) for e in enemy_ids
                           if e in mu and mu[e].get("win_rate") is not None)[1]
            wname = champs[worst_vs].name if worst_vs in champs else "?"
            if counter_worst < -1.0:
                p.notes.append(f"плохой матчап: {wname}")
        if s_pairs:
            best_with = max((sy[a]["win_rate"], a) for a in synergy_ids
                            if a in sy and sy[a].get("win_rate") is not None)
            bname = champs[best_with[1]].name if best_with[1] in champs else "?"
            if best_with[0] >= 53.0:
                p.notes.append(f"сильнее всего с {bname}")
            elif synergy_dev < -0.8:
                p.notes.append("плохо вписывается в состав")

        picks.append(p)

    # Чемпионы, для которых нет ни одного матчапа/синергии, уводим вниз:
    # их оценка в 50% — это отсутствие данных, а не реальный винрейт.
    for p in picks:
        p.data_ok = (p.counters_used + p.synergy_used) > 0
        if not p.data_ok:
            p.notes.append("нет данных за патч")
        if p.banned_by_ally and not p.banned_by_enemy:
            p.notes.append("забанен нашей командой")

    # Забаненные идут в самый низ: пикать их всё равно нельзя, и оставлять
    # их наверху — значит показывать человеку то, что он не выберет.
    picks.sort(key=lambda p: (p.banned_by_enemy or p.banned_by_ally,
                              not p.data_ok, -p.est_winrate, -p.confidence))
    return picks


def ban_advice(pool: list, draft, champs: dict, cache, config,
               role: str | None = None, limit: int = 3) -> list[Pick]:
    """Кого выгоднее забанить на своей позиции.

    Бан — единственный ход, где мы не выбираем из своего пула. Его смысл
    обратный: помешать врагам забрать то, что бьёт именно нас. Поэтому
    кандидаты ранжируются по трём сигналам, а не по общему винрейту:

    1. наведение/пре-пик врага — угроза уже сформулирована, бан её снимает;
    2. пре-пики своей команды — эти чемпионы уже объявлены, их контрпики
       дороже запереть в бане, чем защищать свой пул;
    3. его винрейт против наших мейнов — плохой матчап это тот случай,
       когда один бан дороже двух пиков.

    Пул мейнов берём не весь, а только совпадающий с ролью: на позиции леса
    баны про саппорта-мейна (Эш, Мел) — бессмыслица, это спам от чужой
    роли. Если под роль нет ни одного мейна — остаёмся на всём пуле.

    Кандидаты отсеиваются тем, что уже нельзя или бессмысленно банить:
    забаненные любой командой, наши пики/пре-пики и уже выбранные врагами.
    """
    role = role or getattr(draft, "my_role", "") or config.role
    blocked = draft.banned_ids()
    taken = set(draft.allies) | set(draft.enemies)

    # Бан про текущую позицию, а не про все пулы сразу: матчапы мейна другой
    # роли (например саппорта при игре в лесу) дают только шум в советах.
    role_mains = [m for m in pool if cache.counter_role(m.cid) == role]
    mains = role_mains or list(pool)
    mine = {m.cid: cache.matchups(m.cid) for m in mains}
    if not mine:
        return []
    ban_rates = cache.opponent_ban_rates() or {}

    # Сигнал 1: что враг уже показал — наведение и пре-пики.
    threat = set()
    if getattr(draft, "enemy_hovered", 0):
        threat.add(draft.enemy_hovered)
    for s in getattr(draft, "enemy_slots", []):
        if s.is_prepick:
            threat.add(s.cid)
    threat.discard(0)

    # Сигнал 2: пре-пики своей команды (включая свой собственный). Их
    # контрпики — первое, что враг заберёт, поэтому баним их. Те, по кому
    # данных в кэше нет (чемпион не из пула), просто не дают бонуса.
    protect = set()
    my = getattr(draft, "my_prepick", 0) or getattr(draft, "hovered_champion", 0)
    if my:
        protect.add(my)
    for cid in getattr(draft, "ally_prepicks", []) or []:
        protect.add(cid)
    protect.discard(0)
    protect_match: dict[int, dict] = {}
    for cid in protect:
        mu = cache.matchups(cid)
        if mu:
            protect_match[cid] = mu

    def protect_bonus(cid: int) -> tuple[float, list[str]]:
        """Насколько этот кандидат бьёт пре-пики своей команды."""
        devs, whom = [], []
        for pid, mu in protect_match.items():
            d = mu.get(cid)
            if d and d.get("win_rate") is not None:
                # win_rate — винрейт пре-пика против кандидата; его контрпик
                # тем опаснее, чем ниже этот винрейт.
                devs.append(NEUTRAL - d["win_rate"])
                whom.append(getattr(champs.get(pid), "name", "") or f"#{pid}")
        if not devs:
            return 0.0, []
        return 0.6 * sum(devs) / len(devs), whom

    out: list[Pick] = []
    for cid, champ in champs.items():
        if cid in blocked or cid in taken or (cid and cid in protect):
            continue
        # devs ниже — винрейт нашего мейна против кандидата. Чем он ниже,
        # тем сильнее кандидат бьёт пул, то есть тем ценнее бан. Поэтому в
        # скоринг идёт обратная величина (50 - winrate).
        eff: list[float] = []
        for mu in mine.values():
            d = mu.get(cid)
            if d and d.get("win_rate") is not None:
                eff.append(NEUTRAL - d["win_rate"])
        # Без данных о мейнах кандидат есть смысл банить только если враг
        # сам показывает его — иначе это тыкание пальцем в небо.
        if not eff and cid not in threat:
            continue
        mean = sum(eff) / len(eff) if eff else 0.0
        strong = max(eff) if eff else 0.0
        br = float(ban_rates.get(cid, 0.0) or 0.0)
        # бан-рейт в процентах переводим в те же единицы, что и отклонение
        # винрейта, иначе он перевесит всё остальное
        score = mean + 0.35 * strong + 0.25 * (br - 10.0)
        p = Pick(cid=cid, name=champ.name, icon=champ.icon,
                 est_winrate=_clamp(NEUTRAL + score),
                 counter_dev=round(mean, 2),
                 tags=getattr(champ, "tags", ()),
                 damage_class=getattr(champ, "damage_class", "AD"),
                 ban_risk=br,
                 counters_used=len(eff))
        p.data_ok = True

        # Контрпик объявленного пика своей команды стоит дороже, чем защита
        # своего пула: враг уже знает, что мы берём.
        bonus, whom = protect_bonus(cid)
        if bonus:
            score += bonus
            p.notes.append("защищает препик: " + ", ".join(dict.fromkeys(whom)))
        # Враг показал выбор — снимаем именно его угрозу прежде всего.
        if cid in threat:
            score += 12.0
            p.notes.append("враг наводит — бан снимает угрозу")
        p.est_winrate = _clamp(NEUTRAL + score)
        if br >= 35:
            p.notes.append(f"банят {br:.0f}%")
        if strong >= 3.0:
            p.notes.append(f"бьёт твой пул на {strong:.0f}% выше 50")
        out.append(p)

    out.sort(key=lambda p: (-p.est_winrate, -p.ban_risk))
    return out[:limit]


def top_off_pool(pool: list, draft, champs: dict, cache, config,
                 role: str | None = None, limit: int = 3) -> list[Pick]:
    """Топ пиков вне твоего пула под текущий драфт (блок «вне пула»).

    Считается теми же формулами, что и пул (evaluate): оценивается винрейт
    кандидата против текущего состава врагов, с синергией и составом. Разница
    в источнике данных — кандидаты это чемпионы активной роли (у них в кэше
    есть role-aware матчапы из расширенного синка), а не мейны пула.

    Исключаются: мейны пула (блок про «помимо пула»), уже забаненные и уже
    выбранные любой командой. Кандидатов без данных матчапа опускаем — их
    оценка в 50% это отсутствие данных, а не реальный винрейт.
    """
    role = role or getattr(draft, "my_role", "") or config.role
    pool_ids = {m.cid for m in pool}
    blocked = draft.banned_ids()
    taken = set(draft.allies) | set(draft.enemies)
    candidates = [champs[cid] for cid in champs
                  if cid not in pool_ids and cid not in taken
                  and cid not in blocked
                  and cache.counter_role(cid) == role]
    if not candidates:
        return []
    picks = evaluate(candidates, draft, champs, cache, config, role=role)
    out = [p for p in picks if p.data_ok
           and p.cid not in pool_ids and p.cid not in taken
           and not p.banned_by_enemy and not p.banned_by_ally]
    return out[:limit]
