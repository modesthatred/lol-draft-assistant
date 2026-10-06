"""Глобальный хоткей через Win32 RegisterHotKey.

Своя реализация на ctypes вместо сторонних библиотек: не нужны права
администратора и не нужен pip install, что важно для сборки в .exe.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

MOD_NOREPEAT = 0x4000
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_VK = {f"F{i}": 0x6F + i for i in range(1, 25)}          # F1..F24 -> 0x70..0x87
for _i in range(26):                                      # A..Z
    _VK[chr(ord("A") + _i)] = 0x41 + _i
for _i in range(10):                                      # 0..9
    _VK[str(_i)] = 0x30 + _i
_VK.update({"SPACE": 0x20, "TAB": 0x09, "PAUSE": 0x13,
            "SCROLLLOCK": 0x91, "INSERT": 0x2D, "HOME": 0x24,
            "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
            "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27})


def parse_hotkey(spec: str) -> tuple[int, int] | None:
    """'F8' -> (mods, vk). Поддерживает 'ctrl+alt+F9'."""
    if not spec:
        return None
    mods = 0
    parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    key = parts[-1] if parts else ""
    for m in parts[:-1]:
        if m in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif m in ("alt",):
            mods |= MOD_ALT
        elif m in ("shift",):
            mods |= MOD_SHIFT
        else:
            return None
    vk = _VK.get(key.upper())
    if vk is None:
        return None
    return mods, vk


class HotkeyListener:
    """Отдельный поток с собственным циклом сообщений Windows."""

    def __init__(self, spec: str, callback):
        self.spec = spec
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._tid: int | None = None
        self._ready = threading.Event()
        self.error: str | None = None
        self._stop = threading.Event()

    def start(self) -> bool:
        parsed = parse_hotkey(self.spec)
        if parsed is None:
            self.error = f"не понял хоткей {self.spec!r}"
            return False
        mods, vk = parsed
        self._thread = threading.Thread(target=self._run, args=(mods, vk),
                                        daemon=True)
        self._thread.start()
        self._ready.wait(2.0)
        return self.error is None

    def _run(self, mods: int, vk: int) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
            self.error = (f"хоткей {self.spec} уже занят другой программой")
            self._ready.set()
            return
        self._tid = kernel32.GetCurrentThreadId()
        self._ready.set()
        try:
            msg = wt.MSG()
            while not self._stop.is_set():
                if not user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x1):
                    self._stop.wait(0.05)
                    continue
                if msg.message == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:            # noqa: BLE001
                        pass
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        """Гасим поток и ДОЖИДАЕМСЯ его.

        join обязателен: пока callback не вернулся, он может дёргать Tk, а
        вызывающий к этому моменту уже уничтожил окно. Возвращаться раньше
        означает «я не знаю, в каком состоянии окно».
        """
        self._stop.set()
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        t, self._thread = self._thread, None
        if t is not None and t.is_alive():
            t.join(1.0)
        self.callback = lambda: None      # late callback больше не страшен
