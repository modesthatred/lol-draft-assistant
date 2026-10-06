"""Парсер данных драфта с OP.GG.

ЧТО РАБОТАЕТ, а что нет (проверено живыми запросами, не догадками):

  MetaSRC / LeagueOfGraphs / Mobalytics  -> 403 Cloudflare, бот-блокировка
  u.gg                                   -> отдаёт пустую страницу,
                                            таблицы грузит скриптом
  OP.GG /lol/champions/{c}/builds        -> ЯВЛЯЕТСЯ ЗАГЛУШКОЙ: таблица
                                            матчапов одинакова для всех
                                            чемпионов (Yone/Garen/Jinx дали
                                            побайтово одинаковый результат).
                                            Не используем.
  OP.GG /lol/champions/{c}/counters/{role}  -> настоящие контрпики, роль-aware
  OP.GG /lol/champions/{c}/synergies/{role} -> настоящая синергия, роль-aware

Роль задаётся сегментом пути: /counters/mid. Query-параметры (?role=) сайт
игнорирует — на это ушла первая версия парсера.

Слаги чемпионов у OP.GG нерегулярные и не выводятся из имени по общему
правилу: Lee Sin -> "leesin", а не "lee-sin" (со "lee-sin" сайт отдаёт
пустую страницу). Поэтому перебираем варианты и проверяем результат
фактическим разбором, а не догадкой.
"""
from __future__ import annotations

import html as htmlmod
import re
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

COUNTER_URL = "https://op.gg/lol/champions/{champ}/counters/{role}"
SYNERGY_URL = "https://op.gg/lol/champions/{champ}/synergies/{role}"
ITEMS_URL = "https://op.gg/lol/champions/{champ}/items/{role}"

# Страница считается разобранной, если нашлось столько матчапов
MIN_COUNTERS = 10
MIN_SYNERGIES = 15

_PCT = re.compile(r"(\d{1,2}\.\d{1,2})\s*%")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


class SourceError(RuntimeError):
    """OP.GG вернул неожиданный ответ: смена вёрстки, блокировка, таймаут."""


def _fetch(url: str, timeout: int = 35, retries: int = 2) -> str:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "close",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise SourceError(f"{url}: {type(last).__name__}: {last}")


def _text(s: str) -> str:
    s = _SCRIPT_RE.sub(" ", s)
    return htmlmod.unescape(_TAG_RE.sub(" ", s))


def _flat(s: str) -> str:
    return re.sub(r"\s+", " ", _text(s))


def slug_candidates(name: str) -> list[str]:
    """Упорядоченные варианты слага для перебора. Реальный слаг не всегда
    совпадает с первым — проверяем разбором."""
    base = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    if not base:
        return []
    parts = base.split()
    out = ["-".join(parts), "".join(parts), " ".join(parts)]
    if len(parts) > 1:
        out.append("-".join(parts[1:]))
    seen: list[str] = []
    for s in out:
        if s and s not in seen:
            seen.append(s)
    return seen


def _pair_rows(tail: str, minimum_games: int) -> dict[str, dict]:
    """Список вида 'Yasuo 53.00% 8,283 Viktor 50.10% 6,712 ...' -> dict."""
    pattern = re.compile(
        r"(?:^|\s)([A-Za-z][A-Za-z'.\s]{1,20}?)\s+"
        r"(\d{1,2}\.\d{1,2})\s*%\s+([\d,]{3,})")
    out: dict[str, dict] = {}
    for nm, wr, games in pattern.findall(tail):
        nm = nm.strip()
        if not nm:
            continue
        try:
            value = float(wr)
            g = int(games.replace(",", ""))
        except ValueError:
            continue
        if g < minimum_games:
            continue
        out[nm] = {"win_rate": value, "games": g}
    return out


def _counter_page(slug: str, role: str, name: str):
    page = _fetch(COUNTER_URL.format(champ=slug, role=role))
    flat = _flat(page)

    summary: dict = {}
    head = re.search(
        rf"{re.escape(name)}\s+Counters\s+for\s+[A-Za-z]+.*?"
        rf"Win rate\s+(\d{{1,2}}\.\d{{1,2}})\s*%\s*"
        rf"Pick rate\s+(\d{{1,2}}\.\d{{1,2}})\s*%\s*"
        rf"Ban rate\s+(\d{{1,2}}\.\d{{1,2}})\s*%", flat, re.IGNORECASE)
    if head:
        summary = {"win_rate": float(head.group(1)),
                   "pick_rate": float(head.group(2)),
                   "ban_rate": float(head.group(3))}

    marker = flat.find("Search a champion")
    if marker < 0:
        raise SourceError("нет блока 'Search a champion'")
    rows = _pair_rows(flat[marker + len("Search a champion"):], 100)
    return rows, summary


