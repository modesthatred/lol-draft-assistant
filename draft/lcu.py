"""Чтение состояния драфта из локального API клиента League (LCU).

Клиент во время сессии поднимает HTTPS-сервер на 127.0.0.1:<порт>, порт и пароль
лежат в файле Lockfile рядом с LeagueClient.exe. Сертификат самоподписанный,
поэтому проверку отключаем (соединение идёт по loopback).

Используется один эндпоинт: /lol-champ-select/v1/session — в нём сразу есть
состав обеих команд, все баны и текущая фаза. Один запрос ~10-30 мс, поэтому
задержка на весь отклик уходит на скоринг, а не на сеть.

ВАЖНО: эндпоинт существует только в настоящем драфте (рангед, норма, ARAM-
драфт). На экране подбора ботов в тренировочном режиме его нет — клиент
отдаёт 404, поэтому состав оттуда недоступен.
"""
from __future__ import annotations

import base64
import ctypes
import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# Куда клиент обычно кладёт игру. Абсолютные пути бесполезны: игра может
# стоять на любом диске, поэтому проверяем относительные варианты на каждом.
REL_GAME_DIRS = (
    r"Riot Games\League of Legends",
    r"Games\Riot Games\League of Legends",
    r"Program Files\Riot Games\League of Legends",
    r"Program Files (x86)\Riot Games\League of Legends",
    r"League of Legends",
)

SESSION_PATH = "/lol-champ-select/v1/session"
ICON_INDEX_PATH = "/lol-game-data/assets/v1/champion-icons.json"
GAMEFLOW_PATH = "/lol-gameflow/v1/gameflow-phase"

# Как часто разрешено перебирать диски, если клиент не найден. Без этого
# каждый F8 при выключенном клиенте жил бы полсекунды на stat() по дискам.
RESCAN_COOLDOWN = 60.0
_last_scan = 0.0

# Значения championId, которые клиент использует для "никого"
NONE_ID = 0

PHASE_RU = {
    "PLANNING": "планирование",
    "BAN_PICK": "баны и пики",
    "FINALIZATION": "финальные пики",
    "GAME_START": "старт игры",
    "": "драфт",
}


class LcuUnavailable(RuntimeError):
    pass


# Имена позиций, которые отдаёт клиент, -> наши роли. Клиент знает и "middle",
# и "bottom", а ещё может прислать пустую строку — тогда роль не определяется.
LCU_ROLES = {
    "top": "top",
    "jungle": "jungle",
    "middle": "mid",
    "mid": "mid",
    "bottom": "adc",
    "adc": "adc",
    "support": "support",
    # Клиент в последних патчах присылает именно эти строки, а не
    # support/bottom: "utility" — саппорт, "bottom" — бот. Без них Шако,
    # пикнутый саппортом, выглядел как чемпион без роли.
    "utility": "support",
    "duel": "",
    "none": "",
}

ROLE_RU = {
    "top": "топ",
    "jungle": "лес",
    "mid": "мид",
    "adc": "бот",
    "support": "саппорт",
}


def _role(value) -> str:
    return LCU_ROLES.get(str(value or "").strip().lower(), "")


@dataclass
class Slot:
    """Одна ячейка драфта: кто, на какой позиции и зафиксирован ли он.

    championId в myTeam/theirTeam появляется раньше, чем действие завершено:
    так клиент показывает пре-пик — чемпана, которого союзник (или враг) уже
    выбрал, но ещё не подтвердил. Для скоринга пре-пик — это полноценный
    участник драфта, поэтому держим оба случая, а не только завершённые.
    """
    cell: int = -1
    role: str = ""
    cid: int = 0
    locked: bool = False

    @property
    def is_prepick(self) -> bool:
        return bool(self.cid) and not self.locked


