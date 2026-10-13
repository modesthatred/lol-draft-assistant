"""Персональные данные игрока с u.gg (GraphQL API).

Зачем: советы «что банить против твоих мейнов» и «какие предметы брать
именно тебе» невозможно собрать по одной мете — у игрока своя история.
u.gg отдаёт её бесплатно, без ключа:

  fetchPlayerStatistics   — победы/игры по чемпионам за сезон (роль-aware)
  fetchPlayerMatchSummaries — последние матчи: мой чемпион/роль/предметы,
                              состав обеих команд со ролями
  getCommonlyVersedOpponentChampions — против кого я чаще всего играл

Из матчей считаем то, чего нет в API готовым полем:
  - личные проигрыши против конкретного чемпиона (по роли и всего);
  - личные предметы по чемпиону и роли (что я реально собираю).
Аккаунтов до трёх (мейн + смёрфы на других серверах): их данные
складываем, у каждого свой вес по числу игр.

Роли u.gg: 1 top, 2 jungle, 3 mid, 4 adc, 5 support, 7 любая.
Сезон 26 — текущий (проверено на живых профилях в октябре 2026).
"""
from __future__ import annotations

from . import net

API = "https://u.gg/api"
PROFILE_URL = "https://u.gg/lol/profile/{region}/{name}-{tag}"

ROLE_TO_UGG = {"top": 1, "jungle": 2, "mid": 3, "adc": 4, "support": 5}
UGG_TO_ROLE = {v: k for k, v in ROLE_TO_UGG.items()}
ROLE_ANY = 7
CURRENT_SEASON = 26

# сервера, на которых чаще всего играют русскоязычные (+ мировые).
# Код — тот, который u.gg принимает и в URL профиля, и в GraphQL:
# это riot-id региона (euw1, eun1), а не «человеческий» EUW/EUNE. С сайтом
# «euw» в ссылке даёт 404, хотя профиль существует.
REGIONS = [
    ("ru", "Russia (RU)"),
    ("euw1", "Europe West (EUW)"),
    ("eun1", "Europe Nordic & East (EUNE)"),
    ("na", "North America (NA)"),
    ("kr", "Korea (KR)"),
    ("br", "Brazil (BR)"),
    ("lan", "Latin America North (LAN)"),
    ("las", "Latin America South (LAS)"),
    ("oce", "Oceania (OCE)"),
    ("tr", "Turkey (TR)"),
    ("jp", "Japan (JP)"),
    ("sg", "Singapore/Malaysia/PH (SG)"),
    ("tw", "Taiwan/HK/Macau (TW)"),
    ("vn", "Vietnam (VN)"),
    ("th", "Thailand (TH)"),
]

# Алиасы человеческих названий: раньше в настройках были euw/eune, и
# сохранённые конфиги не должны ломаться после исправления кодов.
REGION_ALIASES = {
    "euw": "euw1",
    "eune": "eun1",
    "eun": "eun1",
    "eu": "euw1",
    "west": "euw1",
    "ru": "ru",
    "kr": "kr",
    "na": "na",
    "br": "br",
    "lan": "lan",
    "las": "las",
    "oce": "oce",
    "tr": "tr",
    "jp": "jp",
    "sg": "sg",
    "tw": "tw",
    "vn": "vn",
    "th": "th",
}


def norm_region(region: str) -> str:
    """Приводит код региона к тому, что понимает u.gg."""
    code = (region or "").strip().lower()
    return REGION_ALIASES.get(code, code)

Q_CHECK = """
query($regionId:String!,$riotTagLine:String!,$riotUserName:String!){
  profileInitSimple(regionId:$regionId, riotTagLine:$riotTagLine,
    riotUserName:$riotUserName){ playerInfo { riotUserName regionId summonerLevel } }
  fetchProfileRanks(regionId:$regionId, riotTagLine:$riotTagLine,
    riotUserName:$riotUserName, seasonId:26){ rankScores { tier rank wins losses lp queueType } }
}"""

Q_CHAMPION_STATS = """
query($regionId:String!,$riotTagLine:String!,$riotUserName:String!,$seasonId:Int!,$role:Int!){
  fetchPlayerStatistics(queueType:[420,440], regionId:$regionId,
    riotTagLine:$riotTagLine, riotUserName:$riotUserName, seasonId:$seasonId,
    role:$role){
    seasonId role
    basicChampionPerformances {
      championId totalMatches wins kills deaths assists gold cs
    }
  }
}"""

Q_MATCHES = """
query($regionId:String!,$riotTagLine:String!,$riotUserName:String!,$page:Int){
  fetchPlayerMatchSummaries(regionId:$regionId, riotTagLine:$riotTagLine,
    riotUserName:$riotUserName, page:$page, queueType:[420], seasonIds:[26]){
    totalNumMatches finishedMatchSummaries
    matchSummaries {
      matchId championId win role matchCreationTime items cs kills deaths
      teamA { championId role teamId }
      teamB { championId role teamId }
    }
  }
}"""


class UggError(RuntimeError):
    """Профиль не найден / источник не ответил."""


def profile_url(region: str, name: str, tag: str) -> str:
    return PROFILE_URL.format(region=norm_region(region),
                              name=name.strip().replace(" ", "%20"),
                              tag=tag.strip())