def _synergy_page(slug: str, role: str):
    page = _fetch(SYNERGY_URL.format(champ=slug, role=role))
    flat = _flat(page)
    rows: dict[str, dict] = {}
    for r in _tag_rows(page)[1:]:
        if len(r) < 3:
            continue
        nm = r[0].strip()
        rates = [float(x) for c in r[1:] for x in _PCT.findall(c)]
        if not nm or not rates:
            continue
        m = re.search(r"([\d,]+)\s*$", r[1] or "")
        rows[nm] = {"win_rate": rates[-1],
                    "games": int(m.group(1).replace(",", "")) if m else None}
    return rows


_TAG_RE_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)


def _tag_rows(page: str) -> list[list[str]]:
    rows = []
    for raw_row in _TAG_RE_ROW.findall(page):
        cells = [_text(c).strip() for c in _CELL_RE.findall(raw_row)]
        if any(cells):
            rows.append(cells)
    return rows


def fetch_counters(name: str, role: str) -> tuple[dict[int, dict], dict, str]:
    """Контрпики чемпиона на роли.

    Возвращает (matchups, summary, used_slug), где
      matchups — {champion_id: {"win_rate", "games"}},
      summary  — метрики самого чемпиона: win_rate / pick_rate / ban_rate.
    Имённый резолвинг делает вызывающий код (у него есть справочник id).
    """
    last_err = "неизвестно"
    for slug in slug_candidates(name):
        try:
            rows, summary = _counter_page(slug, role, name)
        except SourceError as e:
            last_err = str(e)
            continue
        if len(rows) >= MIN_COUNTERS:
            return rows, summary, slug
        last_err = f"разобрано {len(rows)} матчапов (нужно {MIN_COUNTERS})"
    raise SourceError(f"{name}/{role}: {last_err}")


# ---------------- предметы ----------------

# id предмета лежит прямо в URL иконки; там же версия патча
_ITEM_ICON_RE = re.compile(r"/meta/images/lol/(\d+\.\d+\.\d+)/item/(\d+)\.png")
_ITEM_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_ITEM_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ITEM_ID_IN_TAG_RE = re.compile(r"/item/(\d+)\.png")
_ALT_IN_TAG_RE = re.compile(r"\balt=\"([^\"]*)\"")
# у обувных строк картинка без alt, название лежит рядом в <strong class="ml-2…">
_ITEM_NAME_STRONG_RE = re.compile(
    r'<strong class="ml-2[^"]*"[^>]*>(.*?)</strong>', re.S | re.I)
_GAMES_RE = re.compile(r"([\d,]{3,})\s*(?:<!--[^>]*>\s*)*Games", re.I)
_PATCH_ANY_RE = re.compile(r"/meta/images/lol/(\d+\.\d+\.\d+)/")

# Заголовки секций на странице предметов. Ключ — что нам нужно.
_ITEM_SECTIONS = (
    ("core", "Core builds"),
    ("boots", "Boots"),
    ("starter", "Starting Item"),
    ("final", "Final items"),
)
MIN_ITEMS = 2


