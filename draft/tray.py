"""Значок в трее и поиск окна уже запущенного экземпляра.

Зачем это нужно: оверлей сделан без системного заголовка (overrideredirect),
поэтому в таскбаре его не видно, а в трее значка не было. Итог — копия
программы могла молча работать, а найти и закрыть её было нечем: только
всплывал диалог «приложение уже запущено».

Реализовано на чистом ctypes: pystray тянет PIL и pyside, а ради трёх пунктов
меню это лишние 40 МБ в .exe. Здесь только user32/shell32, без зависимостей.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# На 64-битной Windows HWND/HMENU — 64 бита. Без явных прототипов ctypes
# передаёт их как C int, и любой указатель выше 0x7FFFFFFF обрезается:
# окно находится, но «не наше», а значок появляется не туда.
_LRESULT = ctypes.c_longlong
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)

user32.DefWindowProcW.restype = _LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.CreatePopupMenu.argtypes = []
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t,
                               wintypes.LPCWSTR]
user32.TrackPopupMenuEx.restype = wintypes.UINT
user32.TrackPopupMenuEx.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                    ctypes.c_int, wintypes.HWND,
                                    wintypes.LPCVOID]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.restype = wintypes.HWND
# Shell_NotifyIconW живёт в shell32, а не в user32.
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]

NIM_ADD, NIM_DELETE, NIM_SETVERSION = 0, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
NIF_INFO = 0x10
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_COMMAND, WM_DESTROY, WM_LBUTTONDBLCLK = 0x111, 0x02, 0x203

TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
MENU_RESTORE, MENU_QUIT = 40001, 40002
LOADICON = None  # прототип LoadIconW задаётся в _icon_handle


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _NOTIFYICONDATA(ctypes.Structure):
    """NOTIFYICONDATAW. Размер задаём явно: на 64 битах структура
   WinAPI шире, чем подсказка ctypes по умолчанию."""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


def _icon_handle():
    """Системная иконка приложения вместо своей картинки.

    Свою иконку в .exe мы не встраиваем, а иконка Windows есть всегда и не
    требует файла на диске. IDI_APPLICATION передаётся как MAKEINTRESOURCE —
    это обычный «адрес-число», поэтому первый аргумент у LoadIconW тоже
    должен быть указателем, а не int.
    """
    load = user32.LoadIconW
    load.restype = wintypes.HANDLE
    load.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    for res in (32512, 43, 13):        # приложение, приложение, папка
        h = load(None, ctypes.c_void_p(res))
        if h:
            return h
    return None


def surface_existing(title: str) -> bool:
    """Показать окно уже запущенного экземпляра.

    Вызывается вторым экземпляром, который не смог взять мьютекс. Раньше он
    просто показывал диалог, а живое окно оставалось где было — часто за
    другими окнами. Здесь мы ищем окно по заголовку и поднимаем его.
    """
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


class TrayIcon:
    """Значок в трее: показать/скрыть, настройки, выход.

    Живёт на скрытом message-only окне, которое само обрабатывает клики —
    главное окно Tk для этого не годится (оно и так без заголовка).

    В процессе допустим только один трей: значок принадлежит приложению, а
    не окну настроек, и второму экземпляру он не нужен.
    """

    WNDCLASS_NAME = "LoLDraftAssistantTray"

    def __init__(self, tip: str, on_restore, on_quit):
        self.on_restore = on_restore
        self.on_quit = on_quit
        self.tip_text = tip
        self.class_name = f"{self.WNDCLASS_NAME}{id(self)}"
        self._class_registered = False
        self._hwnd = None
        self._nid = None
        self._added = False
        self._old_wndproc = None
        self._proc_ref = None

    # ---------- окно ----------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam == WM_LBUTTONDBLCLK:
                self.on_restore()
            elif lparam == WM_RBUTTONUP or lparam == WM_COMMAND:
                self._popup(hwnd)
            return 0
        if msg == WM_DESTROY:
            self._remove()
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup(self, hwnd):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, MENU_RESTORE, "Показать оверлей")
        user32.AppendMenuW(menu, 0, MENU_QUIT, "Выход")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # Ужин по центру курсора, иначе меню уезжает на второй монитор.
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenuEx(menu,
                                      TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                      pt.x, pt.y, hwnd, None)
        user32.DestroyMenu(menu)
        if cmd == MENU_RESTORE:
            self.on_restore()
        elif cmd == MENU_QUIT:
            self.on_quit()

    def _install_class(self):
        wc = WNDCLASSW()
        wc.style = 0x0008                      # CS_DCLOAK, окно невидимо
        wc.lpfnWndProc = self._proc_ref
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = self.class_name
        # Имя класса уникально на экземпляр: повторная регистрация того же
        # класса в том же процессе падает, а класс живёт до конца процесса.
        # Раньше из-за этого второй экземпляр (и тесты) не получали трей,
        # а скрытое окно оставалось жить и рушило Tk при выходе.
        if user32.RegisterClassW(ctypes.byref(wc)):
            self._class_registered = True
            return True
        # ERROR_CLASS_ALREADY_EXISTS (1410) — окно создать можно.
        return ctypes.get_last_error() == 1410

    def start(self) -> bool:
        if _ACTIVE.get("tray") is not None:
            return False                    # трей в процессе уже есть
        hinst = kernel32.GetModuleHandleW(None)
        self._proc_ref = _WNDPROC(self._wndproc)
        if not self._install_class():
            return False
        hwnd = user32.CreateWindowExW(
            0, self.class_name, "LoLDraftAssistantTray", 0, 0, 0, 0, 0,
            None, None, hinst, None)
        if not hwnd:
            return False
        self._hwnd = hwnd

        nid = _NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = _icon_handle()
        nid.szTip = self.tip_text
        self._nid = nid
        if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self._added = True
            _ACTIVE["tray"] = self
        return self._added

    def _remove(self):
        if self._nid is not None and self._added:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._added = False

    def tip(self, text: str) -> None:
        """Обновить подсказку (например, показать в ней роль и состав)."""
        if self._nid is None or not self._added:
            return
        self.tip_text = text
        self._nid.szTip = text
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid))

    def stop(self) -> None:
        self._remove()
        if _ACTIVE.get("tray") is self:
            _ACTIVE["tray"] = None
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        if self._class_registered:
            user32.UnregisterClassW(self.class_name,
                                    kernel32.GetModuleHandleW(None))
            self._class_registered = False
        # Ссылку на WNDPROC держим до конца: уничтожение последней ссылки
        # на callback, пока окно ещё живо, приводит к падению при выходе.
        self._proc_ref = None


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND,
                                           wintypes.UINT, wintypes.WPARAM,
                                           wintypes.LPARAM)),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


kernel32 = ctypes.windll.kernel32
WM_RBUTTONUP = 0x205

# Трей в процессе один: приложение, а не окно настроек, им владеет.
_ACTIVE: dict = {"tray": None}