"""Кэш данных драфта в SQLite.

Логика: во время драфта сеть не трогаем — всё считается из локальной базы,
поэтому отклик ограничен только скоростью LCU (десятки миллисекунд).
Обновление — раз в сутки, отдельной командой или при запуске, если база пустая.

Таблицы:
  champions — справочник id -> имя/иконка (на случай если сеть лежит)
  matchups  — (main, opponent, win_rate, pick_rate, ban_rate)
  synergy   — (main, partner, win_rate, games, role)
  meta      — время последнего обновления по каждому мейну
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS matchups (
    main_id INTEGER NOT NULL,
    opp_id  INTEGER NOT NULL,
    win_rate REAL,
    pick_rate REAL,
    ban_rate REAL,
    PRIMARY KEY (main_id, opp_id)
);
CREATE TABLE IF NOT EXISTS synergy (
    main_id INTEGER NOT NULL,
    partner_id INTEGER NOT NULL,
    win_rate REAL NOT NULL,
    games INTEGER,
    role TEXT NOT NULL,
    PRIMARY KEY (main_id, partner_id, role)
);
CREATE TABLE IF NOT EXISTS meta (
    main_id INTEGER PRIMARY KEY,
    updated_at REAL NOT NULL,
    role TEXT
);
CREATE TABLE IF NOT EXISTS champions (
    cid INTEGER PRIMARY KEY,
    name TEXT,
    icon TEXT,
    tags TEXT,
    damage TEXT
);
CREATE TABLE IF NOT EXISTS champ_meta (
    cid INTEGER NOT NULL,
    role TEXT NOT NULL,
    win_rate REAL,
    pick_rate REAL,
    ban_rate REAL,
    PRIMARY KEY (cid, role)
);
CREATE TABLE IF NOT EXISTS slugs (
    cid INTEGER PRIMARY KEY,
    slug TEXT,
    counter_role TEXT
);
CREATE TABLE IF NOT EXISTS item_builds (
    cid INTEGER NOT NULL,
    role TEXT NOT NULL,
    slot INTEGER NOT NULL,          -- 0 = ботинки, 1..n = порядок покупок
    item_id INTEGER NOT NULL,
    name TEXT DEFAULT '',
    win_rate REAL DEFAULT 0,
    pick_rate REAL DEFAULT 0,
    games REAL DEFAULT 0,
    patch TEXT DEFAULT '',
    updated_at REAL,
    PRIMARY KEY (cid, role, slot)
);
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Cache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # автосинк пишет из фонового потока, а UI читает из главного:
        # WAL даёт параллельные читатели, лок сериализует записи
        self._lock = threading.RLock()
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            pass
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Если таблица slugs создана старой версией (slug NOT NULL),
        пересоздаём её — иначе частичная вставка роли падает."""
        try:
            info = {r["name"]: r for r in
                    self.conn.execute("PRAGMA table_info(slugs)")}
            if info.get("slug") and info["slug"]["notnull"]:
                self.conn.execute("ALTER TABLE slugs RENAME TO slugs_old")
                self.conn.execute(
                    "CREATE TABLE slugs (cid INTEGER PRIMARY KEY, "
                    "slug TEXT, counter_role TEXT)")
                self.conn.execute(
                    "INSERT INTO slugs (cid, slug, counter_role) "
                    "SELECT cid, slug, NULL FROM slugs_old")
                self.conn.execute("DROP TABLE slugs_old")
        except sqlite3.Error:
            pass

    # ---------- чемпионы ----------
    def save_champions(self, champs: dict) -> None:
        rows = [(c.cid, c.name, c.icon, ",".join(c.tags), c.damage)
                for c in champs.values()]
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO champions VALUES (?,?,?,?,?)", rows)
            self.conn.commit()

    def champion(self, cid: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM champions WHERE cid=?",
                              (cid,)).fetchone()
        return dict(r) if r else None

    def champion_by_name(self, name: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM champions WHERE name=? COLLATE NOCASE",
                              (name,)).fetchone()
        return dict(r) if r else None

    # ---------- матчапы ----------
    def save_matchups(self, main_id: int, data: dict[int, dict]) -> int:
        rows = [(main_id, opp, d.get("win_rate"), d.get("pick_rate"),
                 d.get("ban_rate")) for opp, d in data.items()]
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO matchups VALUES (?,?,?,?,?)", rows)
            self.conn.commit()
        return len(rows)

    def matchups(self, main_id: int) -> dict[int, dict]:
        out = {}
        for r in self.conn.execute(
                "SELECT opp_id, win_rate, pick_rate, ban_rate FROM matchups "
                "WHERE main_id=?", (main_id,)):
            out[r["opp_id"]] = {"win_rate": r["win_rate"],
                                "pick_rate": r["pick_rate"],
                                "ban_rate": r["ban_rate"]}
        return out

    def save_champ_meta(self, cid: int, role: str, meta: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO champ_meta VALUES (?,?,?,?,?)",
                (cid, role, meta.get("win_rate"), meta.get("pick_rate"),
                 meta.get("ban_rate")))
            self.conn.commit()

    def champ_meta(self, cid: int, role: str) -> dict:
        r = self.conn.execute(
            "SELECT win_rate, pick_rate, ban_rate FROM champ_meta "
            "WHERE cid=? AND role=?", (cid, role)).fetchone()
        return dict(r) if r else {}

    def set_slug(self, cid: int, slug: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO slugs (cid, slug) VALUES (?,?) "
                "ON CONFLICT(cid) DO UPDATE SET slug=excluded.slug",
                (cid, slug))
            self.conn.commit()

    def set_counter_role(self, cid: int, role: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO slugs (cid, counter_role) VALUES (?,?) "
                "ON CONFLICT(cid) DO UPDATE SET counter_role=excluded.counter_role",
                (cid, role))
            self.conn.commit()

    def counter_role(self, cid: int) -> str | None:
        r = self.conn.execute("SELECT counter_role FROM slugs WHERE cid=?",
                              (cid,)).fetchone()
        return r["counter_role"] if r else None

    def item_roles(self, cid: int) -> list[str]:
        """Роли, для которых у чемпиона есть собранный билд.

        Нужна, когда роль из конфига не совпала с ролью пика: пул собран под
        одну роль, а в драфте встретился чемпион с другой. Молчать тут нельзя —
        билд есть, просто лежит в соседней роли.
        """
        return [r["role"] for r in self.conn.execute(
            "SELECT DISTINCT role FROM item_builds WHERE cid=? ORDER BY role",
            (cid,))]

    def get_slug(self, cid: int) -> str | None:
        r = self.conn.execute("SELECT slug FROM slugs WHERE cid=?",
                              (cid,)).fetchone()
        return r["slug"] if r else None

    def opponent_ban_rates(self) -> dict[int, float]:
        """Сводный бан-рейт по всем мейнам — по нему оцениваем риск бана."""
        out: dict[int, float] = {}
        for r in self.conn.execute(
                "SELECT opp_id, AVG(ban_rate) b FROM matchups "
                "WHERE ban_rate IS NOT NULL GROUP BY opp_id"):
            out[r["opp_id"]] = r["b"] or 0.0
        return out

    # ---------- предметы ----------
    def save_items(self, cid: int, role: str, path: list[dict],
                   boots: list[dict], patch: str = "") -> int:
        """Кладёт порядок покупок и ботинки. slot=0 — ботинки."""
        now = time.time()
        rows = []
        for b in (boots[:1] if boots else []):
            rows.append((cid, role, 0, b.get("item_id", 0),
                         b.get("name", ""), b.get("win_rate", 0.0),
                         b.get("pick_rate", 0.0), 0.0, patch, now))
        for slot, item in enumerate(path, 1):
            rows.append((cid, role, slot, item.get("item_id", 0),
                         item.get("name", ""), item.get("win_rate", 0.0),
                         item.get("pick_rate", 0.0),
                         item.get("games", 0.0), patch, now))
        with self._lock:
            # чистим целиком: новый билд может оказаться короче старого, и
            # без этого старые слоты остались бы в базе навсегда — вместе с
            # протухшим патчем, из-за которого items_stale() врал
            self.conn.execute("DELETE FROM item_builds WHERE cid=? AND role=?",
                              (cid, role))
            self.conn.executemany(
                "INSERT OR REPLACE INTO item_builds "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            self.conn.commit()
        return len(rows)

    def items(self, cid: int, role: str, limit: int = 3) -> list[dict]:
        out = []
        for r in self.conn.execute(
                "SELECT slot, item_id, name, win_rate, pick_rate, games, "
                "patch FROM item_builds WHERE cid=? AND role=? AND slot>0 "
                "ORDER BY slot LIMIT ?", (cid, role, limit)):
            out.append(dict(r))
        return out

    def boots(self, cid: int, role: str) -> dict | None:
        r = self.conn.execute(
            "SELECT item_id, name, win_rate, pick_rate, patch "
            "FROM item_builds WHERE cid=? AND role=? AND slot=0",
            (cid, role)).fetchone()
        return dict(r) if r else None

    def items_patch(self, cid: int, role: str) -> str | None:
        r = self.conn.execute(
            "SELECT patch FROM item_builds WHERE cid=? AND role=? LIMIT 1",
            (cid, role)).fetchone()
        return r["patch"] if r else None

    def has_items(self, cid: int, role: str) -> bool:
        return bool(self.items(cid, role, limit=1))

    def items_stale(self, cid: int, role: str, max_age_h: float,
                    patch: str = "") -> bool:
        """Протурели ли предметы: по времени или из-за смены патча."""
        r = self.conn.execute(
            "SELECT updated_at, patch FROM item_builds "
            "WHERE cid=? AND role=? LIMIT 1", (cid, role)).fetchone()
        if r is None:
            return True
        if (time.time() - (r["updated_at"] or 0)) > max_age_h * 3600:
            return True
        # патч сменился, а предметы из старого — обязаны обновиться
        return bool(patch) and bool(r["patch"]) and r["patch"] != patch

    # ---------- общее состояние (номер патча и пр.) ----------
    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO app_state (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
            self.conn.commit()

    def get_state(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM app_state WHERE key=?",
                              (key,)).fetchone()
        return r["value"] if r else None

    # ---------- расширенный набор чемпионов роли ----------
    # Контрпики всех чемпионов роли уходят в matchups так же, как у мейнов
    # пула, но их «свежесть» держится отдельным маркером: разово качать и
    # перекачивать 100+ страниц вместе с пулом нельзя — сам запуск приложения
    # уходил бы в сеть на десяток минут. Роль-маркер обновляется раз в
    # all_champions_ttl_hours (по умолчанию неделю).
    _ALL_META_PREFIX = "all_meta:"

    def mark_all_meta(self, role: str) -> None:
        self.set_state(self._ALL_META_PREFIX + role, str(time.time()))

    def all_meta_stale(self, role: str, max_age_h: float = 168.0) -> bool:
        raw = self.get_state(self._ALL_META_PREFIX + role)
        if raw is None:
            return True
        try:
            return time.time() - float(raw) > max_age_h * 3600
        except (TypeError, ValueError):
            return True

    # ---------- синергии ----------
    def save_synergies(self, main_id: int, role: str,
                       data: dict[int, dict]) -> int:
        rows = [(main_id, partner, d.get("win_rate"), d.get("games"), role)
                for partner, d in data.items()]
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO synergy VALUES (?,?,?,?,?)", rows)
            self.conn.commit()
        return len(rows)

    def synergies(self, main_id: int, role: str) -> dict[int, dict]:
        out = {}
        for r in self.conn.execute(
                "SELECT partner_id, win_rate, games FROM synergy "
                "WHERE main_id=? AND role=?", (main_id, role)):
            out[r["partner_id"]] = {"win_rate": r["win_rate"],
                                    "games": r["games"]}
        return out

    def has_role(self, role: str) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM synergy WHERE role=? LIMIT 1", (role,)).fetchone())

    # ---------- метаданные обновления ----------
    def mark_updated(self, main_id: int, role: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES (?,?,?)",
                (main_id, time.time(), role))
            self.conn.commit()

    def last_update(self, main_id: int) -> float | None:
        r = self.conn.execute("SELECT updated_at FROM meta WHERE main_id=?",
                              (main_id,)).fetchone()
        return r["updated_at"] if r else None

    def is_stale(self, main_id: int, max_age_h: float = 24.0) -> bool:
        """Устарело, если нет свежей метки ИЛИ роль для контрпиков не подобрана:
        после синка только синергий роль может остаться пустой."""
        ts = self.last_update(main_id)
        if ts is None:
            return True
        if (time.time() - ts) > max_age_h * 3600:
            return True
        return not self.counter_role(main_id)

    def status(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT m.main_id, c.name, m.updated_at, m.role FROM meta m "
            "LEFT JOIN champions c ON c.cid=m.main_id ORDER BY c.name")]

    def clear(self) -> None:
        with self._lock:
            for t in ("matchups", "synergy", "meta"):
                self.conn.execute(f"DELETE FROM {t}")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()
