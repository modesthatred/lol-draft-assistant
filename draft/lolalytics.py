"""Мета-предметы с lolalytics (второй источник).

Зачем второй источник: op.gg отдаёт билд по чемпиону и роли, но без
привязки к текущему патчу и тиру игроков, и иногда выдаёт устаревший
набор. lolalytics показывает билд именно по линии (lane в query), с
разделением «стартовые / core», по конкретному патчу и тиру (Emerald+),
поэтому на Шене в лесу он заметно отличается от топового — ровно та
жалоба, что «предметы берутся независимо от роли».

Разметка страницы (Qwik SSR): секции разделены маркерами вида
`<!--t=xx-->Starting Items<!---->`, а предметы — картинками
`https://cdn5.lolalytics.com/item64/<id>.webp` с alt-названием.
Парсим обе секции по порядку — он и есть порядок покупок.
"""
from __future__ import annotations

import html as htmlmod
import re

from . import net

BUILD_URL = "https://lolalytics.com/lol/{slug}/build/?lane={lane}"
LANES = {"top": "top", "jungle": "jungle", "mid": "middle", "adc": "bottom",
         "support": "support"}

_SECTION_RE = re.compile(r"<!--t=[a-z0-9]+-->([^<]{2,40})<!---->")
_ITEM_RE = re.compile(
    r'item64/(\d+)\.webp.*?alt="([^"]+)"', re.S)
_RUNE_RE = re.compile(
    r'rune68/(\d+)\.webp[^>]*?alt="([^"]+)"', re.S)

# секция рун вынесена отдельно: кеystone — та руна, что показана яркой
# (без grayscale): вокруг неё — серые альтернативы.
_RUNE_IMG = re.compile(
    r'<img srcset="https://cdn5\.lolalytics\.com/rune68/(\d+)\.webp.*?'
    r'alt="([^"]+)"([^>]*)>', re.S)

# Реальные заголовки разделов страницы билда. Между ними попадаются
# проценты винрейта секций (те же qv-маркеры) — по ним резать нельзя,
# иначе Core Build обрывается на первом же числе.
_SECTIONS = ("starting items", "core build", "item 4", "item 5",
             "item 6", "boots", "full build", "sell order",
             "starter alternatives", "situational items")


def fetch_runes(name: str, lane: str, slug_candidates) -> dict:
    """Главная руна (keystone) чемпиона на линии — с билд-страницы.

    В секции Primary Runes первая яркая иконка (без grayscale) — та руна,
    на которой чаще всего играют; остальные — приглушённые альтернативы.
    Берём яркую: «основная одна большая».
    """
    lane_key = LANES.get(lane, lane)
    last_err = "неизвестно"
    for slug in slug_candidates(name):
        try:
            page = net.get_html(BUILD_URL.format(slug=slug, lane=lane_key),
                                timeout=30)
        except net.NetError as e:
            last_err = str(e)
            continue
        i = page.find("Primary Runes")
        if i < 0:
            last_err = "секции рун нет"
            continue
        j = page.find("Secondary", i)
        if j < 0:
            j = i + 4000
        seg = page[i:j]
        for m in _RUNE_IMG.finditer(seg):
            rid, name_r, rest = int(m.group(1)), m.group(2), m.group(3)
            if "grayscale" in rest:
                continue
            return {"rune_id": rid, "name": name_r,
                    "url":
                        f"https://cdn5.lolalytics.com/rune68/{rid}.webp"}
        last_err = "главная руна не разобрана"
    raise LolalyticsError(f"{name}/{lane}: {last_err}")

# имена секций, из которых собираем билд
_WANTED = ("starting items", "core build")


class LolalyticsError(RuntimeError):
    """Страница не отдалась или в ней поменялась разметка."""


def fetch_build(name: str, lane: str, slug_candidates) -> dict:
    """Билд чемпиона на линии: {"patch", "start": [...], "core": [...]}.

    slug_candidates — функция перебора слагов из opgg (слаги у сайтов
    нерегулярные, общий рецепт не работает).
    """
    lane_key = LANES.get(lane, lane)
    last_err = "неизвестно"
    for slug in slug_candidates(name):
        try:
            page = net.get_html(BUILD_URL.format(slug=slug, lane=lane_key),
                                timeout=30)
        except net.NetError as e:
            last_err = str(e)
            continue
        if "Core Build" not in page and "Starting Items" not in page:
            last_err = "нет секций с билдом"
            continue
        patch = ""
        m = re.search(r'lane[^"]*2x,\s*([0-9]+\.[0-9]+)', page)
        if m:
            patch = m.group(1)
        sections = _split_sections(page)
        core = [_clean(pair) for pair in sections.get("core build", [])]
        start = [_clean(pair) for pair in
                 sections.get("starting items", [])]
        core = _dedup(core)
        if not core:
            last_err = "core-предметы не разобраны (вёрстка?)"
            continue
        return {"patch": patch, "start": _dedup(start)[:3],
                "core": core[:6]}
    raise LolalyticsError(f"{name}/{lane}: {last_err}")


def _split_sections(page: str) -> dict[str, list[tuple[int, str]]]:
    """Секции страницы -> {"starting items": [(id, name)], "core build": [...]}."""
    marks = [(m.start(), m.group(1).strip().lower())
             for m in _SECTION_RE.finditer(page)
             if m.group(1).strip().lower() in _SECTIONS]
    out: dict[str, list[tuple[int, str]]] = {}
    for n, (pos, title) in enumerate(marks):
        if title not in _WANTED:
            continue
        end = marks[n + 1][0] if n + 1 < len(marks) else len(page)
        items = []
        for m in _ITEM_RE.finditer(page[pos:end]):
            iid, alt = int(m.group(1)), htmlmod.unescape(m.group(2))
            if iid and iid not in [i for i, _ in items]:
                items.append((iid, alt))
        out[title] = items
    return out


def _clean(pair: tuple[int, str]) -> dict:
    return {"item_id": pair[0], "name": pair[1], "source": "lolalytics"}


def _dedup(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        if it["item_id"] in seen:
            continue
        seen.add(it["item_id"])
        out.append(it)
    return out