@dataclass
class DraftState:
    # Состав по ролям. allies/enemies ниже — это срез этих слотов, чтобы
    # старый код скоринга продолжал работать.
    ally_slots: list[Slot] = field(default_factory=list)
    enemy_slots: list[Slot] = field(default_factory=list)
    ally_bans: list[int] = field(default_factory=list)
    enemy_bans: list[int] = field(default_factory=list)
    phase: str = ""
    is_my_turn: bool = False
    action_type: str = ""                 # "pick" | "ban" | ""
    timer_seconds: float = 0.0
    # Чемпион, который уже закреплён за тобой — его игра начнётся с этим
    # пиком, поэтому именно ему показываем билд из предметов.
    my_champion: int = 0
    # Наша ячейка в myTeam и роль, которую клиент тебе назначил. По ней
    # определяем, чей пул считать, если включено автоопределение.
    my_cell: int = -1
    my_role: str = ""
    # Чемпион, наведённый в пике: ещё не выбран, но уже интересен.
    hovered_champion: int = 0
    # Кто наводит прямо сейчас: враг — чтобы понимать, кого они готовят.
    enemy_hovered: int = 0
    # Пре-пики союзников (кроме своей ячейки) — в скоринг идут как алли.
    ally_prepicks: list[int] = field(default_factory=list)

    @classmethod
    def from_ids(cls, allies=(), enemies=(), **kw) -> "DraftState":
        """Собрать состояние из голых championId.

        Удобно в тестах и там, где роли не важны: слоты создаются без роли,
        порядок сохраняется. Основной путь — parse_session().
        """
        self = cls(**kw)
        self.ally_slots = [Slot(cell=i, cid=int(c))
                           for i, c in enumerate(allies)]
        self.enemy_slots = [Slot(cell=i, cid=int(c))
                            for i, c in enumerate(enemies)]
        if kw.get("my_cell", -1) < 0 and self.ally_slots:
            self.my_cell = 0
        if not self.my_role and self.ally_slots:
            self.my_role = LCU_ROLES.get("") or ""
        return self

    @property
    def allies(self) -> list[int]:
        return [s.cid for s in self.ally_slots if s.cid]

    @property
    def enemies(self) -> list[int]:
        return [s.cid for s in self.enemy_slots if s.cid]

    @property
    def ally_picks(self) -> list[int]:
        """Союзники с подтверждённым пиком — без пре-пиков."""
        return [s.cid for s in self.ally_slots if s.cid and s.locked]

    @property
    def enemy_picks(self) -> list[int]:
        return [s.cid for s in self.enemy_slots if s.cid and s.locked]

    @property
    def locked_allies(self) -> list[int]:
        return [s.cid for s in self.ally_slots if s.cid and s.locked]

    @property
    def my_slot(self) -> Slot | None:
        if not (0 <= self.my_cell < len(self.ally_slots)):
            return None
        return self.ally_slots[self.my_cell]

    @property
    def my_prepick(self) -> int:
        """Мой собственный пре-пик: выбрал, но ещё не подтвердил."""
        s = self.my_slot
        return s.cid if s and s.is_prepick else 0

    # Изначально свойство называлось my_precick — оставляем, чтобы старые
    # вызовы и скрипты не падали. Новый код использует my_prepick.
    @property
    def my_precick(self) -> int:
        return self.my_prepick

    @property
    def my_champion_locked(self) -> bool:
        """Мой пик подтверждён, а не просто навёден.

        Если слотов нет (состояние собрано вручную из одних championId),
        любой непустой my_champion считаем подтверждённым: иначе билд
        помечался бы как «наведение» при зафиксированном пике.
        """
        s = self.my_slot
        if s is not None:
            return bool(s.locked and s.cid)
        return bool(self.my_champion)

    @property
    def role(self) -> str:
        """Роль, на которой считаем: назначенная клиентом, иначе ручная."""
        return self.my_role

    @property
    def phase_ru(self) -> str:
        return PHASE_RU.get(self.phase, self.phase or "драфт")

    @property
    def is_ban_phase(self) -> bool:
        """Идёт ли сейчас бан.

        Фазу берём только из типа текущего действия. Раньше проверялось ещё и
        "BAN" в названии фазы, но у клиента фаза называется BAN_PICK на всём
        драфте сразу — баны и пики вместе, — так что совет «кого банить»
        показывался и во время пиков.
        """
        phase_u = (self.phase or "").upper()
        if self.action_type:
            return self.action_type == "ban"
        return phase_u in ("BAN", "BANS", "BANNING", "PLANNING")

    @is_ban_phase.setter
    def is_ban_phase(self, _value: bool) -> None:
        pass

    def roles_ru(self) -> str:
        return ", ".join(ROLE_RU[r] for r in sorted({s.role for s in
                                                    self.ally_slots if s.role}))

    def banned_ids(self) -> set[int]:
        return set(self.ally_bans) | set(self.enemy_bans)