def _gql(query: str, variables: dict) -> dict:
    raw = net.post_json(API, {"query": query, "variables": variables},
                        accept="application/json", timeout=30)
    import json
    data = json.loads(raw)
    if "errors" in data:
        msgs = "; ".join(str(e.get("message", "")) for e in data["errors"])
        raise UggError(msgs[:200])
    return data.get("data") or {}


def check_profile(region: str, name: str, tag: str) -> dict:
    """Проверка «профиль существует».

    Возвращает {"ok": True, "profile": {...}} либо {"ok": False, "error": "..."}
    — показываем пользователю в настройках галочкой/крестиком.
    """
    region = norm_region(region)
    name = name.strip()
    tag = tag.strip().lstrip("#")
    if not (region and name and tag):
        return {"ok": False, "error": "заполни все три поля"}
    try:
        data = _gql(Q_CHECK, {"regionId": region, "riotTagLine": tag,
                              "riotUserName": name})
    except (net.NetError, UggError) as e:
        return {"ok": False, "error": f"источник недоступен: {e}"}
    info = (data.get("profileInitSimple") or {}).get("playerInfo") or {}
    if not info:
        return {"ok": False, "error": "профиль не найден"}
    ranks = data.get("fetchProfileRanks") or []
    solo = {}
    for r in ranks:
        if not isinstance(r, dict):
            continue
        scores = r.get("rankScores") or []
        solo = next((s for s in scores if s.get("wins")),
                    scores[0] if scores else {})
        if solo:
            break
    return {"ok": True,
            "profile": {"level": info.get("summonerLevel"),
                        "tier": (solo or {}).get("tier"),
                        "rank": (solo or {}).get("rank"),
                        "wins": (solo or {}).get("wins"),
                        "losses": (solo or {}).get("losses"),
                        "url": profile_url(region, name, tag)}}


def champion_stats(region: str, name: str, tag: str,
                   role: str = "", season: int = CURRENT_SEASON) -> list:
    """Победы/игры по чемпионам профиля. role="" → все роли."""
    role_id = ROLE_TO_UGG.get(role, ROLE_ANY)
    data = _gql(Q_CHAMPION_STATS, {"regionId": norm_region(region),
                                   "riotTagLine": tag.strip().lstrip("#"),
                                   "riotUserName": name.strip(),
                                   "seasonId": season, "role": role_id})
    out = []
    for entry in data.get("fetchPlayerStatistics") or []:
        for row in entry.get("basicChampionPerformances") or []:
            if row.get("totalMatches"):
                out.append(row)
    return out


def matches(region: str, name: str, tag: str, pages: int = 3) -> list:
    """Последние матчи (по 20 на страницу) с составами команд."""
    rows: list[dict] = []
    for page in range(1, max(1, pages) + 1):
        data = _gql(Q_MATCHES, {"regionId": norm_region(region),
                                "riotTagLine": tag.strip().lstrip("#"),
                                "riotUserName": name.strip(), "page": page})
        pack = data.get("fetchPlayerMatchSummaries") or {}
        got = pack.get("matchSummaries") or []
        rows.extend(got)
        # пустая страница или неполная (меньше 20) — история кончилась
        if len(got) < 20:
            break
    return rows


def aggregate_losses(rows: list) -> dict:
    """Против кого игрок проигрывает, с разбивкой по чемпиону и роли.

    Возвращает {(my_champ, ugg_role, enemy_champ_id): {"games": n, "losses": n}}.
    Берём врага в той же роли (direct lane matchup) — именно он решает
    линию. Роль 7 (любая/неизвестная) идёт отдельной строкой.
    """
    agg: dict[tuple[int, int, int], dict] = {}
    for m in rows:
        won = bool(m.get("win"))
        my_champ = m.get("championId") or 0
        my_role = m.get("role") or 7
        my_team = None
        for team in ("teamA", "teamB"):
            for p in m.get(team) or []:
                if p.get("championId") == my_champ and \
                        p.get("role") == my_role:
                    my_team = p.get("teamId")
        if my_team is None:
            continue
        for team in ("teamA", "teamB"):
            for p in m.get(team) or []:
                if p.get("teamId") == my_team:
                    continue
                if (p.get("role") or 7) != my_role:
                    continue
                ch = p.get("championId")
                if not ch:
                    continue
                slot = agg.setdefault((my_champ, my_role, ch),
                                      {"games": 0, "losses": 0})
                slot["games"] += 1
                if not won:
                    slot["losses"] += 1
    return agg


def personal_items(rows: list) -> dict:
    """Что игрок реально собирает: {champ_id: {"role": [(item, count)]}}.

    Считаем только победные сборки — в проигранном мог собрать что угодно
    от безысходности. role = число u.gg, приводится вызывающим кодом.
    """
    agg: dict[int, dict[int, dict[int, int]]] = {}
    for m in rows:
        if not m.get("win"):
            continue
        champ = m.get("championId")
        role = m.get("role")
        if not champ:
            continue
        by_role = agg.setdefault(champ, {})
        counts = by_role.setdefault(role, {})
        for item in m.get("items") or []:
            if item:
                counts[item] = counts.get(item, 0) + 1
    out = {}
    for champ, by_role in agg.items():
        best_role = max(by_role.items(), key=lambda kv: sum(kv[1].values()))
        total = sum(best_role[1].values()) or 1
        out[champ] = {"role": best_role[0],
                      "items": sorted(best_role[1].items(),
                                      key=lambda kv: -kv[1])[:6],
                      "games": total}
    return out
