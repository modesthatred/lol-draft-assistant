"""Журнал обращений к клиенту и разбор драфта.

Зачем: когда что-то идёт не так в живом драфте, драфт уже кончился, а по
логу ошибок видно только «AttributeError». А нужны две вещи:

* last_session.json — сырой ответ клиента на последний /lol-champ-select,
  чтобы понять, что он вообще прислал (и прислал ли);
* draft_trace.jsonl — построчно: что разобрали, что показали и почему.

Пишем в data/ рядом с программой. Ограничение по размеру есть, и любая
ошибка записи молча игнорируется: диагностика не должна ронять приложение
посреди драфта.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .settings import APP_DIR

log = logging.getLogger("draft.trace")

SESSION_FILE = APP_DIR / "last_session.json"
TRACE_FILE = APP_DIR / "draft_trace.jsonl"

# Больше 2 МБ журнала переписываем: за драфт набирается десятки строк, а
# файл должен переживать перезагрузки и не расти без границ.
MAX_TRACE_BYTES = 2 * 1024 * 1024


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:                               # noqa: BLE001
        log.debug("trace write failed", exc_info=True)
        return None


def save_session(session: dict) -> None:
    """Сырой се��ион последнего чтения.

    Пишем всегда, а не только при ошибке: состав на момент жалобы уже не
    восстановить, а файл — единственное, что остаётся.
    """
    def _write():
        tmp = SESSION_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(session, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(SESSION_FILE)

    _safe(_write)


def _rotate_if_needed() -> None:
    try:
        if TRACE_FILE.stat().st_size < MAX_TRACE_BYTES:
            return
    except OSError:
        return
    try:
        # оставляем вторую половину — начало драфта в ней ещё есть
        data = TRACE_FILE.read_text(encoding="utf-8",
                                    errors="replace").splitlines()
        TRACE_FILE.write_text("\n".join(data[len(data) // 2:]) + "\n",
                              encoding="utf-8")
    except OSError:
        pass


def event(kind: str, **fields) -> None:
    """Одна строка в журнал: время, вид события, поля."""
    def _write():
        _rotate_if_needed()
        row = {"t": time.strftime("%H:%M:%S"), "kind": kind}
        row.update(fields)
        with TRACE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    _safe(_write)


def draft_snapshot(draft, champs: dict) -> dict:
    """Компактное состояние разбора — то, на что стоит смотреть в жалобах.

    Здесь видно все четыре класса ошибок сразу: чей это ход, какие слоты
    заполнены (и кто из них пре-пик), что попало в баны, и что вообще видно
    из врагов.
    """
    if draft is None:
        return {"state": None}
    return {
        "state": "ok",
        "phase": draft.phase,
        "action": draft.action_type,
        "my_turn": draft.is_my_turn,
        "my_cell": draft.my_cell,
        "my_role": draft.my_role,
        "ban_phase": draft.is_ban_phase,
        "ally_slots": [[s.cell, s.cid, s.role, s.locked, s.is_prepick]
                       for s in draft.ally_slots],
        "enemy_slots": [[s.cell, s.cid, s.role, s.locked, s.is_prepick]
                        for s in draft.enemy_slots],
        "ally_bans": list(draft.ally_bans),
        "enemy_bans": list(draft.enemy_bans),
        "ally_prepicks": list(draft.ally_prepicks),
        "enemy_hovered": draft.enemy_hovered,
        "hovered": draft.hovered_champion,
        "my_champion": draft.my_champion,
        "my_prepick": draft.my_prepick,
    }


def describe(cid: int, champs: dict) -> str:
    c = champs.get(cid)
    return getattr(c, "name", "") or f"#{cid}"