def _drive_roots() -> list[Path]:
    """Список корней подключённых дисков (C:, D:, G: ...)."""
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    out = []
    for i in range(26):
        if mask & (1 << i):
            out.append(Path(f"{chr(65 + i)}:\\"))
    return out


def _read_lockfile_shared(path: Path) -> str | None:
    """Читает Lockfile, разрешая одновременный доступ.

    Клиент держит файл открытым, и обычный open() иногда падает с
    'Отказано в доступе' (share violation). Открываем сами с
    FILE_SHARE_READ|WRITE — тогда чтение всегда проходит.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    GENERIC_READ = 0x80000000
    SHARE_ALL = 0x1 | 0x2 | 0x4
    OPEN_EXISTING = 3
    INVALID = ctypes.c_void_p(-1).value
    k32 = ctypes.windll.kernel32
    try:
        handle = k32.CreateFileW(str(path), GENERIC_READ, SHARE_ALL, None,
                                 OPEN_EXISTING, 0, None)
        if handle == INVALID:
            return None
        try:
            buf = ctypes.create_string_buffer(4096)
            read = ctypes.c_ulong(0)
            if not k32.ReadFile(handle, buf, 4096, ctypes.byref(read), None):
                return None
            return buf.raw[:read.value].decode("utf-8", "replace")
        finally:
            k32.CloseHandle(handle)
    except Exception:                           # noqa: BLE001
        return None


def _process_paths(names: tuple[str, ...]) -> list[Path]:
    """Пути исполняемых файлов запущенных процессов через psapi.

    Работает без subprocess — ~1-5 мс, поэтому годен для проверки на каждый
    нажатие хоткея, в отличие от запуска PowerShell.
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    psapi = ctypes.windll.psapi
    kernel = ctypes.windll.kernel32

    count = 1024
    while True:
        buf = (wintypes.DWORD * count)()
        got = wintypes.DWORD(0)
        if not psapi.EnumProcesses(
                ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(got)):
            return []
        n = got.value
        if n < count:
            break
        count *= 2                      # процессов больше — переспросим
        if count > 65536:
            return []

    out: list[Path] = []
    for i in range(n):
        pid = buf[i]
        if not pid:
            continue
        h = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            size = wintypes.DWORD(32768)
            text = ctypes.create_unicode_buffer(size.value)
            if kernel.QueryFullProcessImageNameW(h, 0, text,
                                                 ctypes.byref(size)):
                p = Path(text.value)
                if p.name.lower() in names:
                    out.append(p)
        finally:
            kernel.CloseHandle(h)
    return out


def _path_from_process() -> Path | None:
    """Папка игры по пути запущенного клиента.

    Lockfile лежит рядом с LeagueClient.exe, поэтому берём папку у любого
    клиентского процесса: LeagueClient.exe, LeagueClientUx.exe, RiotClient.exe.
    """
    names = ("leagueclient.exe", "leagueclientux.exe", "riotclient.exe")
    for exe in _process_paths(names):
        folder = exe.parent
        if (folder / "Lockfile").is_file():
            return folder
    return None


def _scan_drives() -> Path | None:
    """Медленный запасной путь: перебор дисков. Вызывается редко."""
    for root in _drive_roots():
        for rel in REL_GAME_DIRS:
            folder = root / rel
            try:
                if (folder / "Lockfile").is_file():
                    return folder
            except OSError:
                continue
    return None


def _app_dir() -> Path:
    """Папка данных проекта. Та же, что у конфига и базы.

    Раньше здесь был AppData, но проект переносимый: путь к игре и кэш
    обязаны лежать рядом с программой, а не в профиле Windows.
    """
    from .settings import APP_DIR

    return APP_DIR


def _looks_like_game_folder(folder: Path) -> str:
    """Чем папка похожа на установленную игру. Возвращает '' если нет.

    Lockfile появляется только при запущенном клиенте, поэтому требовать его
    нельзя: иначе путь нельзя было бы задать заранее.
    """
    for name in ("Lockfile", "LeagueClient.exe", "LeagueClientUx.exe"):
        if (folder / name).exists():
            return name
    if folder.name.lower() == "league of legends":
        return "имя папки"
    return ""