def _int(v: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _row_items(raw: str) -> list[tuple[int, str]]:
    """Предметы строки в порядке отображения: [(item_id, name), ...].

    Название берём из alt самой картинки. У обувных строк alt пустой, там
    подставка из соседнего <strong>.
    """
    pairs: list[tuple[int, str]] = []
    seen: set[int] = set()
    for tag in _ITEM_TAG_RE.findall(raw):
        m = _ITEM_ID_IN_TAG_RE.search(tag)
        if not m:
            continue
        iid = _int(m.group(1))
        if not iid or iid in seen:
            continue
        seen.add(iid)
        alt = _ALT_IN_TAG_RE.search(tag)
        name = htmlmod.unescape(alt.group(1)).strip() if alt else ""
        pairs.append((iid, name))
    if any(not n for _, n in pairs):
        fallback = [_text(x).strip() for x in
                    _ITEM_NAME_STRONG_RE.findall(raw)]
        fallback = [htmlmod.unescape(x) for x in fallback if x]
        if fallback:
            pairs = [(i, n or fallback[k] if k < len(fallback) else n)
                     for k, (i, n) in enumerate(pairs)]
    return pairs


def _parse_item_rows(blob: str) -> list[dict]:
    """Строки <tr> секции -> [{items: [(id, name)…], pick_rate, games, win_rate}]."""
    out: list[dict] = []
    for raw in _ITEM_ROW_RE.findall(blob):
        pairs = _row_items(raw)
        if not pairs:
            continue
        pick = games = win = 0.0
        # в строке числа идут так: pick_rate, games, win_rate
        pcts = [float(x) for x in re.findall(r"(\d{1,2}\.\d{1,2})\s*%", raw)]
        g = _GAMES_RE.search(raw)
        if g:
            try:
                games = float(g.group(1).replace(",", ""))
            except ValueError:
                games = 0.0
        if len(pcts) >= 2:
            pick, win = pcts[0], pcts[-1]
        elif pcts:
            win = pcts[0]
        out.append({"items": [i for i, _ in pairs],
                    "names": [n for _, n in pairs],
                    "pick_rate": pick, "games": games, "win_rate": win})
    return out


def recommended_path(rows: list[dict], limit: int = 3) -> list[dict]:
    """Жадная сборка порядка покупок из строк «два предмета».

    OP.GG отдаёт готовые связки (A + B) с числом игр, а не список покупок,
    поэтому строим его сами: первым берём предмет из самых популярных
    связок, затем второй — из связок, где уже есть первый, и так далее.
    Если связок с выбранным набором нет, берём из общей массы, иначе путь
    обрывается на двух предметах.
    """
    if not rows:
        return []
    chosen: list[int] = []
    # счётчик игр — основной признак популярности. Если OP.GG его не отдал
    # (а разметка меняется), сортируемся по винрейту: лучше спорный порядок,
    # чем пустой билд.
    def weight(row: dict) -> float:
        return row["games"] or row["win_rate"]

    while len(chosen) < limit:
        if chosen:
            fitting = [r for r in rows
                       if all(i in r["items"] for i in chosen)]
            fresh = {i for r in fitting for i in r["items"]
                     if i not in chosen}
            # связки с выбранным набором есть, но все предметы из них уже
            # взяты — тогда продолжать не из чего и берём из общей массы
            pool = fitting if fresh else rows
        else:
            pool = rows
        best_id, best_score = 0, -1.0
        for row in pool:
            for iid in row["items"]:
                if iid in chosen:
                    continue
                score = weight(row)
                if score > best_score:
                    best_id, best_score = iid, score
        if not best_id or best_score <= 0:
            break
        chosen.append(best_id)

    out: list[dict] = []
    for iid in chosen:
        # метрики берём из самой популярной связки, где предмет встречается
        best = None
        for row in rows:
            if iid in row["items"] and (best is None or
                                        row["games"] > best["games"]):
                best = row
        if best is None:
            continue
        name = ""
        try:
            name = best["names"][best["items"].index(iid)]
        except (IndexError, ValueError):
            pass
        out.append({"item_id": iid, "name": name,
                    "win_rate": best["win_rate"],
                    "pick_rate": best["pick_rate"], "games": best["games"]})
    return out


def fetch_items(name: str, role: str) -> tuple[dict, str]:
    """Билд чемпиона на роли.

    Возвращает ({patch, path, boots, core_rows}, used_slug), где path —
    порядок первых трёх покупок [{item_id, name, win_rate, ...}].
    """
    last_err = "неизвестно"
    for slug in slug_candidates(name):
        try:
            page = _fetch(ITEMS_URL.format(champ=slug, role=role))
        except SourceError as e:
            last_err = str(e)
            continue

        m = _PATCH_ANY_RE.search(page)
        patch = m.group(1) if m else ""

        # режем страницу на секции по их заголовкам
        bounds: list[tuple[int, str]] = []
        for key, header in _ITEM_SECTIONS:
            i = page.find(header)
            if i >= 0:
                bounds.append((i, key))
        bounds.sort()
        sections: dict[str, str] = {}
        for n, (start, key) in enumerate(bounds):
            end = bounds[n + 1][0] if n + 1 < len(bounds) else len(page)
            sections[key] = page[start:end]

        rows = _parse_item_rows(sections.get("core", ""))
        boots = _parse_item_rows(sections.get("boots", ""))
        path = recommended_path(rows, limit=3)
        if len(path) < MIN_ITEMS:
            last_err = f"разобрано {len(path)} предметов (нужно {MIN_ITEMS})"
            continue
        return ({"patch": patch, "path": path,
                 "boots": [{"item_id": b["items"][0],
                            "name": (b["names"][0] if b["names"] else ""),
                            "win_rate": b["win_rate"],
                            "pick_rate": b["pick_rate"]}
                           for b in boots[:3]],
                 "core_rows": len(rows)}, slug)
    raise SourceError(f"{name}/{role}: {last_err}")


def fetch_synergies(name: str, role: str) -> tuple[dict[str, dict], str]:
    """Синергия с союзниками на роли. Возвращает (rows, used_slug)."""
    last_err = "неизвестно"
    for slug in slug_candidates(name):
        try:
            rows = _synergy_page(slug, role)
        except SourceError as e:
            last_err = str(e)
            continue
        if len(rows) >= MIN_SYNERGIES:
            return rows, slug
        last_err = f"разобрано {len(rows)} синергий (нужно {MIN_SYNERGIES})"
    raise SourceError(f"{name}/{role}: {last_err}")
