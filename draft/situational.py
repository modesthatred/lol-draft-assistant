"""Ситуативные предметы под состав врага.

Сам билд OP.GG не трогаем: популярный порядок покупки для чемпиона и роли —
это данные по патчу, врать в них мы не должны. А вот подсказку «что взять
против этого конкретного состава» можно сказать раньше любого патча: ты
выбираешь чемпиона, а враги уже пикаются один за другим.

Правила работы:
  * совет появляется только когда состав узнаваем (3+ известных чемпионов),
    иначе это шум «антихила нет, магов нет» на пустом экране;
  * копия каждого совета — переводимая строка (tags: anti_heal и т.п.),
    отрисовка — за оверлеем;
  * списки чемпионов консервативные: лучше промолчать, чем советовать
    лишнее. Наборы ниже проверены на практике, это не вся игра.
"""
from __future__ import annotations

# Лечатся сами (вампиризм/самохил) или лечат союзников — резать антихилом.
HEAL_SINCERE = {
    "Aatrox", "Fiora", "Illaoi", "Lee Sin", "Maokai", "Mundo",
    "Red Kayn", "Renekton", "Soraka", "Sylas", "Vladimir", "Warwick",
    "Zac", "Swain", "Senna", "Nami", "Rakan", "Sona", "Yuumi", "Milio",
}
# Щиты на себя или союзников — резать Serpent's Fang.
SHIELDS = {
    "Lulu", "Karma", "Janna", "Seraphine", "Orianna", "Milio", "Rakan",
    "Braum", "Shen", "Tahm Kench", "Lee Sin", "Riven", "Sett",
}

BOOTS_ARMOR = "Plated Steelcaps"
BOOTS_MR = "Mercury's Treads"
ITEMS_MR = "Wit's End / Maw / Force of Nature"
ITEMS_ARMOR = "Death's Dance / Randuin / Thornmail"
ITEM_ANTIHEAL = "Mortal Reminder / Morellonomicon / Thornmail"
ITEM_ANTISHIELD = "Serpent's Fang"


def situational(enemy_ids, champs) -> list[dict]:
    """Советы под видимых врагов: [{tag, why, item}…] или [].

    enemy_ids — чемпион-ид врагов из драфта (числа, среди них могут быть
    пустые слоты — негативные/нулевые). champs — словарь cid: Champion.
    """
    known = [champs.get(c) for c in enemy_ids]
    known = [c for c in known if c is not None]
    if len(known) < 3:
        return []
    n = len(known)
    ad = sum(1 for c in known if c.damage_class == "AD")
    ap = sum(1 for c in known if c.damage_class == "AP")
    heal = sorted({c.name for c in known if c.name in HEAL_SINCERE})
    shields = sorted({c.name for c in known if c.name in SHIELDS})

    out: list[dict] = []
    if len(heal) >= 2:
        out.append({"tag": "anti_heal", "why": ", ".join(heal),
                    "item": ITEM_ANTIHEAL})
    if len(shields) >= 2:
        out.append({"tag": "anti_shield", "why": ", ".join(shields),
                    "item": ITEM_ANTISHIELD})
    if ap >= 3 and ap > ad:
        out.append({"tag": "vs_ap", "why": f"{ap} из {n}",
                    "item": ITEMS_MR})
    elif ad >= 3 and ad > ap:
        out.append({"tag": "vs_ad", "why": f"{ad} из {n}",
                    "item": ITEMS_ARMOR})
    return out