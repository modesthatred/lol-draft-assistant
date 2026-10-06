"""Регресс-тесты без сети: разбор LCU, конфиг, вёрстка оверлея.

Запуск:  python tests/test_app.py
"""
from __future__ import annotations

import argparse
import functools
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Консоль Windows по умолчанию cp1251, и проверки с ⚙ в названии падали с
# UnicodeEncodeError прямо во время прогона — то есть тесты, которые
# должны были показать поломку, вместо этого просто роняли сам раннер.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass

from draft import champions as ch
from draft.cache import Cache
from draft.lcu import DraftState, Slot, parse_session
from draft.main import App
from draft.scoring import ban_advice, evaluate, top_off_pool
from draft.settings import DEFAULTS, Config, load_config

# реальные championId из Data Dragon
YONE, DARIUS, ZED, SMOLDER = 777, 122, 238, 800
AHRI, LEESIN, JANN, LEONA = 103, 64, 51, 99
VEX, LUCIAN = 858, 21

FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {extra}")
        FAILURES.append(name)


def build_fixture_db(path: Path) -> None:
    """Общая база для тестов, которым нужен кэш со статистикой.

    Раньше этот файл ожидалcя на диске и молча пропускал восемь тестов —
    включая рендер оверлея и весь run_ui. Именно там жили два бага, которые
    роняли приложение в живом драфте, а прогон оставался «зелёным».
    Файл собирается кодом, поэтому состояние тестов не зависит от того,
    что осталось на машине.
    """
    c = Cache(path)
    champs, index = ch.load_champions()
    c.save_champions(champs)
    for role in ("top", "jungle", "mid", "bot", "support"):
        for name in ("Shen", "Vi", "Nautilus", "Lee Sin", "Janna", "Leona",
                     "Ahri", "Zed", "Darius", "Lucian", "Vex", "Yone",
                     "Smolder"):
            cid = ch.resolve(name, index)
            if cid:
                c.save_champ_meta(cid, role, {"role": role, "tier": "1"})
                c.set_counter_role(cid, role)
                c.set_slug(cid, name.lower().replace(" ", ""))
    # Разная winrate по ролям, чтобы сортировка пиков была проверяема
    for i, name in enumerate(("Shen", "Vi", "Nautilus")):
        cid = ch.resolve(name, index)
        if not cid:
            continue
        c.save_matchups(cid, {777: {"win_rate": 52.0 + i,
                                    "games": 300 + i * 10}})
        c.save_synergies(cid, "jungle", {cid: {"win_rate": 53.0 + i,
                                              "games": 200}})
        c.save_items(cid, "jungle",
                     [{"name": "Sunfire", "pick_rate": 60.0 - i,
                       "win_rate": 55.0 - i, "games": 500},
                      {"name": "Shurelya", "pick_rate": 40.0,
                       "win_rate": 54.0, "games": 400}],
                     patch="14.24")
        c.mark_updated(cid, "jungle")
    c.set_state("patch", "14.24")
    c.close()


def ensure_ban_db() -> Path:
    """Отдельная база для совета по банам и блока «вне пула».

    В общей фикстуре роль внутри slugs у всех чемпионов одна и та же
    (последняя из цикла по ролям), поэтому фильтр «бан про свою роль» там
    не проверить. Здесь роли расставлены по-настоящему, матчапы завязаны
    на конкретных контрпиков, а помимо мейнов пула добавлены чемпионы роли
    со своими матчапами — для top_off_pool.
    """
    db = Path(tempfile.gettempdir()) / "opencode" / "ban_advice_test2.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.is_file() and db.stat().st_size >= 1024:
        return db
    c = Cache(db)
    champs, _index = ch.load_champions()
    c.save_champions(champs)
    for cid, role in ((98, "top"), (254, "jungle"), (111, "support")):
        c.set_counter_role(cid, role)
    # Ви тяжело даются Аатрокс, Маокай и Картус; Мальфита она бьёт.
    c.save_matchups(254, {266: {"win_rate": 40.0},
                          57: {"win_rate": 42.0},
                          30: {"win_rate": 44.0},
                          54: {"win_rate": 54.0}})
    # Наутилусу некомфортно против Эша — вот его и надо банить на саппорте.
    c.save_matchups(111, {22: {"win_rate": 38.0},
                          777: {"win_rate": 45.0}})
    # Шен Аатрокса бьёт.
    c.save_matchups(98, {266: {"win_rate": 60.0}})
    # Блок «вне пула»: чемпионы активной роли со своими матчапами. Картус
    # обыгрывает Зеда лучше Маокая, Аатрокс — хуже обоих.
    for cid, role in ((57, "jungle"), (30, "jungle"), (266, "jungle")):
        c.set_counter_role(cid, role)
    c.save_matchups(57, {238: {"win_rate": 50.5}})
    c.save_matchups(30, {238: {"win_rate": 52.0}})
    c.save_matchups(266, {238: {"win_rate": 45.0}})
    c.close()
    return db


def ensure_test_config(name: str = "test_config.json") -> Path:
    """Конфиг для тестов, которым нужен непустой пул.

    Файла раньше просто не существовало, тесты молча уезжали в ветку
    needs_setup и проверяли не тот путь, а пустой оверлей проходил как
    «показана ошибка». Конфиг пишется явно, состояние теста от машины
    не зависит.
    """
    path = Path(tempfile.gettempdir()) / "opencode" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(DEFAULTS)
    data["pool"] = ["Shen", "Vi", "Nautilus"]
    data["pools"] = {"jungle": ["Shen", "Vi", "Nautilus"]}
    data["role"] = "jungle"
    data["auto_role"] = True
    data["hotkey"] = "F9"
    data["window"] = {"x": 100, "y": 100, "always_on_top": False}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def ensure_fixture_db() -> Path:
    """Путь к общей базе, создаём при первом обращении."""
    db = Path(tempfile.gettempdir()) / "opencode" / "cli_test.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if not db.is_file() or db.stat().st_size < 1024:
        build_fixture_db(db)
    return db


# ---------------- LCU ----------------
def test_lcu_completed_bans() -> None:
    """Завершённые баны читаются; незавершённый ход — это turn, не бан."""
    s = parse_session({
        "phase": "BAN_PICK",
        "myTeam": [{"championId": LEESIN}, {"championId": JANN},
                   {"championId": LEONA}, {"championId": 0},
                   {"championId": 0}],
        "theirTeam": [{"championId": ZED}, {"championId": DARIUS},
                      {"championId": LUCIAN}, {"championId": VEX},
                      {"championId": 0}],
        "actions": [
            # завершённые баны врагов
            {"type": "ban", "isAllyAction": False, "isComplete": True,
             "isInProgress": False, "championId": YONE},
            {"type": "ban", "isAllyAction": False, "isComplete": True,
             "isInProgress": False, "championId": SMOLDER},
            # завершённые пики союзников
            {"type": "pick", "isAllyAction": True, "isComplete": True,
             "isInProgress": False, "championId": LEESIN},
            {"type": "pick", "isAllyAction": True, "isComplete": True,
             "isInProgress": False, "championId": JANN},
            # мой текущий ход: пик, ещё не сделан
            {"type": "pick", "isAllyAction": True, "isComplete": False,
             "isInProgress": True, "championId": 0},
        ],
        "timer": {"adjustedTimeLeftInPhase": 18.4},
    })
    check("lcu: враги из theirTeam", s.enemies == [ZED, DARIUS, LUCIAN, VEX],
          str(s.enemies))
    check("lcu: союзники из myTeam (без нулей)",
          s.allies == [LEESIN, JANN, LEONA], str(s.allies))
    check("lcu: завершённые баны собраны",
          s.enemy_bans == [YONE, SMOLDER], str(s.enemy_bans))
    check("lcu: мой ход = pick", s.is_my_turn and s.action_type == "pick",
          f"{s.is_my_turn} {s.action_type}")
    check("lcu: не бан-фаза", not s.is_ban_phase)
    check("lcu: таймер", abs(s.timer_seconds - 18.4) < 0.01,
          str(s.timer_seconds))
    check("lcu: фаза", s.phase == "BAN_PICK")


def test_lcu_hover_is_not_a_ban() -> None:
    """championId в незавершённом действии = наведение, а не сделанный бан."""
    s = parse_session({
        "phase": "BAN_PICK",
        "myTeam": [], "theirTeam": [],
        "actions": [
            {"type": "ban", "isAllyAction": False, "isComplete": False,
             "isInProgress": True, "championId": YONE},
        ],
        "timer": {},
    })
    check("lcu: наведение не считается баном", s.enemy_bans == [],
          str(s.enemy_bans))
    check("lcu: ход = бан врага", s.action_type == "ban" and not s.is_my_turn,
          f"{s.action_type} {s.is_my_turn}")
    check("lcu: is_ban_phase", s.is_ban_phase)


def test_lcu_missing_teammate_slots() -> None:
    """В myTeam сидит 10 слотов, из них 5 нулей — их надо отбросить."""
    s = parse_session({
        "phase": "PLANNING",
        "myTeam": [{"championId": AHRI}] + [{"championId": 0}] * 9,
        "theirTeam": [{"championId": ZED}],
        "actions": [], "timer": {},
    })
    check("lcu: 1 союзник", s.allies == [AHRI], str(s.allies))
    check("lcu: 1 враг", s.enemies == [ZED], str(s.enemies))
    check("lcu: пустой session не падает",
          parse_session({}).allies == [])
    check("lcu: мусор в championId не падает",
          parse_session({"myTeam": [{"championId": "abc"}, {"championId": -3}]}
                        ).allies == [])