def set_league_path(folder: str | Path | None) -> Path | None:
    """Запоминает папку с игрой (или сбрасывает при None).
    Значение переживает перезапуск и не требует сканирования дисков."""
    p = _app_dir() / "lol_path.txt"
    if folder is None:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    folder = Path(folder)
    if not _looks_like_game_folder(folder):
        raise LcuUnavailable(
            f"В папке {folder} нет ни Lockfile, ни LeagueClient.exe — "
            f"похоже, это не папка с игрой")
    try:
        p.write_text(str(folder), encoding="utf-8")
    except OSError as e:
        raise LcuUnavailable(f"Не удалось сохранить путь: {e}") from e
    return folder


def detect_league_path(force: bool = False) -> Path | None:
    """Папка с игрой: ручной путь -> кэш -> живой процесс -> диски.

    Диски сканируются не чаще раза в RESCAN_COOLDOWN, чтобы не тормозить
    на каждом нажатии хоткея, когда клиент не запущен.
    """
    global _last_scan
    override = _app_dir() / "lol_path.txt"
    if override.is_file():
        try:
            custom = Path(override.read_text(encoding="utf-8").strip())
            if _looks_like_game_folder(custom):
                return custom
            # ручной путь протух — клиент переехал или ещё не запущен
        except OSError:
            pass

    if not force:
        cached = _app_dir() / "league_path.txt"
        if cached.is_file():
            try:
                remembered = Path(cached.read_text(encoding="utf-8").strip())
                if (remembered / "Lockfile").is_file():
                    return remembered
            except OSError:
                pass

    # клиент запущен — спросим у него, это быстрее и надёжнее перебора
    folder = _path_from_process()
    if folder is None:
        now = time.time()
        if force or now - _last_scan > RESCAN_COOLDOWN:
            _last_scan = now
            folder = _scan_drives()
    if folder is None:
        return None
    try:
        (_app_dir() / "league_path.txt").write_text(str(folder),
                                                    encoding="utf-8")
    except OSError:
        pass
    return folder


def find_lockfile() -> Path | None:
    folder = detect_league_path()
    return (folder / "Lockfile") if folder else None


def _credentials(lockfile: Path) -> tuple[str, str]:
    """Lockfile: LeagueClient:pid:port:password:protocol"""
    raw = _read_lockfile_shared(lockfile) or ""
    raw = raw.strip()
    parts = raw.split(":")
    if len(parts) < 4:
        raise LcuUnavailable(
            f"Lockfile не прочитан: {lockfile} "
            f"({raw[:80]!r}). Закрой клиент и запусти заново.")
    _, _pid, port, password = parts[0], parts[1], parts[2], parts[3]
    token = base64.b64encode(f"riot:{password}".encode()).decode()
    return port, token


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def call(path: str, port: str, token: str, timeout: float = 2.0):
    url = f"https://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Connection": "close",
    })
    with urllib.request.urlopen(req, timeout=timeout,
                                context=_ssl_context()) as r:
        body = r.read()
    return json.loads(body.decode("utf-8", "replace"))


def _int(v) -> int:
    try:
        n = int(v)
        return n if n > 0 else 0
    except (TypeError, ValueError):
        return 0


