"""Метаданные чемпионов: id, имена, теги, тип урона, иконки.

ИСТОЧНИК ИДЕТАЛЬНОСТИ — Data Dragon (официальный Riot CDN):
    /cdn/{version}/data/en_US/champion.json
в нём объект data ключуется числовым championId ("266" = Yone) — это ровно то
же самое значение, которое отдаёт LCU в драфте (pickId/championId), поэтому
сопоставление с экрана клиента получается точным.

Чего делать НЕЛЬЗЯ: брать id из CommunityDragon champion-summary.json. Там
другая внутренняя нумерация (Yone=777, MasterYi=60011, Darius=122), и она не
совпадает с Riot. Мы это проверяли — такие id нельзя использовать ни для
матчинга с LCU, ни для иконок.

CommunityDragon оставлен только как резервный источник имён, если ddragon
недоступен.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

DDRAGON_VERSIONS = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_CHAMPIONS = ("https://ddragon.leagueoflegends.com/cdn/{v}/data/en_US/"
                     "champion.json")
CDRAGON_SUMMARY = ("https://raw.communitydragon.org/latest/plugins/"
                   "rcp-be-lol-game-data/global/default/v1/champion-summary.json")

ROLES = ("top", "jungle", "mid", "adc", "support")

# Написание тегов в ddragon -> короткое имя для конфига и логики
TAG_ROLES = {"Fighter": "fighter", "Tank": "tank", "Mage": "mage",
             "Assassin": "assassin", "Support": "support",
             "Marksman": "marksman"}


@dataclass
class Champion:
    cid: int                       # Riot championId — совпадает с LCU
    name: str
    title: str = ""
    tags: tuple[str, ...] = ()
    damage: str = ""
    icon: str = ""                 # URL квадратной иконки
    roles: tuple[str, ...] = ()    # позиции, если удалось получить

    @property
    def is_tank(self) -> bool:
        return "Tank" in self.tags

    @property
    def is_support(self) -> bool:
        return "Support" in self.tags

    @property
    def is_mage(self) -> bool:
        return "Mage" in self.tags

    @property
    def damage_class(self) -> str:
        """'AD' или 'AP' — для оценки состава команды."""
        d = (self.damage or "").lower()
        if d.startswith("phys"):
            return "AD"
        if d.startswith("mag"):
            return "AP"
        return "AP" if self.is_mage else "AD"

    def to_row(self) -> dict:
        return {"cid": self.cid, "name": self.name, "title": self.title,
                "tags": list(self.tags), "damage": self.damage,
                "icon": self.icon, "roles": list(self.roles)}


def _fetch_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def normalize_name(raw: str) -> str:
    """'Dr. Mundo' / 'Dr Mundo' / 'drmundo' -> 'drmundo'."""
    return re.sub(r"[^a-z0-9]", "", (raw or "").lower())


def _slug(name: str) -> str:
    """Имя для URL OP.GG: 'Dr. Mundo' -> 'dr-mundo', 'Master Yi' -> 'master-yi'."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip().lower())
    return re.sub(r"-+", "-", s).strip("-")


def current_patch() -> str:
    """Номер текущего патча, например "16.19.1". Пусто, если сеть недоступна."""
    try:
        return str(_fetch_json(DDRAGON_VERSIONS)[0])
    except Exception:                              # noqa: BLE001
        return ""


def _snapshot_path():
    """Локальный снимок справочника рядом с данными приложения."""
    from .settings import APP_DIR

    return APP_DIR / "champions.json"


def _save_snapshot(out: dict, version: str) -> None:
    """Кладём справочник на диск, чтобы следующий старт был офлайн.

    Список чемпионов меняется раз в патч, а load_champions() дёргается при
    каждом запуске и в каждом фоновом воркере. Без снимка это два сетевых
    запроса на старте, и — что хуже — фоновый запрос продолжается, когда
    главный поток уже гасит Tk.
    """
    try:
        path = _snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": version,
                   "champions": [{"cid": c.cid, "name": c.name,
                                  "title": c.title, "tags": list(c.tags),
                                  "damage": c.damage, "icon": c.icon}
                                 for c in out.values()]}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)
    except Exception:                               # noqa: BLE001
        pass


def _load_snapshot():
    """Читаем снимок. None — снимка нет или он битый."""
    try:
        raw = json.loads(_snapshot_path().read_text(encoding="utf-8"))
        out = {}
        for c in raw.get("champions", []):
            cid = int(c.get("cid", 0))
            if cid <= 0:
                continue
            out[cid] = Champion(cid=cid, name=c.get("name", ""),
                                title=c.get("title", ""),
                                tags=tuple(c.get("tags") or ()),
                                damage=c.get("damage", ""),
                                icon=c.get("icon", ""))
        return (out, raw.get("version", "")), bool(out)
    except Exception:                               # noqa: BLE001
        return ({}, ""), False


def load_champions(allow_network: bool = True) -> tuple[dict[int, Champion], str]:
    """Справочник чемпионов: {championId: Champion}. Второе значение — версия
    патча, она попадает в URL иконок.

    Порядок источников: локальный снимок -> сеть. Снимок экономит два
    запроса на старте и оставляет приложение рабочим без интернета.
    """
    snap, ok = _load_snapshot()
    if ok:
        return snap
    if not allow_network:
        return {}, ""

    try:
        version = _fetch_json(DDRAGON_VERSIONS)[0]
        data = _fetch_json(DDRAGON_CHAMPIONS.format(v=version)).get("data", {})
    except Exception:
        version, data = "", ""

    out: dict[int, Champion] = {}
    # ВАЖНО: data ключуется именем ("Yone"), а числовой championId лежит
    # в поле "key" ("266"). Брать int() из ключа слова нельзя.
    for c in data.values():
        try:
            cid = int(c.get("key", 0))
        except (TypeError, ValueError):
            continue
        if not cid or cid <= 0:
            continue
        info = c.get("info") or {}
        out[cid] = Champion(
            cid=cid,
            name=c.get("name", ""),
            title=c.get("title", ""),
            tags=tuple(c.get("tags") or ()),
            damage=info.get("damage", ""),
            icon=(f"https://ddragon.leagueoflegends.com/cdn/{version}/img/"
                  f"champion/{c.get('image', {}).get('full', '')}"),
        )

    if not out:                      # резерв: ddragon недоступен
        version = ""
        try:
            for e in _fetch_json(CDRAGON_SUMMARY):
                cid = e.get("id")
                if isinstance(cid, int) and cid > 0:
                    out[cid] = Champion(cid=cid, name=e.get("name", ""))
        except Exception:
            pass
    if out:
        _save_snapshot(out, version)
    return out, version


def build_index(champs: dict[int, Champion]) -> dict[str, int]:
    """Поиск по разным написаниям имени -> championId."""
    idx: dict[str, int] = {}
    for c in champs.values():
        for variant in (c.name, normalize_name(c.name), _slug(c.name)):
            if variant:
                idx.setdefault(str(variant).lower(), c.cid)
        idx.setdefault(str(c.cid), c.cid)
    return idx


def resolve(text: str, index: dict[str, int]) -> int | None:
    """'yone' / 'Yone' / 'Dr. Mundo' / '266' -> championId."""
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return int(raw)
    for variant in (raw, raw.lower(), normalize_name(raw), _slug(raw)):
        if variant and variant.lower() in index:
            return index[variant.lower()]
    return None


def slug_for(champ: Champion) -> str:
    return _slug(champ.name)