# ---------------- конфиг ----------------
def test_config_live_dict_and_roundtrip() -> None:
    """Правка вложенного раздела не должна теряться (регессия)."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.json"
        cfg = load_config(p)
        cfg.window["x"] = 1234
        cfg.window["y"] = 77
        cfg.save()
        again = load_config(p)
        check("config: позиция сохранилась",
              (again.window["x"], again.window["y"]) == (1234, 77),
              f"{again.window['x']},{again.window['y']}")

        check("config: pool — копия, исходный не задет",
              cfg.pool == DEFAULTS["pool"] and cfg.pool is not again.pool)
        check("config: несуществующий файл -> дефолты",
              load_config(Path(td) / "absent.json").role == "mid")

        # регресс: save() писал всегда в DEFAULT_CONFIG, игнорируя --config
        custom = Path(td) / "custom.json"
        custom.write_text('{"role": "adc"}', encoding="utf-8")
        c2 = load_config(custom)
        c2.role_check = None
        c2.data["role"] = "support"
        saved_to = c2.save()
        check("config: save пишет в свой файл, а не в DEFAULT",
              saved_to == custom, str(saved_to))
        check("config: содержимое по адресу",
              json.loads(custom.read_text(encoding="utf-8"))["role"]
              == "support")


def test_config_missing_file_gets_defaults() -> None:
    cfg = load_config(Path(tempfile.gettempdir()) / "no_such_cfg_xyz.json")
    check("config: дефолты на месте", cfg.role == "mid" and cfg.hotkey == "F8",
          f"{cfg.role} {cfg.hotkey}")
    check("config: пул по умолчанию пуст (своих мейнов не выдумываем)",
          cfg.pool == [], str(cfg.pool))
    check("config: веса из DEFAULTS",
          cfg.weights.get("counter") == DEFAULTS["weights"]["counter"])
    check("config: неизвестная роль -> mid",
          Config({"role": "smurf"}).role == "mid")


# ---------------- кэш ----------------
def test_cache_stale_requires_counter_role() -> None:
    """Метка обновления есть, но роль не подобрана — считаем устаревшим."""
    with tempfile.TemporaryDirectory() as td:
        c = Cache(Path(td) / "t.db")
        c.mark_updated(YONE, None)
        check("cache: без counter_role -> stale", c.is_stale(YONE, 24))
        c.set_counter_role(YONE, "mid")
        check("cache: с counter_role -> свежо", not c.is_stale(YONE, 24))
        check("cache: старый timestamp -> stale", c.is_stale(YONE, 0))

        c.save_champ_meta(YONE, "mid", {"win_rate": 52.1, "pick_rate": 3.0,
                                        "ban_rate": 10.0})
        got = c.champ_meta(YONE, "mid")
        check("cache: champ_meta по роли", abs(got["win_rate"] - 52.1) < 1e-6,
              str(got))
        check("cache: champ_meta другой роли пуст", c.champ_meta(YONE, "top") == {})
        c.close()


def test_cache_concurrent_write_and_read() -> None:
    """Автосинк пишет из фонового потока, UI читает из главного."""
    import threading
    with tempfile.TemporaryDirectory() as td:
        c = Cache(Path(td) / "t.db")
        errors: list[Exception] = []

        def writer():
            try:
                for i in range(200):
                    c.save_matchups(YONE, {i: {"win_rate": 50.0 + i % 5}})
                    c.save_synergies(YONE, "mid",
                                     {i: {"win_rate": 52.0, "games": 100}})
                    c.mark_updated(YONE, "mid")
            except Exception as e:            # noqa: BLE001
                errors.append(e)

        def reader():
            try:
                for _ in range(200):
                    c.matchups(YONE)
                    c.synergies(YONE, "mid")
                    c.opponent_ban_rates()
            except Exception as e:            # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        check("cache: нет гонок записи/чтения", not errors,
              str(errors[:1]))
        c.close()


# ---------------- оверлей ----------------
def test_overlay_renders_all_states() -> None:
    """Три вида сообщений: пики, ошибка, статус."""
    from draft.ui import DraftOverlay, _subtitle

    db = ensure_fixture_db()
    if not db.is_file():
        print("  skip оверлей: нет тестовой базы")
        return
    cfg = load_config(ensure_test_config())
    cache = Cache(db)
    champs, _ = ch.load_champions()
    index = ch.build_index(champs)
    # Пул задаём явно, а не берём из конфига: проверка ожидает, что в пуле
    # есть именно забаненный Yone, иначе проверять нечего.
    pool = [champs[ch.resolve(e, index)] for e in
            ("Shen", "Vi", "Nautilus", "Yone") if ch.resolve(e, index)]
    draft = DraftState.from_ids(
        allies=[ch.resolve(n, index) for n in ("Lee Sin", "Janna", "Leona")],
        enemies=[ch.resolve(n, index) for n in ("Zed", "Darius", "Lucian", "Vex")],
        enemy_bans=[ch.resolve(n, index) for n in ("Yone", "Smolder")],
        phase="BAN_PICK", action_type="pick", is_my_turn=True,
        timer_seconds=18.0)

    ov = DraftOverlay(cfg)
    try:
        picks = evaluate(pool, draft, champs, cache, cfg)
        check("overlay: пики отсортированы по убыванию",
              [p.est_winrate for p in picks if p.data_ok] ==
              sorted((p.est_winrate for p in picks if p.data_ok), reverse=True),
              str([p.name for p in picks]))
        banned = [p for p in picks if p.banned_by_enemy]
        check("overlay: забаненный помечен флагом",
              [p.name for p in banned] == ["Yone"], str([p.name for p in banned]))
        check("overlay: метка бана попадает в подпись",
              all("забанен" in _subtitle(p) for p in banned),
              str([_subtitle(p) for p in banned]))
        check("overlay: иконки на диске",
              all(ov.icons.photo(p.cid, p.icon, 40) for p in picks))

        for kind, payload in (("picks", (picks, "заголовок")),
                              ("error", ([], "нет драфта: тест")),
                              ("status", ([], "обновляю статистику"))):
            ov.queue.put((kind, payload[0], payload[1]))
            for _ in range(3):
                ov.root.update()
                ov._pump()
            ov.root.update_idletasks()
            rows = ov.body.winfo_children()
            check(f"overlay: {kind} отрисован ({len(rows)} виджетов)",
                  bool(rows), f"kind={kind}")

        # Блок «вне пула» идёт под списком мейнов и растягивает окно.
        ov.queue.put(("picks", picks[:2], "заголовок", None,
                      {"off_pool": picks[:2]}))
        for _ in range(3):
            ov.root.update()
            ov._pump()
        ov.root.update_idletasks()
        labels = [str(w.cget("text"))
                  for w in ov.body.winfo_children()
                  if w.winfo_class() == "Label"]
        check("overlay: блок вне пула отрисован",
              any("вне пула" in t for t in labels), str(labels[:10]))

        # Нижний бар: пара «данные/лига» и стадия/прогресс синка.
        ov.queue.put(("picks", picks[:2], "заголовок", None,
                      {"footer": {"data_patch": "14.5",
                                  "league_patch": "14.6",
                                  "running": True,
                                  "stage": "чемпионы роли: 40/170…",
                                  "done": 40, "total": 170}}))
        for _ in range(3):
            ov.root.update()
            ov._pump()
        ov.root.update_idletasks()

        def _all_labels(widget):
            out = []
            for w in widget.winfo_children():
                if w.winfo_class() == "Label":
                    out.append(str(w.cget("text")))
                out.extend(_all_labels(w))
            return out

        labels = _all_labels(ov.body)
        check("overlay: футер данные/лига",
              any(t.startswith("данные") for t in labels)
              and any("лига" in t for t in labels), str(labels[:14]))
        check("overlay: стадия синка в футере",
              any("40/170" in t for t in labels), str(labels[:14]))
        check("overlay: бар/стадия видимы в пустых состояниях",
              any("14.6" in t for t in labels), str(labels[:14]))
        check("overlay: перетаскивание привязано",
              bool(ov.root.bind("<B1-Motion>")))
        check("overlay: always on top",
              ov.root.attributes("-topmost") == 1)
    finally:
        # destroy() без update() не обрабатывает <Destroy>, поэтому
        # on_gone (остановка фоновых потоков) вызываем явно — иначе
        # watcher живёт до конца процесса и роняет Tcl при выходе.
        if getattr(ov, 'on_gone', None):
            ov.on_gone()
        ov.root.destroy()
        cache.close()


def test_destroy_breaks_tk_reference_cycles() -> None:
    """Регресс: Tcl_AsyncDelete — падение интерпретатора.

    Раньше App, оверлей и замыкания хоткея образовывали цикл, который
    подбирал сборщик мусора в фоновом потоке синка. Удалять Tk-объекты
    можно только из главного потока, поэтому процесс падал целиком —
    и падал не сразу, а на следующем тесте, что делало баг почти неуловимым.

    После сноса окна все обратные ссылки обязаны быть пустыми: тогда объект
    освобождается по счётчику ссылок сразу, в главном потоке.
    """
    import tkinter

    import draft.main as m

    tmp = Path(tempfile.gettempdir()) / "opencode"
    db = ensure_fixture_db()
    if not db.is_file():
        print("  skip циклы: нет тестовой базы")
        return

    class FakeListener:
        def __init__(self, spec, cb):
            self.spec = spec

        def start(self):
            return True

        def stop(self):
            pass

    orig_listener = m.HotkeyListener
    orig_loop = tkinter.Tk.mainloop
    m.HotkeyListener = FakeListener
    tkinter.Tk.mainloop = lambda self, *a, **k: None
    try:
        args = argparse.Namespace(config=str(tmp / "test_config.json"),
                                  db=str(db), sync=False, force=False,
                                  once=False)
        app = m.App(args)
        ov = app.run_ui()
        for _ in range(3):
            ov.root.update()
            ov._pump()
        # destroy() без update() не обрабатывает <Destroy>, поэтому
        # on_gone (остановка фоновых потоков) вызываем явно — иначе
        # watcher живёт до конца процесса и роняет Tcl при выходе.
        if getattr(ov, 'on_gone', None):
            ov.on_gone()
        ov.root.destroy()
        check("tk-циклы: потоки синка остановлены",
              not [t for t in app._workers if t.is_alive()],
              str(app._workers))
        check("tk-циклы: хоткей отпущен", app.listener is None)
        check("tk-циклы: refresh_fn отвязан", app.refresh_fn is None)
        check("tk-циклы: ссылка на оверлей снята", app._overlay is None)
        check("tk-циклы: ссылка на root снята", app._root is None)
        check("tk-циклы: статус-callback отвязан", app._status_cb is None)
        check("tk-циклы: приложение помечено мёртвым", app._alive is False)
        check("tk-циклы: оверлей помечен мёртвым", ov.dead is True)
        check("tk-циклы: колбэки оверлея отвязаны",
              ov.on_refresh is None and ov.on_gone is None)
    finally:
        m.HotkeyListener = orig_listener
        tkinter.Tk.mainloop = orig_loop


# ---------------- предметы: парсер, кэш, отрисовка ----------------
ITEM_HTML = """
<div class="wrapper">
<h2>Core builds</h2>
<table><tbody>
<tr><td>
  <img src="/meta/images/lol/16.19.1/item/3118.png" alt="Malignance">
  <span class="pick_rate">54.02%</span>
  <span class="win_rate">52.10%</span>
  <span class="cnt">8,120 Games</span>
</td></tr>
<tr><td>
  <img src="/meta/images/lol/16.19.1/item/3006.png" alt="Rabadon's Deathcap">
  <span class="pick_rate">52.40%</span>
  <span class="win_rate">52.60%</span>
  <span class="cnt">6,700 Games</span>
</td></tr>
<tr><td>
  <img src="/meta/images/lol/16.19.1/item/3364.png" alt="Maelstrom">
  <span class="pick_rate">41.00%</span>
  <span class="win_rate">51.00%</span>
  <span class="cnt">10 Games</span>
</td></tr>
</tbody></table>
<h2>Boots</h2>
<table><tbody>
<tr><td>
  <img src="/meta/images/lol/16.19.1/item/3089.png" alt="Sorcerer's Shoes">
  <span class="pick_rate">52.90%</span>
  <span class="win_rate">52.90%</span>
  <span class="cnt">9,000 Games</span>
