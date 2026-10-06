"""Настройки помощника: файл конфига + пути приложения."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _build_root() -> Path:
    """Корень сборки — тот же, что и у лежащего рядом .exe.

    Данные проекта живут только здесь, в подпапке data: никакого AppData.
    Иначе перенос папки на другой диск оставлял бы за собой конфиг, базу и
    лог в профиле, а половина настроек молча терялась бы.
    """
    if getattr(sys, "frozen", False):
        # dist/DraftAssistant.exe -> корень сборки на уровень выше
        return Path(sys.executable).resolve().parent.parent
    override = os.environ.get("DRAFT_ASSISTANT_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent


APP_DIR = _build_root() / "data"
DEFAULT_CONFIG = APP_DIR / "config.json"
DEFAULT_DB = APP_DIR / "draft.db"
DEFAULT_ICONS = APP_DIR / "icons"

DEFAULTS = {
    # пул пустой намеренно: чужие мейны придумывать нельзя, пользователь
    # выбирает их в окне настроек (⚙) при первом запуске
    "pool": [],
    # пулы по ролям: {"jungle": ["Shen", ...], "support": [...]}.
    # Нужен потому, что в драфте тебя могут поставить на любую роль, а
    # мейны и правильный счёт матчапа у каждой роли свои.
    "pools": {},
    "role": "mid",
    # роль берём из драфта (клиент знает, кем тебя поставили); role выше
    # остаётся ручным запасным вариантом, если клиент роль не отдал
    "auto_role": True,
    "hotkey": "F8",
    "window": {"width": 300, "height": 96, "x": None, "y": None,
               "opacity": 0.94},
    "weights": {
        "counter": 0.55,      # вклад матчапа против врагов
        "synergy": 0.30,      # вклад синергии с союзниками
        "comp": 0.15,         # вклад оценки состава команды
        "worst_case": 0.35    # внутри counter: доля худшего матчапа
    },
    "show": {"icons": True, "comp_notes": True, "always_on_top": True},
    "items": {"enabled": True, "show_count": 3, "show_boots": True},
    "sync": {"max_age_hours": 24, "synergy_roles": [],
             "patch_check_hours": 6,
             # Блок «вне пула» (топ пиков помимо мейнов) строится по данным
             # ВСЕХ чемпионов активной роли. Это осознанно отдельный проход
             # синка: он дольше пула, поэтому обновляется реже, а не при
             # каждом запуске.
             "all_champions": True,
             "all_champions_ttl_hours": 168},
}

VALID_ROLES = ("top", "jungle", "mid", "adc", "support")
ALL_ROLES = VALID_ROLES


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    data: dict = field(default_factory=dict)
    # куда этот конфиг реально читался — save() обязан писать туда же,
    # иначе --config my.json сохранял бы поверх DEFAULT_CONFIG
    path: Path | None = None

    # --- доступ ---
    @property
    def pool(self) -> list[str]:
        """Пул ручной роли. Для активной роли бей pool_for()."""
        return list(self.pool_for(self.role))

    @property
    def pools(self) -> dict[str, list[str]]:
        """Пулы по ролям: {"jungle": ["Shen"], "support": ["Lulu"]}.

        Старый плоский `pool` приводим к pools[role] — иначе после перехода
        на роли прежние мейны молча пропали бы из расчёта.
        """
        raw = self.data.get("pools")
        out: dict[str, list[str]] = {}
        if isinstance(raw, dict):
            for r, names in raw.items():
                if r in VALID_ROLES and isinstance(names, list):
                    clean = [str(n) for n in names if str(n).strip()]
                    if clean:
                        out[r] = clean
        if out:
            return out
        legacy = [str(n) for n in (self.data.get("pool") or [])
                  if str(n).strip()]
        return {self.role: legacy} if legacy else {}

    def pool_for(self, role: str) -> list[str]:
        """Мейны роли. Для неизвестной/пустой роли берём ручную, иначе в
        драфте на незнакомой позиции показывать было бы нечего."""
        pools = self.pools
        names = pools.get(role) or []
        if names or role == self.role:
            return list(names)
        return list(pools.get(self.role) or [])

    def pool_roles(self) -> list[str]:
        """Роли, для которых есть хотя бы один мейн — по ним и синкаем."""
        return [r for r in VALID_ROLES if self.pools.get(r)]

    @property
    def auto_role(self) -> bool:
        return bool(self.data.get("auto_role", True))

    def active_role(self, detected: str = "") -> str:
        """Роль, по которой считаем: из драфта, иначе ручная."""
        if self.auto_role and detected in VALID_ROLES:
            return detected
        return self.role

    def set_pools(self, pools: dict[str, list[str]]) -> None:
        clean = {r: [str(n) for n in (names or []) if str(n).strip()]
                 for r, names in (pools or {}).items()
                 if r in VALID_ROLES}
        clean = {r: n for r, n in clean.items() if n}
        self.data["pools"] = clean
        # плоский pool больше не источник правды, но держим его в синхроне:
        # иначе старые версии программы и внешние скрипты читали бы не то
        primary = clean.get(self.role) or next(iter(clean.values()), [])
        self.data["pool"] = list(primary)

    @property
    def role(self) -> str:
        r = str(self.data.get("role", "mid")).lower()
        return r if r in VALID_ROLES else "mid"

    @property
    def hotkey(self) -> str:
        return str(self.data.get("hotkey") or "F8")

    def _live(self, key: str) -> dict:
        """Живой словарь раздела конфига (не копия) — иначе правки
        вложенных значений из UI (позиция окна) молча теряются."""
        v = self.data.get(key)
        if not isinstance(v, dict):
            v = {}
            self.data[key] = v
        return v

    @property
    def weights(self) -> dict:
        return self._live("weights")

    @property
    def window(self) -> dict:
        return self._live("window")

    @property
    def show(self) -> dict:
        return self._live("show")

    @property
    def items(self) -> dict:
        return self._live("items")

    @property
    def items_count(self) -> int:
        """Сколько первых предметов показывать."""
        try:
            n = int(self.items.get("show_count", 3))
        except (TypeError, ValueError):
            n = 3
        return max(1, min(n, 6))

    @property
    def sync(self) -> dict:
        return self._live("sync")

    def save(self) -> Path:
        target = self.path or DEFAULT_CONFIG
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return target


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG
    raw = {}
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    cfg = Config(_deep_merge(DEFAULTS, raw), path=p)
    if not path and not p.is_file():
        # первый запуск: создаём редактируемый конфиг, чтобы пользователю
        # не пришлось искать его вручную
        try:
            cfg.save()
        except OSError:
            pass
    return cfg


def ensure_dirs() -> None:
    for d in (APP_DIR, DEFAULT_ICONS):
        d.mkdir(parents=True, exist_ok=True)