def _raw_int(v) -> int:
    """Число как есть, без обрезки в ноль: номера ячеек бывают нулевыми."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def _flat_actions(session: dict) -> list[dict]:
    """Все действия драфта одним плоским списком.

    Клиент отдаёт actions как список РАУНДОВ, где каждый раунд — свой список
    действий: [[{...10 банов...}], [{...}]]. Раньше код итерировался напрямую по
    session["actions"], получал список вместо словаря и отбрасывал всё через
    isinstance(a, dict) — из-за этого не читались ни баны, ни пики, ни чья
    очередь. Разворачиваем вложенность; плоский список тоже принимаем.
    """
    out: list[dict] = []
    for item in session.get("actions") or []:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, list):
            for sub in item:
                if isinstance(sub, dict):
                    out.append(sub)
    return out


def _cell_map(team: list) -> dict[int, int]:
    """cellId из myTeam/theirTeam -> индекс в этом массиве.

    Клиент нумерует ячейки ОБЩИМ пространством, но порядок сторон не фиксирован:
    в одном матче у нас 0-4 и у врагов 5-9, в другом наоборот — у нас 5-9, у
    врагов 0-4. Поэтому «свой индекс» ищем через явный cellId каждого слота,
    а не через сдвиг. Без этого my_cell уходил за пределы массива (например 7
    при пяти слотах), my_slot становился None — и пропадал блок предметов.
    """
    out: dict[int, int] = {}
    for i, t in enumerate(team):
        if not isinstance(t, dict):
            continue
        cid = _raw_int(t.get("cellId"))
        if cid >= 0 and cid not in out:
            out[cid] = i
    return out


def parse_session(session: dict) -> DraftState:
    """Разбор /lol-champ-select/v1/session в компактный снимок драфта.

    Ключевое отличие от наивного разбора: myTeam/theirTeam не различают
    завершённый пик и пре-пик (чемпион просто подставлен заранее). Поэтому
    «зафиксирован» определяется по завершённым действиям в actions, а слот
    без завершённого действия помечается как пре-пик — он уже влияет на
    драфт, хоть и может быть передуман.
    """
    st = DraftState()
    timer = session.get("timer") or {}
    # Фазы у клиента нет на верхнем уровне сессии — она лежит в timer.phase
    # ("PLANNING", "BAN_PICK", "FINALIZATION", "GAME_STARTING"). Без этого
    # фаза была пустой, а по ней считается бан-фаза и подпись в шапке.
    st.phase = str(session.get("phase") or timer.get("phase") or "")

    team = [t if isinstance(t, dict) else {}
            for t in (session.get("myTeam") or [])]
    foe = [t if isinstance(t, dict) else {}
           for t in (session.get("theirTeam") or [])]
    n_ally = len(team)
    # cellId -> индекс: единственный надёжный перевод номера ячейки в слот,
    # потому что стороны не всегда 0-4/5-9 (см. _cell_map).
    ally_by_cell = _cell_map(team)
    foe_by_cell = _cell_map(foe)
    cell_raw = _raw_int(session.get("localPlayerCellId"))

    my_cell = ally_by_cell.get(cell_raw, -1)
    if my_cell < 0 and 0 <= cell_raw < n_ally:
        # cellId отсутствует в myTeam — остаётся прямое совпадение индексом.
        my_cell = cell_raw
    if my_cell < 0:
        # Последний шанс: старая нумерация с единицы. Берём ту трактовку,
        # что попадает на непустой слот.
        cands = [c for c in (cell_raw - 1, cell_raw) if 0 <= c < n_ally]
        filled = [c for c in cands if _int(team[c].get("championId"))]
        my_cell = (filled or cands or [-1])[0]
    st.my_cell = my_cell

    # --- слоты команд: роль и чемпион ---
    def _slots(key: str) -> list[Slot]:
        out = []
        for i, t in enumerate(session.get(key) or []):
            if not isinstance(t, dict):
                t = {}
            # position — фактическая позиция пика, assignedPosition —
            # назначенная клиентом до пика. Достаётся любой непустой.
            role = _role(t.get("position")) or _role(t.get("assignedPosition"))
            out.append(Slot(cell=i, role=role, cid=_int(t.get("championId"))))
        return out

    st.ally_slots = _slots("myTeam")
    st.enemy_slots = _slots("theirTeam")

    # --- действия: фиксируем баны, ход и наведение ---

    def _resolve(pool: list[Slot], raw: int, cid: int,
                 cell_map: dict[int, int]) -> Slot | None:
        """Слот по номеру ячейки из actorCellId.

        Номера в actions и в myTeam/theirTeam — одно и то же пространство cellId,
        но порядок сторон не фиксирован (у нас бывают 0-4, а врагам 5-9, и
        наоборот). Поэтому переводим через явный cellId слота; сдвиги и
        «с какой базы» больше не нужны. Если cellId в команде не проставлены
        (старый формат или тесты) — запасной путь по совпадению чемпиона.
        """
        if raw < 0:
            return None
        i = cell_map.get(raw)
        if i is not None and 0 <= i < len(pool):
            return pool[i]
        best: tuple[int, Slot] | None = None
        for i in (raw, raw - 1, raw - 6, raw - 5):
            if not (0 <= i < len(pool)):
                continue
            s = pool[i]
            rank = 0 if s.cid == cid else (1 if not s.cid else 2)
            if best is None or rank < best[0]:
                best = (rank, s)
            if rank == 0:
                break
        return best[1] if best else None

    turn_kind = ""
    turn_mine = False
    in_progress_total = 0
    in_progress_ally = False
    # Пики кладём в слоты после прохода: пре-пик лежит только здесь, в
    # myTeam[].championId на этом шаге ещё 0.
    pending: list[tuple[Slot, int, bool, bool]] = []

    for a in _flat_actions(session):
        cid = _int(a.get("championId"))
        kind = a.get("type") or ""
        # Клиент отдаёт признак завершения то как isComplete, то как
        # completed — разные версии LCU использовали оба. Берём любой
        # непустой: иначе ни один бан и ни один пик не считались бы
        # сделанными, и доска оставалась пустой все матчу.
        raw_done = a.get("isComplete")
        if raw_done is None:
            raw_done = a.get("completed")
        complete = bool(raw_done)
        live = bool(a.get("isInProgress")) and not complete
        ally_action = bool(a.get("isAllyAction"))
        pool = st.ally_slots if ally_action else st.enemy_slots
        # У пика ячейка своя у каждого игрока, у бана бывает -1 — общий бан
        # команды, и слоту он не соответствует.
        cell_map = ally_by_cell if ally_action else foe_by_cell
        slot = (_resolve(pool, _raw_int(a.get("actorCellId")), cid, cell_map)
                if cid else None)
        is_mine = slot is not None and slot is st.my_slot

        # Текущий ход — незавершённое действие. Свой ход узнаём по ячейке, а
        # не по флагу isAllyAction: на фазе планирования незавершённым бывает
        # и общий бан, и тогда чужая ячейка дала бы ложное «не моя очередь».
        if live:
            in_progress_total += 1
            in_progress_ally = in_progress_ally or ally_action
            if is_mine or not turn_kind:
                turn_kind, turn_mine = kind, is_mine

        if kind == "ban" and cid and complete:
            (st.ally_bans if ally_action else st.enemy_bans).append(cid)
            continue
        if kind != "pick" or not cid:
            continue
        if slot is not None:
            pending.append((slot, cid, live, ally_action))

    # Единственное незавершённое действие на нашей стороне — наш ход, даже
    # если клиент не отдал номер ячейки.
    if turn_kind and not turn_mine and in_progress_total == 1 and in_progress_ally:
        turn_mine = True
    st.action_type = turn_kind
    st.is_my_turn = turn_mine

    st.is_my_turn = turn_mine

    # Вот здесь пики становятся видимыми: у пре-пиков myTeam[].championId
    # пуст, а закрытый пик клиент туда пишет только после подтверждения.
    for slot, cid, live, ally_action in pending:
        slot.cid = cid
        slot.locked = not live

    # Наведение: клиент подставил championId, но действие не завершено.
    for slot, cid, live, ally_action in pending:
        if not live:
            continue
        if ally_action:
            if slot is st.my_slot and not st.hovered_champion:
                st.hovered_champion = cid
        elif not st.enemy_hovered:
            st.enemy_hovered = cid

    # Банты из actions дублируются в отдельном поле bans — берём и их,
    # иначе на фазе планирования (где actions ещё пуст) банты теряются.
    bans = session.get("bans") or {}
    for src, dst in (("myTeamBans", st.ally_bans),
                     ("theirTeamBans", st.enemy_bans)):
        for cid in bans.get(src) or []:
            cid = _int(cid)
            if cid and cid not in dst:
                dst.append(cid)

    # свой пик: наша ячейка в myTeam. Чемпион в ней может быть уже
    # подтверждён (locked) или ещё только пре-пик — для показа билда это
    # один и тот же чемпион, различается только подпись в интерфейсе.
    slot = st.my_slot
    if slot is not None:
        st.my_role = slot.role
        st.my_champion = slot.cid
    st.ally_prepicks = [s.cid for s in st.ally_slots
                        if s.is_prepick and s.cell != st.my_cell]

    st.timer_seconds = float(timer.get("adjustedTimeLeftInPhase") or 0)
    return st


def gameflow_phase(port: str, token: str, timeout: float = 2.0) -> str:
    """Текущая фаза клиента: Lobby, ChampSelect, GameStart и т.п."""
    try:
        return str(call(GAMEFLOW_PATH, port, token, timeout=timeout))
    except Exception:                           # noqa: BLE001
        return ""


GAMEFLOW_RU = {
    "": "фаза неизвестна",
    "None": "клиент без сессии",
    "Lobby": "лобби",
    "Matches": "поиск матча",
    "ChampSelect": "драфт",
    "GameStart": "старт игры",
    "InProgress": "идёт игра",
    "WaitingForStats": "статистика",
    "PreEndOfGame": "конец игры",
    "EndOfGame": "итоги",
}

# Фазы, в которых есть эндпоинт драфта. Вне их оверлей всё равно показывает
# список, но перечитывать сессию бессмысленно — эндпоинт вернёт 404.
DRAFT_PHASES = ("ChampSelect", "Planning")


def client_status() -> dict:
    """Живой статус клиента League — то, что пользователь видит в оверлее.

    Раньше «видно ли игру» можно было узнать только по тексту ошибки, а она
    одинаково звучала и для «клиент не запущен», и для «сейчас не драфт».
    Здесь эти случаи разведены по полям, чтобы интерфейс мог показать
    конкретное состояние, а не гадать.

    level: ok — клиент в драфте, warn — запущен, но не драфт,
           error — клиент не найден или не отвечает.
    """
    out = {"running": False, "connected": False, "phase": "", "path": "",
           "level": "error", "detail": ""}

    folder = detect_league_path()
    if folder is None:
        out["detail"] = "клиент не найден — запусти League"
        return out
    out["path"] = str(folder)

    lock = folder / "Lockfile"
    if not lock.is_file():
        # Папку игры знаем, но клиент в ней не запущен: Lockfile создаётся
        # только живым клиентом. Это нормальное состояние, не ошибка.
        out["detail"] = "папка найдена, клиент не запущен"
        return out

    try:
        port, token = _credentials(lock)
    except LcuUnavailable as e:
        out["detail"] = f"Lockfile не прочитан ({e})"
        return out

    phase = gameflow_phase(port, token)
    out["running"] = True
    if not phase:
        out["detail"] = "клиент запущен, но LCU не отвечает"
        return out

    out["connected"] = True
    out["phase"] = phase
    if phase in DRAFT_PHASES:
        out["level"] = "ok"
    else:
        out["level"] = "warn"
    out["detail"] = f"клиент запущен · {GAMEFLOW_RU.get(phase, phase)}"
    return out


def fetch_session(timeout: float = 2.0) -> DraftState:
    lf = find_lockfile()
    if not lf:
        raise LcuUnavailable(
            "Lockfile не найден — клиент League не запущен, либо игра "
            "установлена на нестандартном диске. Укажи папку с игрой в файле "
            r"data\lol_path.txt рядом с программой")
    port, token = _credentials(lf)
    try:
        raw = call(SESSION_PATH, port, token, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Эндпоинт драфта существует только во время пик-бана.
            # В тренировочном режиме (подбор ботов) его нет — это ожидаемо.
            phase = gameflow_phase(port, token, timeout=timeout)
            hint = {
                "Lobby": "сейчас лобби, а не драфт",
                "ChampSelect": "драфт есть, но состав ещё пуст",
                "GameStart": "игра уже началась",
            }.get(phase, f"фаза клиента: {phase or 'неизвестна'}")
            raise LcuUnavailable(
                f"Сейчас не драфт ({hint}). Поддерживаются рангед,обычная и "
                "ARAM — на экране подбора ботов в тренировочном режиме "
                "состав через LCU недоступен.") from e
        raise LcuUnavailable(f"LCU ответил {e.code}") from e
    except Exception as e:                       # noqa: BLE001
        raise LcuUnavailable(
            f"не удалось достучаться до клиента ({type(e).__name__}: {e}). "
            f"Возможно, сессия драфта ещё не началась.") from e
    # Сырой ответ сохраняем всегда: когда драфт закончится, разбирать
    # «почему не увидел врагов» будет уже не по чему.
    from . import trace

    trace.save_session(raw)
    return parse_session(raw)


def fetch_champion_icon_index(timeout: float = 3.0) -> dict[int, str] | None:
    """championId -> путь к иконке прямо от клиента.

    Используется как сверка наших id с id клиента: если клиент уже запущен,
    его данные авторитетнее любого внешнего справочника.
    """
    try:
        lf = find_lockfile()
        if not lf:
            return None
        port, token = _credentials(lf)
        raw = call(ICON_INDEX_PATH, port, token, timeout=timeout)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return {int(k): v for k, v in raw.items() if str(k).isdigit()}