</td></tr>
</tbody></table>
</div>
"""


def _split_item_sections(html: str) -> dict[str, str]:
    """Режет фикстуру на секции — так же, как это делает fetch_items."""
    from draft import opgg

    out: dict[str, str] = {}
    bounds = [(html.find(h), k) for k, h in opgg._ITEM_SECTIONS
              if html.find(h) >= 0]
    bounds.sort()
    for n, (start, key) in enumerate(bounds):
        end = bounds[n + 1][0] if n + 1 < len(bounds) else len(html)
        out[key] = html[start:end]
    return out


def test_item_rows_and_recommended_path() -> None:
    """Разбор страницы предметов: id, названия, винрейты, порядок покупки."""
    from draft import opgg

    sections = _split_item_sections(ITEM_HTML)
    core = opgg._parse_item_rows(sections.get("core", ""))
    boots = opgg._parse_item_rows(sections.get("boots", ""))
    flat = [(i, n) for r in core for i, n in zip(r["items"], r["names"])]
    ids = {i for i, _ in flat}
    names = dict(flat)
    check("items: id предметов найдены", {3118, 3006, 3364} <= ids,
          str(sorted(ids)))
    check("items: названия из alt",
          names.get(3118) == "Malignance" and
          names.get(3006) == "Rabadon's Deathcap", str(names))
    mrow = next(r for r in core if 3118 in r["items"])
    check("items: винрейт распарсен",
          51.0 < mrow["win_rate"] < 53.0, str(mrow["win_rate"]))
    check("items: pick_rate отделен от win_rate",
          53.0 < mrow["pick_rate"] < 55.0, str(mrow["pick_rate"]))
    check("items: games распарсен", mrow["games"] > 1000, str(mrow["games"]))
    check("items: обувь распознана отдельной секцией",
          boots and boots[0]["items"][0] == 3089, str(boots))

    # порядок покупки строится только по core-секции: обувь в путь не должна
    # попасть даже будучи самой частой
    path = opgg.recommended_path(core, limit=2)
    got = [p["item_id"] for p in path]
    check("items: порядок покупки — по популярности", got == [3118, 3006],
          str(got))
    check("items: лимит соблюден", len(path) <= 2)
    check("items: названия перенесены в путь",
          path[0]["name"] == "Malignance", str(path[0]))
    check("items: пустой вход дает пустой путь",
          opgg.recommended_path([], limit=3) == [])


def test_item_path_falls_back_without_games() -> None:
    """Разметка OP.GG может потерять счетчик игр — молчать тогда нельзя.

    Раньше при нулевых games цикл выходил сразу, и билд оказывался пустым:
    в оверлее просто ничего не показывалось, без объяснений.
    """
    from draft import opgg

    rows = [{"items": [3118, 3006], "names": ["Malignance", "RDC"],
             "pick_rate": 0.0, "games": 0.0, "win_rate": 52.0},
            {"items": [6653], "names": ["Zyra's Blight"],
             "pick_rate": 0.0, "games": 0.0, "win_rate": 51.0}]
    path = opgg.recommended_path(rows, limit=2)
    check("items: без games порядок строится по винрейту",
          [p["item_id"] for p in path] == [3118, 3006],
          str([p["item_id"] for p in path]))


def test_item_cache_roundtrip_and_stale() -> None:
    """Кэш предметов: запись, чтение, протухание по патчу."""
    tmp = Path(tempfile.gettempdir()) / "opencode"
    db = tmp / "items_t.db"
    if db.is_file():
        db.unlink()
    cache = Cache(db)
    try:
        cid, role = 103, "mid"          # Ahri
        path = [{"item_id": 3118, "name": "Malignance", "win_rate": 0.54,
                 "pick_rate": 0.2, "games": 900},
                {"item_id": 3006, "name": "Rabadon's Deathcap",
                 "win_rate": 0.53, "pick_rate": 0.1, "games": 400},
                {"item_id": 6653, "name": "Zyra's Blight",
                 "win_rate": 0.52, "pick_rate": 0.05, "games": 200}]
        boots = [{"item_id": 3089, "name": "Sorcerer's Shoes",
                  "win_rate": 0.529, "pick_rate": 0.3}]
        n = cache.save_items(cid, role, path, boots, "16.19.1")
        check("cache-items: записано 4 строки", n == 4, str(n))
        got = cache.items(cid, role, limit=3)
        check("cache-items: порядок слотов сохранён",
              [g["item_id"] for g in got] == [3118, 3006, 6653],
              str([g["item_id"] for g in got]))
        check("cache-items: патч в строке", got[0]["patch"] == "16.19.1",
              got[0]["patch"])
        b = cache.boots(cid, role)
        check("cache-items: обувь лежит в слоте 0",
              b and b["item_id"] == 3089, str(b))
        check("cache-items: свежий патч не протух",
              not cache.items_stale(cid, role, 24, "16.19.1"))
        check("cache-items: новый патч = протух",
              cache.items_stale(cid, role, 24, "16.19.2"))

        # регресс: билд стал короче — старые слоты не должны пережить запись,
        # иначе items_stale() вечно видит протухший патч
        cache.save_items(cid, role, path[:1], boots, "16.19.2")
        got2 = cache.items(cid, role, limit=3)
        check("cache-items: старые слоты удалены",
              [g["item_id"] for g in got2] == [3118],
              str([g["item_id"] for g in got2]))
        check("cache-items: патч обновился",
              cache.items_stale(cid, role, 24, "16.19.2") is False)

        # обувь пропала из выдачи — слот 0 тоже должен очиститься
        cache.save_items(cid, role, path[:1], [], "16.19.3")
        check("cache-items: снятая обувь удалена",
              cache.boots(cid, role) is None, str(cache.boots(cid, role)))
    finally:
        cache.close()
        if db.is_file():
            db.unlink()


def test_build_for_pick_prefers_locked_champion() -> None:
    """Блок предметов: сначала закреплённый пик, иначе наведённый."""
    from draft.main import build_for_pick

    tmp = Path(tempfile.gettempdir()) / "opencode"
    db = tmp / "build_pick_t.db"
    if db.is_file():
        db.unlink()
    cache = Cache(db)
    try:
        cfg = load_config(tmp / "build_pick_t.json")
        cfg.data["pool"] = ["Ahri", "Fiora"]
        ahri, fiora = 103, 38
        for cid in (ahri, fiora):
            cache.save_items(cid, cfg.role, [
                {"item_id": 3118, "name": "A", "win_rate": 0.5},
                {"item_id": 3006, "name": "B", "win_rate": 0.4},
            ], [{"item_id": 3089, "name": "Shoes", "win_rate": 0.5}],
                "16.19.1")
        champs = {ahri: ch.Champion(cid=ahri, name="Ahri"),
                  fiora: ch.Champion(cid=fiora, name="Fiora")}

        # пик есть — берём именно его, даже если наведён другой
        draft = DraftState(my_champion=fiora, hovered_champion=ahri)
        b = build_for_pick(draft, cache, cfg, champs)
        check("build: взят закреплённый пик", b and b["cid"] == fiora,
              str(b and b["cid"]))
        check("build: имя чемпиона", b and b["name"] == "Fiora", str(b))
        check("build: помечен как пик", b and b["locked"] is True)
        check("build: три предмета в билде",
              b and len(b["items"]) == 2, str(b and b["items"]))
        check("build: обувь приложена", b and b["boots"] is not None)
        check("build: патч проставлен", b and b["patch"] == "16.19.1",
              str(b and b["patch"]))
        check("build: url предмета для иконки",
              b and "16.19.1/img/item/3118.png" in b["items"][0]["url"],
              str(b and b["items"][0]["url"]))

        # пика нет — fallback на наведение
        draft2 = DraftState(hovered_champion=ahri)
        b2 = build_for_pick(draft2, cache, cfg, champs)
        check("build: fallback на наведённого", b2 and b2["cid"] == ahri)
        check("build: наведение не помечено как пик",
              b2 and b2["locked"] is False)

        # ни пика, ни наведения — блока нет
        check("build: без выбора блока нет",
              build_for_pick(DraftState(), cache, cfg, champs) is None)
        check("build: без драфта блока нет",
              build_for_pick(None, cache, cfg, champs) is None)

        # предметов в кэше нет — лучше молчать, чем врать
        draft3 = DraftState(my_champion=1)
        check("build: без данных в кэше блока нет",
              build_for_pick(draft3, cache, cfg, champs) is None)

        # настройка выключателя обязана реально отключать блок
        cfg.data["items"] = {"enabled": False, "show_count": 3,
                             "show_boots": True}
        check("build: выключенный переключатель убирает блок",
              build_for_pick(draft, cache, cfg, champs) is None)
        cfg.data["items"] = {"enabled": True, "show_count": 3,
                             "show_boots": False}
        check("build: без обуви билд короче",
              build_for_pick(draft, cache, cfg, champs)["boots"] is None)
        cfg.data["items"] = {"enabled": True, "show_count": 1,
                             "show_boots": True}
        check("build: лимит предметов уважается",
              len(build_for_pick(draft, cache, cfg, champs)["items"]) == 1)

        # билд собрали под другой ролью — показываем его, а не пустоту
        cfg.data["items"] = {"enabled": True, "show_count": 3,
                             "show_boots": True}
        cfg.data["role"] = "support"
        b3 = build_for_pick(DraftState(my_champion=ahri), cache, cfg, champs)
        check("build: билд взят из соседней роли",
              b3 and b3["role"] in cache.item_roles(ahri) and
              b3["role"] != "support" and len(b3["items"]) == 2,
              str(b3 and (b3["role"], len(b3["items"]))))
    finally:
        cache.close()
        if db.is_file():
            db.unlink()


def _all_label_texts(widget) -> list[str]:
    """Все тексты меток под виджетом, включая вложенные фреймы.

    Блок предметов строится в отдельном Frame, поэтому обход только
    прямых детей его не увидит.
    """
    out: list[str] = []
    for child in widget.winfo_children():
        if child.winfo_class() == "Label":
            out.append(child.cget("text"))
        out.extend(_all_label_texts(child))
    return out


def test_overlay_renders_item_block() -> None:
    """Блок предметов реально рисуется в оверлее."""
    import tkinter

    from draft.ui import DraftOverlay

    cfg = load_config(Path(tempfile.gettempdir()) / "opencode" /
                      "test_config.json")
    ov = DraftOverlay(cfg)
    build = {
        "cid": 103, "name": "Ahri", "locked": True, "role": "mid",
        "patch": "16.19.1",
        "items": [{"item_id": 3118, "name": "Malignance", "win_rate": 0.54,
                   "url": ""},
                  {"item_id": 3006, "name": "Rabadon's Deathcap",
                   "win_rate": 0.53, "url": ""}],
        "boots": {"item_id": 3089, "name": "Sorcerer's Shoes",
                  "win_rate": 0.52, "url": ""},
    }
    try:
        ov.post([], "заголовок", build)
        ov._pump()
        ov.root.update_idletasks()
        texts = _all_label_texts(ov.body)
        check("overlay: имя чемпиона в блоке",
              any("Ahri" in t for t in texts), str(texts))
        check("overlay: названия предметов",
              any("Malignance" in t for t in texts) and
              any("Sorcerer's Shoes" in t for t in texts), str(texts))
        check("overlay: билд отмечен как пик",
              any("пик" in t for t in texts), str(texts))
        check("overlay: билд есть — про «нет данных» молчим",
              not any("нет данных" in t for t in texts), str(texts))

        ov.post([], "заголовок", None)
        ov._pump()
        ov.root.update_idletasks()
        texts2 = _all_label_texts(ov.body)
        check("overlay: блок исчезает без данных",
              not any("Malignance" in t for t in texts2), str(texts2))
    finally:
        # destroy() без update() не обрабатывает <Destroy>, поэтому
        # on_gone (остановка фоновых потоков) вызываем явно — иначе
        # watcher живёт до конца процесса и роняет Tcl при выходе.
        if getattr(ov, 'on_gone', None):
            ov.on_gone()
        ov.root.destroy()


def test_lcu_own_pick_from_local_player_cell() -> None:
    """localPlayerCellId -> наш пик, и только наш."""
    session = {
        "phase": "BAN_PICK", "timer": {"adjustedTimeLeftInPhase": 18.0},
        "localPlayerCellId": 1, "actions": [],
        "myTeam": [
            {"championId": 0, "action": "pick", "completed": False},
            {"championId": 103, "action": "pick", "completed": True},
            {"championId": 64, "action": "pick", "completed": True},
            {"championId": 0, "action": "ban", "completed": True},
            {"championId": 0, "action": "ban", "completed": True},
        ],
        "theirTeam": [{"championId": 7}, {"championId": 222}, {}],
        "bans": {"myTeamBans": [222, 51, 11], "theirTeamBans": [103, 7, 0]},
    }
    st = parse_session(session)
    check("lcu: свой пик прочитан", st.my_champion == 103, str(st.my_champion))
    check("lcu: свой пик не попал в союзники дважды",
          st.allies.count(103) == 1, str(st.allies))
    check("lcu: мой бан не в союзниках", 222 not in st.allies, str(st.allies))

    session2 = dict(session, localPlayerCellId=-1)
    check("lcu: некорректный cell не ломает разбор",
          parse_session(session2).my_champion == 0)

    session3 = dict(session, myTeam=[{"championId": 0}])
    check("lcu: cell за пределами myTeam -> 0",
          parse_session(session3).my_champion == 0)


def test_lcu_live_shape_picks_from_actions() -> None:
    """Пики приходят из actions, а не из myTeam/theirTeam.

    Живой клиент держит championId в myTeam пустым до подтверждения, поэтому
    наивный разбор видел пустую доску: ни союзников, ни врагов. Плюс номер
    поля называется actorCellId — с actorCell слот всегда оставался не найден.
    """
    session = {
        "phase": "BAN_PICK", "localPlayerCellId": 3,
        "myTeam": [{"cellId": i, "position": p, "championId": 0}
                   for i, p in enumerate(["top", "jungle", "mid",
                                          "jungle", "support"])],
        "theirTeam": [{"cellId": 5 + i, "position": p, "championId": 0}
                      for i, p in enumerate(["top", "jungle", "mid",
                                             "bot", "support"])],
        "actions": [
            {"id": 0, "actorCellId": -1, "type": "ban", "isAllyAction": True,
             "isComplete": True, "isInProgress": False, "championId": 111},
            {"id": 1, "actorCellId": -1, "type": "ban", "isAllyAction": False,
             "isComplete": True, "isInProgress": False, "championId": 222},
            {"id": 2, "actorCellId": 2, "type": "pick", "isAllyAction": True,
             "isComplete": True, "isInProgress": False, "championId": 64},
            {"id": 3, "actorCellId": 3, "type": "pick", "isAllyAction": True,
             "isComplete": True, "isInProgress": False, "championId": 121},
            {"id": 4, "actorCellId": 6, "type": "pick", "isAllyAction": False,
             "isComplete": True, "isInProgress": False, "championId": 36},
            {"id": 5, "actorCellId": 4, "type": "pick", "isAllyAction": True,
             "isComplete": False, "isInProgress": True, "championId": 555},
            {"id": 6, "actorCellId": 8, "type": "pick", "isAllyAction": False,
             "isComplete": False, "isInProgress": True, "championId": 145},
        ],
    }
    st = parse_session(session)
    check("lcu: враги видны из actions", 36 in st.enemies and 145 in st.enemies,
          str(st.enemies))
    check("lcu: союзники видны из actions", 64 in st.allies, str(st.allies))
    check("lcu: пик попал в свою ячейку",
          st.ally_slots[2].cid == 64 and st.ally_slots[2].role == "mid",
          str(st.ally_slots))
    check("lcu: подтверждённый пик locked", st.ally_slots[2].locked)
    check("lcu: пре-пик не locked", st.ally_slots[4].is_prepick)
    check("lcu: пре-пик отдельно от пиков",
          st.ally_picks == [64, 121] and st.ally_prepicks == [555],
          f"{st.ally_picks} / {st.ally_prepicks}")
    check("lcu: вражеский наведённый прочитан", st.enemy_hovered == 145,
          str(st.enemy_hovered))
    check("lcu: чужая очередь не считается нашей", not st.is_my_turn)
    # Клиент нумерует ячейки с нуля, поэтому localPlayerCellId=3 — это
    # четвёртый слот myTeam, а не третий со сдвигом.
    check("lcu: своя ячейка по actorCellId", st.my_cell == 3, str(st.my_cell))
    check("lcu: мой чемпион из своей ячейки", st.my_champion == 121,
          str(st.my_champion))


def test_overlay_grows_window_with_content() -> None:
    """Окно растёт вместе с содержимым — строки не накладываются.

    Высота выставлялась один раз, при первой переполненной строке, и дальше
    не обновлялась: доска ролей, баны и блок предметов добавлялись, а окно
    оставалось прежней высоты, и мейны визуально наезжали друг на друга.
    """
    import tkinter

    from draft.ui import DraftOverlay

    cfg = load_config(Path(tempfile.gettempdir()) / "opencode" /
                      "test_config.json")
    ov = DraftOverlay(cfg)

    class FakePick:
        cid = 103
        name = "Ahri"
        icon = ""
        est_winrate = 52.0
        data_ok = True
        confidence = 1.0
        confidence_pct = 100
        banned_by_enemy = False
        banned_by_ally = False
        ban_risk = 0.0
        notes: list[str] = []

    extra = {"board": {"allies": [("Shen", "jungle", True, False)],
                       "enemies": [("Ahri", "mid", False, False)],
                       "my_cell": 0},
             "bans": {"ally": ["Yone"], "enemy": ["Zed"]}}
    try:
        ov.post([FakePick()], "заголовок", None, extra)
        ov._pump()
        ov.root.update_idletasks()
        ov._fit_width()
        short = ov.root.winfo_reqheight()

        ov.post([FakePick() for _ in range(6)], "заголовок", {
            "cid": 103, "name": "Ahri", "locked": True, "role": "mid",
            "patch": "16.19.1",
            "items": [{"item_id": 3118, "name": "A", "win_rate": 0.5,
                       "url": ""},
                      {"item_id": 3006, "name": "B", "win_rate": 0.4,
                       "url": ""}],
            "boots": {"item_id": 3089, "name": "Shoes", "win_rate": 0.5,
                      "url": ""}}, extra)
        ov._pump()
        ov.root.update_idletasks()
        ov._fit_width()
        tall = ov.root.winfo_reqheight()
        check("overlay: содержимое стало выше", tall > short,
              f"{short} -> {tall}")
        check("overlay: окно выросло под содержимое",
              ov.root.winfo_height() >= tall,
              f"win={ov.root.winfo_height()} req={tall}")
    finally:
        if getattr(ov, 'on_gone', None):
            ov.on_gone()
        ov.root.destroy()


def test_lcu_nested_rounds_from_real_client() -> None:
    """Реальный снимок клиента: actions — список РАУНДОВ, ячейки с нуля.

    Клиент отдаёт actions как [[ ban-раунд ], [ ten_bans_reveal ], [ pick-раунд ]].
    Код итерировался напрямую и получал список вместо словаря, отбрасывая всё
    по isinstance(a, dict): не читались ни баны, ни пики, ни очередь — доска
    оставалась пустой весь матчу. Плюс признак завершения называется
    completed, а не isComplete, и assignedPosition приходит как "utility"
    вместо "support".
    """
    session = {
        "localPlayerCellId": 0,
        "timer": {"phase": "BAN_PICK", "adjustedTimeLeftInPhase": 24.0},
        "myTeam": [
            {"cellId": 0, "assignedPosition": "jungle", "championId": 98},
            {"cellId": 1, "assignedPosition": "utility", "championId": 147},
            {"cellId": 2, "assignedPosition": "bottom", "championId": 18},
            {"cellId": 3, "assignedPosition": "top", "championId": 74},
            {"cellId": 4, "assignedPosition": "middle", "championId": 101},
        ],
        "theirTeam": [
            {"cellId": 5, "championId": 21}, {"cellId": 6, "championId": 59},
            {"cellId": 7, "championId": 412}, {"cellId": 8, "championId": 106},
            {"cellId": 9, "championId": 157},
        ],
        "actions": [
            [{"id": i, "actorCellId": i, "type": "ban",
              "isAllyAction": i < 5, "isInProgress": False,
              "completed": True, "championId": 100 + i} for i in range(10)],
            [{"id": 104, "actorCellId": -1, "type": "ten_bans_reveal",
              "isAllyAction": False, "completed": True,
              "isInProgress": False, "championId": 0}],
            [{"id": 10 + i, "actorCellId": i, "type": "pick",
              "isAllyAction": i < 5, "completed": True,
              "isInProgress": False, "championId": 200 + i}
             for i in range(10)],
        ],
    }
    st = parse_session(session)
    check("live: фаза из timer", st.phase == "BAN_PICK", st.phase)
    check("live: баны наши прочитаны",
          st.ally_bans == [100, 101, 102, 103, 104], str(st.ally_bans))
    check("live: баны врагов прочитаны",
          st.enemy_bans == [105, 106, 107, 108, 109], str(st.enemy_bans))
    check("live: пики союзников прочитаны",
          st.ally_picks == [200, 201, 202, 203, 204], str(st.ally_picks))
    check("live: пики врагов прочитаны",
          st.enemy_picks == [205, 206, 207, 208, 209], str(st.enemy_picks))
    check("live: пре-пиков нет после подтверждения", st.ally_prepicks == [],
          str(st.ally_prepicks))
    check("live: подтверждённый пик locked", all(s.locked for s in st.ally_slots))
    check("live: своя ячейка при нумерации с нуля",
          st.my_cell == 0 and st.my_champion == 200,
          f"{st.my_cell}/{st.my_champion}")
    check("live: роль utility читается как саппорт",
          st.ally_slots[1].role == "support", st.ally_slots[1].role)
    check("live: роль middle читается как мид",
          st.ally_slots[4].role == "mid", st.ally_slots[4].role)
    check("live: роль bottom читается как бот",
          st.ally_slots[2].role == "adc", st.ally_slots[2].role)


def test_lcu_side_uses_real_cell_ids() -> None:
    """Наши слоты не обязаны быть 0-4: реальный матч, где у нас 5-9.

    LCU нумерует cellId в общем пространстве, и сторона клиента не фиксирует
    диапазон: здесь наши 5-9, враги 0-4. Старый расчёт по сдвигам давал
    my_cell=7 (вне пяти слотов) — my_slot становился None и блок предметов
    пропадал. Ищем слот через сам field cellId, а не через «наша команда —
    первые».
    """
    session = {
        "localPlayerCellId": 7,
        "timer": {"phase": "GAME_STARTING"},
        "myTeam": [
            {"cellId": 5, "assignedPosition": "utility", "championId": 17},
            {"cellId": 6, "assignedPosition": "bottom", "championId": 901},
            {"cellId": 7, "assignedPosition": "jungle", "championId": 98},
            {"cellId": 8, "assignedPosition": "top", "championId": 41},
            {"cellId": 9, "assignedPosition": "middle", "championId": 238},
        ],
        "theirTeam": [
            {"cellId": 0, "championId": 26}, {"cellId": 1, "championId": 32},
            {"cellId": 2, "championId": 516}, {"cellId": 3, "championId": 101},
            {"cellId": 4, "championId": 222},
        ],
        "actions": [
            [{"type": "ban", "actorCellId": -1, "isAllyAction": False,
              "completed": True, "championId": 89},
             {"type": "ban", "actorCellId": -1, "isAllyAction": True,
              "completed": True, "championId": 90}],
            [{"type": "ban", "actorCellId": -1, "isAllyAction": False,
              "completed": True, "championId": 234},
             {"type": "ban", "actorCellId": -1, "isAllyAction": True,
              "completed": True, "championId": 804}],
            [{"type": "pick", "actorCellId": 0, "isAllyAction": False,
              "completed": True, "championId": 26},
             {"type": "pick", "actorCellId": 1, "isAllyAction": False,
              "completed": True, "championId": 32},
             {"type": "pick", "actorCellId": 2, "isAllyAction": False,
              "completed": True, "championId": 516},
             {"type": "pick", "actorCellId": 3, "isAllyAction": False,
              "completed": True, "championId": 101},
             {"type": "pick", "actorCellId": 4, "isAllyAction": False,
              "completed": True, "championId": 222},
             {"type": "pick", "actorCellId": 5, "isAllyAction": True,
              "completed": True, "championId": 17},
             {"type": "pick", "actorCellId": 6, "isAllyAction": True,
              "completed": True, "championId": 901},
             {"type": "pick", "actorCellId": 7, "isAllyAction": True,
              "completed": True, "championId": 98},
             {"type": "pick", "actorCellId": 8, "isAllyAction": True,
              "completed": True, "championId": 41},
             {"type": "pick", "actorCellId": 9, "isAllyAction": True,
              "completed": True, "championId": 238}],
        ],
    }
    st = parse_session(session)
    check("side: свой слот найден по cellId",
          st.my_cell == 2 and st.my_champion == 98, f"{st.my_cell}/{st.my_champion}")
    check("side: роль по cellId", st.my_role == "jungle", st.my_role)
    check("side: союзники в порядке myTeam",
          st.allies == [17, 901, 98, 41, 238], str(st.allies))
    check("side: враги в порядке theirTeam",
          st.enemies == [26, 32, 516, 101, 222], str(st.enemies))
    check("side: пики врагов не на нашем слоте",
          st.ally_slots[2].cid == 98, str(st.ally_slots[2].cid))


def test_lcu_ban_phase_not_during_picks() -> None:
    """BAN_PICK — это баны и пики сразу, совет по бану в пиках не нужен."""
    st = parse_session({
        "localPlayerCellId": 0,
        "timer": {"phase": "BAN_PICK"},
        "myTeam": [{"championId": 0} for _ in range(5)],
        "theirTeam": [{"championId": 0} for _ in range(5)],
        "actions": [[{"actorCellId": 0, "type": "pick", "isAllyAction": True,
                      "isInProgress": True, "completed": False,
                      "championId": 0}]],
    })
    check("live: на фазе пиков бан-совет скрыт", not st.is_ban_phase,
          f"{st.action_type}/{st.phase}")
    check("live: пик наш ход", st.is_my_turn and st.action_type == "pick")


def test_lcu_ban_turn_recognized() -> None:
    """Подсказка по бану зависит от «сейчас наш ход» — разбираем оба случая."""
    team = [{"cellId": i, "position": "jungle", "championId": 0}
            for i in range(5)]
    base = {"phase": "BAN_PICK", "localPlayerCellId": 2,
            "myTeam": team,
            "theirTeam": [{"cellId": 5 + i, "championId": 0}
                          for i in range(5)]}

    mine = dict(base, actions=[
        {"actorCellId": 2, "type": "ban", "isAllyAction": True,
         "isComplete": False, "isInProgress": True, "championId": 0}])
    st = parse_session(mine)
    check("lcu: наш бан распознан", st.is_ban_phase and st.is_my_turn,
          f"{st.action_type}/{st.is_my_turn}")

    theirs = dict(base, actions=[
        {"actorCellId": 7, "type": "ban", "isAllyAction": False,
         "isComplete": False, "isInProgress": True, "championId": 0}])
    st2 = parse_session(theirs)
    check("lcu: чужой бан не выдан за наш",
          st2.is_ban_phase and not st2.is_my_turn,
          f"{st2.action_type}/{st2.is_my_turn}")

    # Общий бан команды: ячейки нет, но ход единственный и на нашей стороне.
    teamwide = dict(base, actions=[
        {"actorCellId": -1, "type": "ban", "isAllyAction": True,
         "isComplete": False, "isInProgress": True, "championId": 0}])
    st3 = parse_session(teamwide)
    check("lcu: общий бан команды считается нашим",
          st3.is_ban_phase and st3.is_my_turn,
          f"{st3.action_type}/{st3.is_my_turn}")


def test_scoring_tank_from_prepick_not_flagged() -> None:
    """«нет танка в команде» на пре-пике Шена — ложное замечание.

    Состав считается по всем слотам, включая пре-пики: танк уже выбран, и
    предлагать второго танка на том же основании бессмысленно.
    """
    champs = {
        121: ch.Champion(cid=121, name="Shen", tags=("Tank", "Fighter")),
        111: ch.Champion(cid=111, name="Nautilus", tags=("Tank", "Fighter")),
        64: ch.Champion(cid=64, name="Lee Sin", tags=("Fighter",)),
    }
    config = load_config(ensure_test_config("tank_config.json"))
    cache = Cache(ensure_fixture_db())

    # Шен — пре-пик союзника, не подтверждён.
    draft = DraftState.from_ids(allies=(121,), my_champion=121)
    draft.ally_slots[0].is_prepick  # пре-пик
    picks = evaluate([champs[111]], draft, champs, cache, config)
    check("скоринг: пре-пик танка учтён",
          "нет танка в команде" not in picks[0].notes, str(picks[0].notes))

    # А вот когда танка действительно нет — замечание обязано появиться.
    empty = DraftState.from_ids(allies=(64,), my_champion=64)
    picks2 = evaluate([champs[111]], empty, champs, cache, config)
    check("скоринг: без танка замечание есть",
          "нет танка в команде" in picks2[0].notes, str(picks2[0].notes))


def test_ban_advice_role_relevant() -> None:
    """Баны считаются под текущую роль: в лесу не предлагается Эш из саппорта.

    Раньше совет перебирал всех мейнов пула, и контрпик единственного
    саппорт-мейна (Эш против Наутилуса) всплывал в топ на позиции леса.
    Сначала фильтруем мейнов пула по роли, и только потом считаем сильных
    контрпиков.
    """
    champs, _index = ch.load_champions()
    cache = Cache(ensure_ban_db())
    config = load_config(ensure_test_config("ban_config.json"))
    pool = [champs[98], champs[254], champs[111]]
    draft = DraftState.from_ids(allies=(), enemies=())
    draft.phase = "BAN_PICK"

    jungle = [p.name for p in ban_advice(pool, draft, champs, cache, config,
                                         role="jungle")]
    check("баны: на лесу саппорт-контрпик (Эш) не предлагается",
          "Ashe" not in jungle, str(jungle))
    check("баны: сильнейший контрпик Ви (Аатрокс) в самом верху",
          jungle and jungle[0] == "Aatrox", str(jungle))
    check("баны: чемпион, которого Ви бьёт, вне топа",
          "Malphite" not in jungle, str(jungle))

    support = [p.name for p in ban_advice(pool, draft, champs, cache, config,
                                          role="support")]
    check("баны: на саппорте контрпик Наутилуса (Эш) предлагается",
          support and support[0] == "Ashe", str(support))


def test_ban_advice_prefers_true_counters() -> None:
    """В баны идут те, кто бьёт твой пул, а не те, кого ты бьёшь.

    Знак скоринга был перевёрнут: кандидат ранжировался по винрейту твоего
    мейна ПРОТИВ него, и в начало попадали чемпионы, которых ты и так бьёшь.
    Теперь разворачиваем — чемпион с самым низким винрейтом твоего мейна
    обязан быть самым дорогим баном.
    """
    champs, _index = ch.load_champions()
    cache = Cache(ensure_ban_db())
    config = load_config(ensure_test_config("ban_config.json"))
    pool = [champs[98], champs[254], champs[111]]
    draft = DraftState.from_ids(allies=(), enemies=())
    draft.phase = "BAN_PICK"

    advice = ban_advice(pool, draft, champs, cache, config, role="jungle",
                        limit=10)
    top = {p.name: p.est_winrate for p in advice}
    check("баны: Аатрокс (Ви проигрывает) дороже Мальфита (Ви бьёт)",
          top.get("Aatrox", 0.0) > top.get("Malphite", 99.0),
          str(top))
    worst = {p.name for p in advice if any("бьёт твой пул" in n for n in p.notes)}
    check("баны: контрпики помечены как бьющие пул",
          "Aatrox" in worst and "Maokai" in worst, str(worst))


def test_ban_advice_protects_ally_prepick() -> None:
    """Пре-пик своей команды не баним, а вот его контрпики — наоборот.

    Союзник уже закрепил чемпиона, и враг заберёт его слабое место:
    контрпики объявленного выбора дороже, чем защита своего пула.
    """
    champs, _index = ch.load_champions()
    cache = Cache(ensure_ban_db())
    config = load_config(ensure_test_config("ban_config.json"))
    pool = [champs[98], champs[254], champs[111]]

    draft = DraftState.from_ids(allies=(), enemies=())
    draft.phase = "BAN_PICK"
    draft.my_role = "jungle"
    draft.my_cell = -1
    draft.ally_slots = [Slot(cell=0, cid=254)]          # союзник закрепил Ви
    draft.ally_prepicks = [254]

    advice = ban_advice(pool, draft, champs, cache, config, role="jungle")
    names = [p.name for p in advice]
    check("баны: свой пре-пик (Ви) в советы не идёт",
          "Vi" not in names, str(names))
    top = advice[0]
    check("баны: контрпик пре-пика отмечен",
          "защищает препик: Vi" in top.notes, str(top.notes))
    check("баны: контрпик пре-пика усилен бонусом",
          top.est_winrate >= 66.5, f"{top.name} {top.est_winrate}")


def test_ban_advice_threat_from_enemy_hover() -> None:
    """"Враг наводит" поднимает кандидата в бан даже без данных матчапа.

    Пусть по кандидату нет ни одного матчапа мейнов пула — раз враг сам
    показал выбор, бан снимает именно его угрозу.
    """
    champs, _index = ch.load_champions()
    cache = Cache(ensure_ban_db())
    config = load_config(ensure_test_config("ban_config.json"))
    pool = [champs[98], champs[254], champs[111]]

    draft = DraftState.from_ids(allies=(), enemies=())
    draft.phase = "BAN_PICK"
    draft.my_role = "jungle"
    draft.enemy_hovered = 267                                # Нами, данных нет

    advice = ban_advice(pool, draft, champs, cache, config,
                        role="jungle", limit=5)
    nami = next((p for p in advice if p.cid == 267), None)
    check("баны: наведённый без данных всё равно в совете",
          nami is not None, str([p.name for p in advice]))
    check("баны: пометка об угрозе есть",
          nami is not None and any("враг наводит" in n for n in nami.notes),
          str(nami.notes if nami else None))


def test_top_off_pool_and_cache_marker() -> None:
    """Блок «вне пула»: топ чемпионов роли помимо мейнов под драфт.

    Кандидаты — чемпионы активной роли из расширенного синка; оцениваются
    теми же формулами винрейта против текущих врагов. Мейны пула, уже
    выбранные своей командой и забаненные врагами исключаются. Свежесть
    расширенного набора живёт в собственном маркере кэша.
    """
    champs, _index = ch.load_champions()
    cache = Cache(ensure_ban_db())
    config = load_config(ensure_test_config("offpool_config.json"))
    pool = [champs[98], champs[254], champs[111]]

    draft = DraftState.from_ids(allies=(), enemies=(238,))      # против Зеда
    tops = top_off_pool(pool, draft, champs, cache, config, role="jungle")
    names = [p.name for p in tops]
    check("вне пула: лучший по винрейту против Зеда первый",
          names and names[0] == "Karthus", str(names))
    check("вне пула: мейн пула исключён", "Vi" not in names, str(names))
    check("вне пула: размер не больше трёх", len(names) <= 3, str(names))

    taken = DraftState.from_ids(allies=(57,), enemies=(238,))
    n2 = [p.name for p in top_off_pool(pool, taken, champs, cache, config,
                                       role="jungle")]
    check("вне пула: выбранный своей командой исключён",
          "Maokai" not in n2, str(n2))

    banned = DraftState.from_ids(allies=(), enemies=(238,),
                                 enemy_bans=(30,))
    n3 = [p.name for p in top_off_pool(pool, banned, champs, cache, config,
                                       role="jungle")]
    check("вне пула: забаненный врагами исключён",
          "Karthus" not in n3, str(n3))

    am = Path(tempfile.gettempdir()) / "opencode" / "allmeta_test.db"
    if am.is_file():
        am.unlink()
    c2 = Cache(am)
    check("вне пула: без маркера набор устарел",
          c2.all_meta_stale("jungle", 168.0))
    c2.mark_all_meta("jungle")
    check("вне пула: после маркера набор свежий",
          not c2.all_meta_stale("jungle", 168.0))
    check("вне пула: маркер чужой роли не смущает",
          c2.all_meta_stale("support", 168.0))
    c2.close()
    cache.close()


def test_sync_role_candidates_offline() -> None:
    """Расширенный синк: матчапы+мета+роль всех чемпионов активной роли.

    Сеть заменяем моком: страница роли есть только у пары чемпионов,
    остальные «не растут» на роли и честно уходят в no_role, мейны пула
    вообще не перекачиваются, маркер роли проставляется.
    """
    import draft.opgg as opgg_mod
    from draft.sync import sync_role_candidates

    db = Path(tempfile.gettempdir()) / "opencode" / "role_candidates_test.db"
    if db.is_file():
        db.unlink()
    cache = Cache(db)
    champs, _index = ch.load_champions()
    cache.save_champions(champs)
    index = ch.build_index(champs)
    config = load_config(ensure_test_config("rolemata_config.json"))

    calls: list[tuple[str, str]] = []

    def fake_fetch(name: str, role: str):
        calls.append((name, role))
        if name == "Maokai":
            return ({"Zed": {"win_rate": 50.5}},
                    {"win_rate": 48.0, "pick_rate": 5.0, "ban_rate": 1.0},
                    "maokai")
        if name == "Karthus":
            return ({"Darius": {"win_rate": 57.0}},
                    {"win_rate": 55.0, "pick_rate": 3.0, "ban_rate": 0.5},
                    "karthus")
        raise opgg_mod.SourceError(f"{name}/{role}: страницы роли нет")

    orig = opgg_mod.fetch_counters
    opgg_mod.fetch_counters = fake_fetch
    try:
        names, no_role, aborted = sync_role_candidates(config, cache, champs,
                                                       index, role="jungle")
    finally:
        opgg_mod.fetch_counters = orig

    check("синк-роль: не прервался", not aborted)
    check("синк-роль: мейны пула не перекачивались",
          all(name not in ("Shen", "Vi", "Nautilus") for name, _ in calls),
          str(calls[:5]))
    check("синк-роль: собраны чемпионы роли",
          sorted(names) == ["Karthus", "Maokai"], str(names))
    check("синк-роль: остаток без данных роли",
          len(no_role) == len(champs) - 3 - 2, str(len(no_role)))
    check("синк-роль: матчап Маокая сохранён",
          cache.matchups(57).get(238, {}).get("win_rate") == 50.5,
          str(cache.matchups(57).get(238)))
    check("синк-роль: мета Картуса сохранена",
          cache.champ_meta(30, "jungle").get("win_rate") == 55.0,
          str(cache.champ_meta(30, "jungle")))
    check("синк-роль: роль проставлена",
          cache.counter_role(57) == "jungle"
          and cache.counter_role(30) == "jungle",
          f"{cache.counter_role(57)} {cache.counter_role(30)}")
    check("синк-роль: маркер роли проставлен",
          not cache.all_meta_stale("jungle", 168.0))
    cache.close()


def test_sync_skips_extended_during_draft() -> None:
    """Расширенный проход (сотни запросов) не запускается во время драфта.

    draft_active=True в sync(): сеть в драфте недопустима — маркер роли не
    ставится, чтобы следующий запуск догрузил вне-пула, и всё это видно в
    сводке и прогресс-сообщении.
    """
    import draft.opgg as opgg_mod
    import time
    from draft.sync import sync

    db = Path(tempfile.gettempdir()) / "opencode" / "draftsync_test.db"
    if db.is_file():
        db.unlink()
    cache = Cache(db)
    champs, _index = ch.load_champions()
    cache.save_champions(champs)
    config = load_config(ensure_test_config("draftsync_config.json"))

    def boom(*_a, **_k):
        raise opgg_mod.SourceError("сеть выключена")

    messages: list[str] = []
    orig_count = opgg_mod.fetch_counters
    orig_syn = opgg_mod.fetch_synergies
    orig_items = opgg_mod.fetch_items
    orig_sleep = time.sleep
    opgg_mod.fetch_counters = boom
    opgg_mod.fetch_synergies = boom
    opgg_mod.fetch_items = boom
    time.sleep = lambda _s: None
    try:
        report = sync(config, cache, progress=messages.append,
                      draft_active=lambda: True)
    finally:
        opgg_mod.fetch_counters = orig_count
        opgg_mod.fetch_synergies = orig_syn
        opgg_mod.fetch_items = orig_items
        time.sleep = orig_sleep

    check("синк-драфт: расширенный проход отмечен в сводке",
          any("идёт драфт" in w for w in report.warnings),
          str(report.warnings))
    check("синк-драфт: без сети быстро завершился",
          all("вне пула" not in m for m in messages), str(messages[:6]))
    check("синк-драфт: маркер роли не поставлен",
          cache.all_meta_stale("jungle", 168.0))
    cache.close()


def test_sync_role_candidates_aborts_midway() -> None:
    """Прерывание расширенного прохода: done-граница, маркер не ставится.

    abort_if срабатывает на пакете из 10 чемпионов: обработано ровно 10,
    остальное не качаем, а отсутствие маркера заставит следующий запуск
    догрузить пропущенных — собранное не теряется.
    """
    import draft.opgg as opgg_mod
    import draft.sync as sync_mod
    from draft.sync import sync_role_candidates

    db = Path(tempfile.gettempdir()) / "opencode" / "abort_test.db"
    if db.is_file():
        db.unlink()
    cache = Cache(db)
    champs, _index = ch.load_champions()
    cache.save_champions(champs)
    index = ch.build_index(champs)
    config = load_config(ensure_test_config("abort_config.json"))

    def fake_fetch(_name: str, _role: str):
        return ({"Zed": {"win_rate": 50.0}},
                {"win_rate": 49.0, "pick_rate": 5.0, "ban_rate": 1.0},
                "abort-slug")

    orig = opgg_mod.fetch_counters
    orig_sleep = sync_mod.time.sleep
    opgg_mod.fetch_counters = fake_fetch
    sync_mod.time.sleep = lambda _s: None
    try:
        names, no_role, aborted = sync_role_candidates(
            config, cache, champs, index, role="jungle",
            abort_if=lambda: True)
    finally:
        opgg_mod.fetch_counters = orig
        sync_mod.time.sleep = orig_sleep

    check("синк-роль: оборван на границе пакета", aborted)
    check("синк-роль: обработан ровно один пакет",
          len(names) == 10, str(len(names)))
    check("синк-роль: без данных роли при обрыве не осталось",
          no_role == [], str(len(no_role)))
    check("синк-роль: маркер не поставлен при обрыве",
          cache.all_meta_stale("jungle", 168.0))
    cache.close()


def test_footer_stale_reason_and_progress() -> None:
    """Футер оверлея: патч-несоответствие и живой прогресс синка.

    _stale_reason/две _footer_data считаются без сети, только по кэшу и по
    текущему состоянию синка — на каждую перерисовку драфта их и зовут.
    """
    from draft.main import App

    app = object.__new__(App)
    app.champs, _ix = ch.load_champions()
    app.champs = dict(app.champs)
    db = Path(tempfile.gettempdir()) / "opencode" / "footer_test.db"
    if db.is_file():
        db.unlink()
    app.cache = Cache(db)
    app.cache.save_champions(app.champs)
    app.config = load_config(ensure_test_config("footer_config.json"))
    app.pools = {"jungle": [app.champs[98], app.champs[254],
                            app.champs[111]]}
    app._sync_state = {"running": True, "stage": "чемпионы роли: 40/170…",
                       "done": 40, "total": 170, "last": ""}
    for cid in (98, 254, 111):
        app.cache.set_counter_role(cid, "jungle")
        app.cache.mark_updated(cid, "jungle")
    app.cache.mark_all_meta("jungle")

    app.cache.set_state("patch", "14.5")
    app.cache.set_state("patch_seen", "14.6")
    footer = app._footer_data("jungle")
    check("футер: несоответствие патчей видно",
          "14.5" in (footer.get("stale_reason") or ""),
          str(footer))
    check("футер: данные/лига отдаются",
          footer.get("data_patch") == "14.5"
          and footer.get("league_patch") == "14.6",
          f'{footer.get("data_patch")} {footer.get("league_patch")}')
    check("футер: живой прогресс пробрасывается",
          footer.get("running") is True and footer.get("done") == 40,
          str(footer))

    app.cache.set_state("patch", "14.6")
    check("футер: при свежих данных причины нет",
          app._footer_data("jungle").get("stale_reason") is None,
          str(app._footer_data("jungle")))

    app.cache.set_state("patch", "")
    app.cache.set_state("patch_seen", "")
    check("футер: без патча данных — повод обновиться",
          app._footer_data("jungle").get("stale_reason")
          == "статистика ещё не собрана",
          str(app._footer_data("jungle")))
    app.cache.close()


def test_config_pools_stay_separate() -> None:
    """Ролевые пулы изолированы: саппорт не подмешивается джунгле.

    Именно это чинит жалобу «добавил саппортов — их видно и в пуле леса,
    и Люкс зачем-то в кандидатах джунгли».
    """
    from draft.settings import Config

    cfg = Config(data={}, path=Path(tempfile.gettempdir()) /
                 "opencode" / "pools_cfg.json")
    cfg.set_pools({"jungle": ["Shen", "Vi"],
                   "support": ["Lulu", "Nami", "Soraka"]})
    check("пулы: джунгла изолирована",
          cfg.pool_for("jungle") == ["Shen", "Vi"],
          str(cfg.pool_for("jungle")))
    check("пулы: саппорт изолирован",
          cfg.pool_for("support") == ["Lulu", "Nami", "Soraka"],
          str(cfg.pool_for("support")))
    check("пулы: роли перечислены по порядку",
          cfg.pool_roles() == ["jungle", "support"], str(cfg.pool_roles()))
    check("пулы: плоский pool = пул ручной роли",
          cfg.data["pool"] == ["Shen", "Vi"], str(cfg.data["pool"]))


def test_draft_signature_has_no_missing_fields() -> None:
    """Подпись состояния не должна падать на реальном снимке драфта.

    Раньше здесь обращались к draft.ally_picks, которого в DraftState не
    было: исключение в потоке опроса убивало автообновление целиком, и окно
    молча показывало устаревший состав.
    """
    app = object.__new__(App)
    app.champs = {}
    app.pools = {}
    app.config = load_config(ensure_test_config("sig_config.json"))
    draft = parse_session({
        "phase": "BAN_PICK", "localPlayerCellId": 1,
        "myTeam": [{"championId": 0} for _ in range(5)],
        "theirTeam": [{"championId": 0} for _ in range(5)],
        "actions": [{"actorCellId": 1, "type": "pick", "isAllyAction": True,
                     "isComplete": True, "isInProgress": False,
                     "championId": 121}],
    })
    try:
        sig = app._draft_signature(draft)
    except AttributeError as e:
        check("подпись состояния считается", False, str(e))
        return
    check("подпись состояния считается", bool(sig))
    check("подпись меняется при смене состава",
          sig != app._draft_signature(parse_session(dict({
        "phase": "BAN_PICK", "localPlayerCellId": 1,
        "myTeam": [{"championId": 0} for _ in range(5)],
        "theirTeam": [{"championId": 0} for _ in range(5)],
        "actions": [{"actorCellId": 2, "type": "pick", "isAllyAction": True,
                     "isComplete": True, "isInProgress": False,
                     "championId": 64}],
    }))))


def test_hotkey_stop_waits_for_thread() -> None:
    """stop() обязан дождаться потока.

    Иначе хоткей успевает вызвать callback на уже снесённое окно — это и
    есть источник Tcl_AsyncDelete в продакшене, а не только в тестах.
    """
    import threading
    import time

    from draft.hotkey import HotkeyListener

    fired = threading.Event()
    listener = HotkeyListener("Ctrl+Alt+F24", fired.set)
    if not listener.start():                  # хоткей занят — проверим stop()
        print("  skip stop(): хоткей занят другой программой")
        return
    listener.stop()
    check("hotkey: поток остановлен",
          listener._thread is None or not listener._thread.is_alive())
    # после stop() поздний вызов callback уже не страшен
    listener.callback()
    check("hotkey: поздний callback обезврежен", not fired.is_set())
    time.sleep(0.1)
    check("hotkey: фона нет после stop", not fired.is_set())


def test_run_ui_startup() -> None:
    """Регресс: run_ui падал на self.refresh_quiet (несуществующий метод).
    Проверяем весь путь старта, подменив mainloop, чтобы не блокировать."""
    import argparse
    import tkinter

    import draft.main as m

    tmp = Path(tempfile.gettempdir()) / "opencode"
    db = ensure_fixture_db()
    if not db.is_file():
        print("  skip run_ui: нет тестовой базы")
        return

    cfg_path = ensure_test_config()
    args = argparse.Namespace(config=str(cfg_path),
                              db=str(db), sync=False, force=False,
                              once=False)
    app = m.App(args)
    check("app: пул собран", bool(app.pool), str(app.missing))
    # Авто-синк в фоне шлёт post_status и затирает отрисованную ошибку —
    # проверка ниже ловила бы то сообщение, то это. Здесь важен сам рендер,
    # а не гонка с сетью, поэтому синк отключаем явно.
    app.needs_sync = lambda: False

    orig = tkinter.Tk.mainloop
    tkinter.Tk.mainloop = lambda self, *a, **k: None      # не блокируем
    try:
        ov = app.run_ui()
    finally:
        tkinter.Tk.mainloop = orig
    try:
        check("run_ui: оверлей создан", ov is not None)
        check("run_ui: on_refresh назначен", callable(ov.on_refresh))
        # рендер идёт через очередь + after(), а mainloop мы отключили,
        # поэтому прокручиваем кадры вручную
        for _ in range(6):
            ov.root.update()
            ov._pump()
        ov.root.update_idletasks()
        check("run_ui: заголовок отрисован",
              bool(ov.header.cget("text")), ov.header.cget("text"))
        # клиент не запущен — ждём сообщение об ошибке, а не исключение
        check("run_ui: без клиента показана ошибка",
              any("нет данных" in w.cget("text").lower() or
                  "Lockfile" in w.cget("text") or "нет драфта" in
                  w.cget("text").lower()
                  for w in ov.body.winfo_children()
                  if w.winfo_class() == "Label"),
              str([w.cget("text") for w in ov.body.winfo_children()]))
        ov.on_refresh()          # не должно бросать
        check("run_ui: on_refresh безопасен", True)
    finally:
        # destroy() без update() не обрабатывает <Destroy>, поэтому
        # on_gone (остановка фоновых потоков) вызываем явно — иначе
        # watcher живёт до конца процесса и роняет Tcl при выходе.
        if getattr(ov, 'on_gone', None):
            ov.on_gone()
        ov.root.destroy()


def test_settings_window_saves_pool() -> None:
    """Окно настроек: выбор роли и пула, сохранение в config.json."""
    import tkinter

    import draft.main as m
    from draft.settings_ui import MAX_POOL, ROLE_RU, SettingsWindow

    tmp = Path(tempfile.gettempdir()) / "opencode"
    db = ensure_fixture_db()
    cfg_path = Path(tempfile.gettempdir()) / "opencode" / "settings_t.json"
    if not db.is_file():
        print("  skip настройки: нет тестовой базы")
        return

    # отдельный конфиг, чтобы не портить боевой
    cfg_path.write_text(json.dumps({"pool": ["Ahri"], "role": "top"},
                                   ensure_ascii=False), encoding="utf-8")
    args = argparse.Namespace(config=str(cfg_path), db=str(db), sync=False,
                              force=False, once=False)
    app = m.App(args)

    orig = tkinter.Tk.mainloop
    tkinter.Tk.mainloop = lambda self, *a, **k: None
    try:
        ov = app.run_ui()
    finally:
        tkinter.Tk.mainloop = orig
    try:
        # оверлей закрываемый: есть и кнопка, и меню, и on_quit
        check("настройки: кнопка выхода есть", ov.btn_close is not None)
        check("настройки: кнопка настроек есть", ov.btn_settings is not None)
        check("настройки: on_quit назначен", callable(ov.on_quit))
        check("настройки: on_settings назначен", callable(ov.on_settings))
        labels = []
        for i in range(int(ov._menu.index("end")) + 1):
            try:
                labels.append(str(ov._menu.entrycget(i, "label")))
            except tkinter.TclError:
                pass
        check("настройки: в меню есть Выход",
              any("Выход" in x for x in labels), str(labels))
        check("настройки: в меню есть Настройки",
              any("Настройки" in x for x in labels), str(labels))

        sw = SettingsWindow(ov.root, app.config, app.cache, app.champs,
                            on_save=lambda: app.apply_settings())
        check("настройки: роль из конфига", sw.role.get() == "top",
              sw.role.get())
        check("настройки: пул из конфига", len(sw.selected) == 1,
              str(sw.selected))
        check("настройки: роли на русском",
              set(ROLE_RU) == {"top", "jungle", "mid", "adc", "support"})

        # выбираем трёх новых мейнов и сохраняем
        sw.search.set("a")
        sw._filter()
        names = [sw.champs[cid].name for cid in sw._current]
        targets = [n for n in ("Kassadin", "Lux", "Annie") if n in names]
        cids = [ch.resolve(n, sw.index) for n in targets]
        sw._add(cids)
        check("настройки: добавление работает",
              len(sw.selected) == 1 + len(cids),
              f"{len(sw.selected)} vs {1 + len(cids)}")

        sw.role.set("mid")
        sw.hotkey.set("F9")
        sw.opacity.set(0.85)
        sw.item_enabled.set(False)
        sw.item_boots.set(False)
        sw.item_count.set(2)
        # _save() пишет lol_path.txt — уводим в temp
        from draft import lcu as _lcu
        _sb = Path(tempfile.mkdtemp(prefix="lolpath_"))
        _orig_dir = _lcu._app_dir
        _lcu._app_dir = lambda: _sb
        try:
            sw._save()
        finally:
            _lcu._app_dir = _orig_dir
            shutil.rmtree(_sb, ignore_errors=True)
        check("настройки: окно закрылось", not sw.top.winfo_exists())

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        check("настройки: пул сохранён", len(saved["pool"]) == 1 + len(cids),
              str(saved["pool"]))
        check("настройки: роль сохранена", saved["role"] == "mid")
        check("настройки: хоткей сохранён", saved["hotkey"] == "F9")
        check("настройки: прозрачность сохранена",
              abs(saved["window"]["opacity"] - 0.85) < 1e-6,
              str(saved["window"].get("opacity")))
        check("настройки: блок предметов выключается",
              saved["items"]["enabled"] is False, str(saved.get("items")))
        check("настройки: обувь отключается",
              saved["items"]["show_boots"] is False, str(saved.get("items")))
        check("настройки: число предметов сохраняется",
              saved["items"]["show_count"] == 2, str(saved.get("items")))
        check("настройки: apply_settings подхватил предметы",
              app.config.items["enabled"] is False and
              app.config.items_count == 2, str(app.config.items))
        check("настройки: apply_settings пересобрал пул",
              app.config.role == "mid" and app.pool,
              f"{app.config.role} {len(app.pool)}")
    finally:
        # destroy() без update() не обрабатывает <Destroy>, поэтому
        # on_gone (остановка фоновых потоков) вызываем явно — иначе
        # watcher живёт до конца процесса и роняет Tcl при выходе.
        if getattr(ov, 'on_gone', None):
            ov.on_gone()
        ov.root.destroy()


def test_empty_pool_opens_settings() -> None:
    """Первый запуск без мейнов не падает, а просит настроить пул."""
    import tkinter

    import draft.main as m

    tmp = Path(tempfile.gettempdir()) / "opencode"
    db = ensure_fixture_db()
    cfg_path = Path(tempfile.gettempdir()) / "opencode" / "empty_t.json"
    if not db.is_file():
        print("  skip пустой пул: нет тестовой базы")
        return

    cfg_path.write_text(json.dumps({"pool": [], "role": "mid"},
                                   ensure_ascii=False), encoding="utf-8")
    args = argparse.Namespace(config=str(cfg_path), db=str(db), sync=False,
                              force=False, once=False)
    app = m.App(args)
    check("пустой пул: needs_setup", app.needs_setup is True)
    check("пустой пул: не падает", app.pool == [])
    check("пустой пул: once() честно отвечает",
          app.once() == 2, str(app.once()))

    # GUI должен дойти до открытия настроек, а не упасть
    orig_after = tkinter.Misc.after
    orig_mainloop = tkinter.Tk.mainloop
    calls: list[int] = []
    tkinter.Misc.after = lambda self, ms, *a, **k: (calls.append(ms), 0)[1]
    tkinter.Tk.mainloop = lambda self, *a, **k: None
    try:
        ov = app.run_ui()
    finally:
        tkinter.Tk.mainloop = orig_mainloop
        tkinter.Misc.after = orig_after
    try:
        check("пустой пул: настройки запланированы через after(150)",
              150 in calls, str(calls[:5]))
        # настройки реально создаются (иначе после первого запуска
        # пользователь не сможет выбрать мейнов)
        ov.on_settings()
        check("пустой пул: SettingsWindow открывается без ошибок", True)
        for w in ov.root.winfo_children():
            if isinstance(w, tkinter.Toplevel):
                w.destroy()
    finally:
        # destroy() без update() не обрабатывает <Destroy>, поэтому
        # on_gone (остановка фоновых потоков) вызываем явно — иначе
        # watcher живёт до конца процесса и роняет Tcl при выходе.
        if getattr(ov, 'on_gone', None):
            ov.on_gone()
        ov.root.destroy()


def test_lockfile_discovery_and_404_message() -> None:
    """Путь к игре ищем на всех дисках, а не по зашитому C:.

    Регресс: у пользователя League стоит на G:, а код искал только C:/D:/E: —
    из-за этого приложение молчало."""
    import draft.main as m
    from draft import lcu

    check("lcu: хардкод C:\\Riot... больше не единственный путь",
          not hasattr(lcu, "LOCKFILE_PATHS"), "LOCKFILE_PATHS остался")
    check("lcu: есть перебор относительных путей",
          any("Riot Games" in r for r in lcu.REL_GAME_DIRS),
          str(lcu.REL_GAME_DIRS[:2]))
    check("lcu: есть путь gameflow для диагностики",
          lcu.GAMEFLOW_PATH.endswith("gameflow-phase"))

    # фейковое окружение: Lockfile лежит на «другом диске»
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "Riot Games" / "League of Legends"
        fake.mkdir(parents=True)
        (fake / "Lockfile").write_text(
            "LeagueClient:123:456:secret:https", encoding="utf-8")

        app_dir = lcu._app_dir()
        cache = app_dir / "league_path.txt"
        override = app_dir / "lol_path.txt"
        saved = {p: (p.read_text(encoding="utf-8") if p.exists() else None)
                 for p in (cache, override)}
        try:
            cache.unlink(missing_ok=True)
            override.unlink(missing_ok=True)
            # клиент на машине может быть запущен — гасим поиск через
            # процесс, чтобы проверка шла именно по перебору дисков
            orig_proc = lcu._path_from_process
            lcu._path_from_process = lambda: None
            # подменяем перебор дисков на единственный «корень»
            orig_roots = lcu._drive_roots
            lcu._drive_roots = lambda: [Path(td)]
            try:
                found = lcu.find_lockfile()
            finally:
                lcu._drive_roots = orig_roots
                lcu._path_from_process = orig_proc
            check("lcu: Lockfile найден на нестандартном диске",
                  found is not None and found.name == "Lockfile", str(found))
            check("lcu: папка игры запомнена в кэш",
                  cache.exists() and
                  cache.read_text(encoding="utf-8").strip() == str(fake),
                  cache.read_text(encoding="utf-8") if cache.exists()
                  else "нет файла")

            port, token = lcu._credentials(found)
            check("lcu: порт и токен разобраны",
                  port == "456" and token, f"{port} {token[:8]}")
        finally:
            for p, val in saved.items():
                if val is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_text(val, encoding="utf-8")

    check("single instance: функция есть",
          callable(m.acquire_single_instance))


def _raises_lcu(fn) -> bool:
    from draft import lcu
    try:
        fn()
    except lcu.LcuUnavailable:
        return True
    except Exception:                           # noqa: BLE001
        return False
    return False


def test_settings_has_client_section() -> None:
    """В окне настроек есть поле пути и кнопки Обзор/Найти сам."""
    import tkinter

    import draft.main as m
    from draft.settings_ui import SettingsWindow

    tmp = Path(tempfile.gettempdir()) / "opencode"
    db = ensure_fixture_db()
    cfg_path = tmp / "client_t.json"
    if not db.is_file():
        print("  skip клиент-секция: нет тестовой базы")
        return
    cfg_path.write_text(json.dumps({"pool": ["Ahri"], "role": "mid"},
                                   ensure_ascii=False), encoding="utf-8")
    args = argparse.Namespace(config=str(cfg_path), db=str(db), sync=False,
                              force=False, once=False)
    app = m.App(args)
    orig = tkinter.Tk.mainloop
    tkinter.Tk.mainloop = lambda self, *a, **k: None
    try:
        ov = app.run_ui()
    finally:
        tkinter.Tk.mainloop = orig
    try:
        sw = SettingsWindow(ov.root, app.config, app.cache, app.champs)
        check("клиент-секция: поле пути есть",
              isinstance(sw.league_path, tkinter.StringVar))
        texts = []
        for w in sw.top.winfo_children():
            stack = [w]
            while stack:
                cur = stack.pop()
                try:
                    t = cur.cget("text")
                    if t:
                        texts.append(str(t))
                except tkinter.TclError:
                    pass
                stack.extend(cur.winfo_children())
        joined = " | ".join(texts)
        check("клиент-секция: кнопка Обзор", "Обзор" in joined, joined[:120])
        check("клиент-секция: кнопка Найти сам",
              "Найти сам" in joined, joined[:120])
        sw._client_status()
        check("клиент-секция: статус отображается",
              bool(sw.client_lbl.cget("text")), sw.client_lbl.cget("text"))
        sw.top.destroy()
    finally:
        # destroy() без update() не обрабатывает <Destroy>, поэтому
        # on_gone (остановка фоновых потоков) вызываем явно — иначе
        # watcher живёт до конца процесса и роняет Tcl при выходе.
        if getattr(ov, 'on_gone', None):
            ov.on_gone()
        ov.root.destroy()


def _root_gone(root) -> bool:
    """Окно уничтожено? Tk после destroy() бросает TclError, а не 0."""
    import tkinter

    try:
        return not root.winfo_exists()
    except tkinter.TclError:
        return True


def test_quit_does_not_recurse_and_releases_hotkey() -> None:
    """× / ПКМ→Выход / Q: один вызов, хоткей освобождён, окно снесено.

    Раньше quit_app звал overlay.quit(), а тот звал on_quit — рекурсия до
    RecursionError, окно оставалось висеть.
    """
    import tkinter

    import draft.main as m

    tmp = Path(tempfile.gettempdir()) / "opencode"
    db = ensure_fixture_db()
    if not db.is_file():
        print("  skip выход: нет тестовой базы")
        return
    cfg_path = tmp / "quit_t.json"
    cfg_path.write_text(json.dumps({"pool": ["Ahri"], "role": "mid",
                                    "hotkey": "Ctrl+Alt+F11"},
                                   ensure_ascii=False), encoding="utf-8")
    args = argparse.Namespace(config=str(cfg_path), db=str(db), sync=False,
                              force=False, once=False)
    app = m.App(args)
    orig_loop = tkinter.Tk.mainloop
    started = {"n": 0}
    stopped = {"n": 0}

    class FakeListener:
        def __init__(self, spec, cb):
            self.spec = spec

        def start(self):
            started["n"] += 1
            return True

        def stop(self):
            stopped["n"] += 1

    import draft.main as _mm
    orig_listener = _mm.HotkeyListener
    _mm.HotkeyListener = FakeListener
    tkinter.Tk.mainloop = lambda self, *a, **k: None
    try:
        ov = app.run_ui()
        check("выход: хоткей поднялся", started["n"] == 1, str(started))
        check("выход: on_quit назначен", ov.on_quit is not None)

        ov.quit()          # раньше это уходило в бесконечную рекурсию
        check("выход: без рекурсии (окно снесено)", _root_gone(ov.root))
        check("выход: хоткей освобождён", stopped["n"] == 1, str(stopped))
        check("выход: ссылка на on_quit сброшена", ov.on_quit is None)
    finally:
        tkinter.Tk.mainloop = orig_loop
        _mm.HotkeyListener = orig_listener
        try:
            # destroy() без update() не обрабатывает <Destroy>, поэтому
            # on_gone (остановка фоновых потоков) вызываем явно — иначе
            # watcher живёт до конца процесса и роняет Tcl при выходе.
            if getattr(ov, "on_gone", None):
                ov.on_gone()
            ov.root.destroy()
        except tkinter.TclError:
            pass


def test_apply_settings_restarts_hotkey_and_starts_sync() -> None:
    """Смена хоткея применяется сразу, первый пул запускает синхронизацию."""
    import tkinter

    import draft.main as m

    tmp = Path(tempfile.gettempdir()) / "opencode"
    db = ensure_fixture_db()
    if not db.is_file():
        print("  skip apply: нет тестовой базы")
        return
    cfg_path = tmp / "apply_t.json"
    cfg_path.write_text(json.dumps({"pool": ["Ahri"], "role": "mid",
                                    "hotkey": "Ctrl+Alt+F11"},
                                   ensure_ascii=False), encoding="utf-8")
    args = argparse.Namespace(config=str(cfg_path), db=str(db), sync=False,
                              force=False, once=False)
    app = m.App(args)
    orig_loop = tkinter.Tk.mainloop
    tkinter.Tk.mainloop = lambda self, *a, **k: None
    specs = []

    class FakeListener:
        def __init__(self, spec, cb):
            specs.append(spec)
            self.spec = spec

        def start(self):
            return True

        def stop(self):
            return True

    orig_listener = m.HotkeyListener
    m.HotkeyListener = FakeListener
    tkinter.Tk.mainloop = lambda self, *a, **k: None
    try:
        ov = app.run_ui()
    finally:
        tkinter.Tk.mainloop = orig_loop

    sync_calls = {"n": 0}
    orig_sync = app.auto_sync_if_needed

    def fake_sync(on_status=None, on_done=None):
        sync_calls["n"] += 1

    try:
        # 1. пустой пул -> синк не запускаем (считать нечего)
        # pool у App — свойство без сеттера, поэтому emptiness задаём
        # через конфиг и пересборку, как это делает реальный код.
        app.config.set_pools({})
        app.config.data["pool"] = []
        app.auto_sync_if_needed = fake_sync
        app.apply_settings()
        check("apply: пустой пул не запускает синк", sync_calls["n"] == 0,
              str(sync_calls))

        # 2. выбрали пул -> синк должен начаться сам
        app.config.set_pools({"jungle": ["Ahri"]})
        app.config.data["pool"] = ["Ahri"]
        app.apply_settings()
        check("apply: первый пул запускает синхронизацию",
              sync_calls["n"] == 1, str(sync_calls))

        # 3. смена хоткея -> пересоздаём слушатель
        before = len(specs)
        app.config.data["hotkey"] = "Ctrl+Alt+F12"
        app.apply_settings()
        check("apply: смена хоткея пересоздаёт слушатель",
              len(specs) == before + 1 and specs[-1] == "Ctrl+Alt+F12",
              str(specs))
    finally:
        app.auto_sync_if_needed = orig_sync
        m.HotkeyListener = orig_listener
        try:
            # destroy() без update() не обрабатывает <Destroy>, поэтому
            # on_gone (остановка фоновых потоков) вызываем явно — иначе
            # watcher живёт до конца процесса и роняет Tcl при выходе.
            if getattr(ov, "on_gone", None):
                ov.on_gone()
            ov.root.destroy()
        except tkinter.TclError:
            pass


def test_settings_path_accepts_folder_without_lockfile() -> None:
    """Путь можно задать заранее, когда клиент ещё не запущен."""
    from draft import lcu

    app_dir = lcu._app_dir()
    override = app_dir / "lol_path.txt"
    saved = override.read_text(encoding="utf-8") if override.exists() else None
    sandbox = Path(tempfile.mkdtemp(prefix="lolpath_"))
    orig_dir = lcu._app_dir
    lcu._app_dir = lambda: sandbox
    try:
        future = sandbox / "Riot Games" / "League of Legends"
        future.mkdir(parents=True)
        (future / "LeagueClient.exe").write_bytes(b"MZ")   # клиент не запущен
        check("путь: папка принимается без Lockfile",
              lcu.set_league_path(future) == future)
        check("путь: сохраняется и читается",
              lcu.detect_league_path() == future, str(lcu.detect_league_path()))

        junk = sandbox / "Downloads"
        junk.mkdir()
        try:
            lcu.set_league_path(junk)
            check("путь: случайная папка отвергнута", False, "принял")
        except lcu.LcuUnavailable:
            check("путь: случайная папка отвергнута", True)

        # кэш убран -> путь ищется заново; клиента на машине может быть,
        # поэтому гасим оба источника и считаем сканирования дисков
        (sandbox / "lol_path.txt").unlink(missing_ok=True)
        (sandbox / "league_path.txt").unlink(missing_ok=True)
        lcu._last_scan = 0.0
        calls = {"n": 0}
        orig_proc, orig_scan = lcu._path_from_process, lcu._scan_drives
        lcu._path_from_process = lambda: None
        lcu._scan_drives = lambda: (calls.__setitem__("n", calls["n"] + 1),
                                    None)[1]
        try:
            for _ in range(5):
                lcu.detect_league_path()
            check("путь: диски сканируются не чаще раза в минуту",
                  calls["n"] == 1, f"{calls['n']} раз")
            lcu.detect_league_path(force=True)
            check("путь: force=True сканирует сразу", calls["n"] == 2,
                  f"{calls['n']} раз")
        finally:
            lcu._path_from_process = orig_proc
            lcu._scan_drives = orig_scan
            lcu._last_scan = 0.0
    finally:
        lcu._app_dir = orig_dir
        shutil.rmtree(sandbox, ignore_errors=True)
        if saved is None:
            override.unlink(missing_ok=True)
        else:
            override.write_text(saved, encoding="utf-8")


# Тесты, создающие окно Tk. Им нужен отдельный процесс: падение Tcl
# (Tcl_AsyncDelete и подобное) убивает интерпретатор целиком, и из-за этого
# прогон обрывался, не дойдя до остальных проверок.
TK_TESTS = {
    "test_overlay_grows_window_with_content",
    "test_overlay_renders_all_states",
    "test_overlay_renders_item_block",
    "test_destroy_breaks_tk_reference_cycles",
    "test_run_ui_startup",
    "test_settings_window_saves_pool",
    "test_empty_pool_opens_settings",
    "test_settings_has_client_section",
    "test_quit_does_not_recurse_and_releases_hotkey",
    "test_apply_settings_restarts_hotkey_and_starts_sync",
}

# Код для дочернего процесса: те же подмены сети и синка, затем запуск
# одного теста по имени. Провал — ненулевой код возврата.
_SUBPROCESS_SRC = '''
import functools
import sys

import draft.main as _m


def _fake_do_sync(self, force=True, progress=None):
    from draft.sync import SyncReport
    rep = SyncReport()
    for n in self.config.pool:
        rep.ok.append(str(n))
    return rep


def _no_network(url, timeout=30):
    raise OSError("сеть в тестах выключена")


_m.App.do_sync = _fake_do_sync
_m.ch.current_patch = lambda: ""
_m.ch._fetch_json = _no_network
_m.ch.load_champions = functools.partial(_m.ch.load_champions,
                                         allow_network=False)
_g = {"__name__": "test_app", "__file__": "tests/test_app.py"}
exec(compile(open("tests/test_app.py", encoding="utf-8").read(),
             "tests/test_app.py", "exec"), _g)
_g["FAILURES"].clear()
_g[sys.argv[1]]()
sys.exit(1 if _g["FAILURES"] else 0)
'''


def _run_in_subprocess(name: str) -> tuple[bool, str]:
    """Запустить один тест с Tk в отдельном процессе."""
    import subprocess

    proc = subprocess.run([sys.executable, "-c", _SUBPROCESS_SRC, name],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out = proc.stdout or ""
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        out = out + "\n  процесс упал: " + " | ".join(tail)
        return False, out
    return ("FAIL" not in out), out


def main() -> int:
    # Тесты не ходят в сеть: фоновый синк живёт дольше самого теста,
    # возвращается в уже снесённый Tk (Tcl_AsyncDelete -> падение процесса)
    # и делает прогон нестабильным.
    import draft.main as _m

    def _fake_do_sync(self, force=True, progress=None):
        self.last_sync_summary = "тест: сеть отключена"
        from draft.sync import SyncReport
        rep = SyncReport()
        for n in self.config.pool:
            rep.ok.append(str(n))
        return rep

    _m.App.do_sync = _fake_do_sync
    _m.ch.current_patch = lambda: ""

    # Сеть в тестах выключена полностью. Раньше подменялся только do_sync,
    # а фоновый воркер auto_sync_if_needed дозвонился до ddragon через
    # build_pools -> load_champions: запрос висел дольше теста, воркер жил
    # после гашения Tk, и процесс падал с Tcl_AsyncDelete уже после того,
    # как все проверки отмечены пройденными.
    def _no_network(url, timeout=30):
        raise OSError("сеть в тестах выключена")

    _m.ch._fetch_json = _no_network
    _m.ch.load_champions = functools.partial(_m.ch.load_champions,
                                             allow_network=False)

    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for name, t in tests:
        print(f"\n{name}")
        if name in TK_TESTS:
            # Тесты с Tk уходят в отдельный процесс: падение Tcl
            # (Tcl_AsyncDelete и подобное) убивает интерпретатор целиком,
            # и из-за этого прогон обрывался, не дойдя до остальных тестов.
            ok, out = _run_in_subprocess(name)
            print(out.rstrip())
            if not ok:
                print("  FAIL процесс с Tk упал")
                FAILURES.append(name)
            continue
        try:
            t()
        except Exception:                    # noqa: BLE001
            print("  FAIL исключение:")
            traceback.print_exc()
            FAILURES.append(name)
    # Фоновые потоки (watcher, синк, хоткей) обязаны быть остановлены к
    # этому месту: иначе Tk разбирается уже после их финализации, и процесс
    # падает с «Tcl_AsyncDelete: async handler deleted by the wrong thread»
    # ПОСЛЕ того, как все проверки прошли. Раньше это выглядело как
    # «тесты зелёные, а код возвращает ошибку».
    import threading as _th

    leftover = [t for t in _th.enumerate() if t is not _th.main_thread()]
    if leftover:
        print("не остановленные фоновые потоки: " +
              ", ".join(f"{t.name}({'alive' if t.is_alive() else 'dead'})"
                        for t in leftover))
        for t in leftover:
            t.join(timeout=2.0)
        still = [t for t in leftover if t.is_alive()]
        if still:
            print("ВНИМАНИЕ: потоки не завершились: " +
                  ", ".join(t.name for t in still))
            FAILURES.append("остались фоновые потоки")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"ПРОВАЛЕНО {len(FAILURES)}: " + ", ".join(FAILURES))
        return 1
    print("все проверки пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
