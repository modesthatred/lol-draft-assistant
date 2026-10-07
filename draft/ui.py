"""Оверлей с тир-листом: иконки чемпионов и оценка винрейта под ними.

Требование, из которого исходит компоновка: вертикальный список от лучшего
мейна к худшему, винрейт под иконкой каждого. Поэтому строка это:

    [цветная полоса] [иконка 40x40] [имя + пояснение] [WR%]

Иконки тянутся с ddragon (120x120) и уменьшаются на subsample(3,3) —
это позволяет обойтись без Pillow, чтобы не тянуть зависимость в .exe.
"""
from __future__ import annotations

import queue
import logging
import os
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import ttk

from .champions import UA
from .settings import DEFAULT_ICONS
from .version import APP_NAME, VERSION

log = logging.getLogger(__name__)

BG = "#12151c"
FG = "#e8ecf4"
MUTED = "#8b95a8"
ACCENT = "#4a9eff"

# Цветовые границы по оценке
TONE_HIGH = "#3ddc84"
TONE_MID = "#f0c040"
TONE_LOW = "#f0603a"
TONE_NONE = "#4a5262"


ICON_PX = 40
ICON_BOX_H = 66          # иконка 40 + процент под ней + зазор

# Ширина заголовка = ширина окна минус поля рамки и место под ⚙ ×.
# Без этого переноса длинная строка («роль · враги · твой ход · 29с») растягивала
# окно вчетверо шире списка чемпионов, а при уведомлении — почти на пол-экрана.
HEADER_CHROME = 58


def _tone(wr: float | None) -> str:
    if wr is None:
        return TONE_NONE
    if wr >= 52:
        return TONE_HIGH
    if wr >= 48:
        return TONE_MID
    return TONE_LOW


class IconCache:
    """Иконки на диске + в памяти. Скачиваются лениво, но один раз."""

    def __init__(self, folder: Path = DEFAULT_ICONS):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._photo: dict[int, tk.PhotoImage] = {}

    def _path(self, cid: int) -> Path:
        return self.folder / f"{cid}.png"

    def ensure(self, cid: int, url: str) -> Path:
        p = self._path(cid)
        if p.is_file() and p.stat().st_size > 0:
            return p
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=12) as r:
                p.write_bytes(r.read())
        except Exception:                       # noqa: BLE001
            pass
        return p

    def photo(self, cid: int, url: str, size: int = 40) -> tk.PhotoImage | None:
        if cid in self._photo:
            return self._photo[cid]
        path = self.ensure(cid, url)
        if not path.is_file() or path.stat().st_size == 0:
            return None
        try:
            img = tk.PhotoImage(file=str(path))
        except Exception:                       # noqa: BLE001
            return None
        w = img.width()
        factor = max(1, round(w / size))
        if factor > 1:
            img = img.subsample(factor, factor)
        self._photo[cid] = img
        return img


class DraftOverlay:
    def __init__(self, config, on_refresh=None, on_settings=None,
                 on_quit=None):
        self.config = config
        self.on_refresh = on_refresh
        self.on_settings = on_settings
        self.on_quit = on_quit
        # вызывается, если окно снесли мимо quit_app: владелец освободит хоткей
        self.on_gone = None
        self.icons = IconCache()
        self.queue: queue.Queue = queue.Queue()
        self.visible = True
        # окно снесено: после этого любое касание Tk из чужого потока
        # (хоткей, фоновый синк) роняет интерпретатор
        self.dead = False
        self._pump_after = None

        win = config.window
        self.root = tk.Tk()
        # Заголовок окна ищет второй экземпляр, чтобы поднять это окно
        # вместо молчаливого отказа запуститься.
        self.root.title(f"{APP_NAME} {VERSION}")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", bool(config.show.get("always_on_top", True)))
        try:
            self.root.attributes("-alpha", float(win.get("opacity", 0.94)))
        except Exception:
            pass
        # overrideredirect убирает заголовок и кнопку из таскбара, поэтому
        # закрытие обязано быть собственным: кнопка ×, меню по ПКМ, Q.
        self.root.overrideredirect(True)
        self._set_noactivate()

        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=(10, 4), pady=(6, 0))

        self.header = tk.Label(top, text="", bg=BG, fg=MUTED,
                               anchor="w", font=("Segoe UI", 9),
                               justify="left", wraplength=self._wrap())
        self.header.pack(side="left", fill="x", expand=True)

        mk_btn = lambda txt, cmd, fg: tk.Label(   # noqa: E731
            top, text=txt, bg=BG, fg=fg, font=("Segoe UI", 11),
            cursor="hand2", padx=4)
        self.btn_settings = mk_btn("⚙", self.open_settings, MUTED)
        self.btn_settings.pack(side="right")
        self.btn_close = mk_btn("×", self.quit, TONE_LOW)
        self.btn_close.pack(side="right", padx=(4, 0))
        for b in (self.btn_settings, self.btn_close):
            b.bind("<Enter>", lambda e, w=b: w.configure(fg="#e8ecf4"))
            b.bind("<Leave>", lambda e, w=b: w.configure(fg=MUTED if
                                                         w is
                                                         self.btn_settings
                                                         else TONE_LOW))
            # клик по кнопке не должен ещё и тянуть окно: ButtonPress
            # отдаём "break" (прерывает перетаскивание оверлея), а команду
            # вешаем на отпускание — ButtonPress и Button-1 это одно событие.
            b.bind("<ButtonPress-1>", lambda e: "break")
        self.btn_settings.bind("<ButtonRelease-1>",
                               lambda e: self.open_settings())
        self.btn_close.bind("<ButtonRelease-1>", lambda e: self.quit())

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        self.root.bind("<ButtonPress-1>", self._drag)
        self.root.bind("<B1-Motion>", self._drag_move)
        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.bind("<space>", lambda e: self.refresh())
        self.root.bind("<q>", lambda e: self.quit())
        self.root.bind("<Button-3>", self._popup_menu)
        self._menu = tk.Menu(self.root, tearoff=0)
        self._menu.add_command(label="Обновить  (F8)",
                               command=lambda: self.refresh())
        self._menu.add_command(label="Настройки…  (⚙)",
                               command=self.open_settings)
        self._menu.add_separator()
        self._menu.add_command(label="Скрыть  (Esc)",
                               command=lambda: self.hide())
        self._menu.add_command(label="Выход  (Q)", command=self.quit)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        x, y = win.get("x"), win.get("y")
        if x is None or y is None:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = sw - int(win.get("width", 300)) - 30
            y = sh // 3
        self.root.geometry(f"+{int(x)}+{int(y)}")
        self._drag_offset = (0, 0)
        self._drag_origin = (0, 0)
        self._moved = False
        self._forced_w = False
        self.root.after(60, self._pump)
        self.root.bind("<Destroy>", self._on_destroy, add="+")

    # ---------------- ширина ----------------
    def _wrap(self) -> int:
        """Ширина переноса текста: окно минус рамка и кнопки справа."""
        try:
            base = int(self.config.window.get("width", 300))
        except (TypeError, ValueError):
            base = 300
        return max(200, base - HEADER_CHROME)

    def _fit_width(self) -> None:
        """Накрываем упрямый текст: шире половины экрана окно не растянем.

        Перенос в заголовке обычно достаточно, но если строка не влезает
        даже в две строки (или попался очень длинный ник), окно всё равно
        ушло бы вбок — тогда задаём ширину руками.
        """
        if self.dead:
            return
        try:
            self.root.update_idletasks()
            need_w = self.root.winfo_reqwidth()
            need_h = self.root.winfo_reqheight()
        except Exception:                   # noqa: BLE001
            return
        limit = max(240, min(self.root.winfo_screenwidth() // 2,
                             self._wrap() + HEADER_CHROME))
        x, y = self.root.winfo_x(), self.root.winfo_y()

        # Ширину прижимаем только когда текст не влезает, а высоту задаём
        # по содержимому КАЖДЫЙ раз. Раньше высота выставлялась один раз, при
        # первой переполненной строке, и дальше не обновлялась: блоки
        # (доска ролей, баны, билд, список мейнов) добавлялись поверх, а
        # окно оставалось прежней высоты — строки накладывались друг на
        # друга и обрезались нижним краем.
        self._forced_w = need_w > limit
        if self._forced_w:
            w = limit
        else:
            # winfo_width() у несопоставленного окна равен 1 — взять его
            # значит схлопнуть оверлей в один пиксель. Тогда отдаём
            # естественную ширину содержимого.
            cur_w = self.root.winfo_width()
            w = cur_w if cur_w > 1 else need_w
        max_h = self.root.winfo_screenheight() - 60
        h = max(1, min(need_h, max_h))
        cur_h = self.root.winfo_height()
        if w != self.root.winfo_width() or (cur_h > 1 and h != cur_h):
            self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _popup_menu(self, event):
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def open_settings(self):
        if self.on_settings:
            self.on_settings()

    def quit(self):
        # on_quit сам может снова позвать quit — не пускаем по кругу
        if getattr(self, "_quitting", False):
            return
        self._quitting = True
        if self.on_quit:
            self.on_quit()
        else:
            self.root.destroy()

    def _on_destroy(self, event):
        if event.widget is not self.root:
            return
        self.dead = True
        if self._pump_after is not None:
            try:
                self.root.after_cancel(self._pump_after)
            except Exception:                   # noqa: BLE001
                pass
            self._pump_after = None
        if self._moved:
            self._remember_pos()
        # окно снесли не через quit_app — сообщаем владельцу, чтобы он
        # освободил хоткей, иначе он останется висеть в системе
        if self.on_gone:
            try:
                self.on_gone()
            except Exception:                   # noqa: BLE001
                log.debug("on_gone failed")

    # ---------------- перетаскивание ----------------
    def _drag(self, event):
        self._drag_origin = (event.x_root, event.y_root)

    def _drag_move(self, event):
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        self.root.geometry(f"+{self.root.winfo_x() + dx}+"
                           f"{self.root.winfo_y() + dy}")
        self._drag_origin = (event.x_root, event.y_root)
        self._moved = True
        # геометрия только с позицией снимает ручную ширину — помечаем,
        # иначе _fit_width решит, что ширина уже зажата, и не пережмёт её
        self._forced_w = False

    def _remember_pos(self):
        """Сохраняем позицию, чтобы окно не прыгало при следующем запуске."""
        try:
            self.config.window["x"] = self.root.winfo_x()
            self.config.window["y"] = self.root.winfo_y()
            self.config.save()
        except Exception:                    # noqa: BLE001
            pass

    # ---------------- обновление ----------------
    def refresh(self) -> None:
        if self.on_refresh:
            self.on_refresh()

    def _pump(self) -> None:
        if self.dead:
            return
        try:
            while True:
                payload = self.queue.get_nowait()
                self._render(*payload)
        except queue.Empty:
            pass
        if not self.dead:
            self._pump_after = self.root.after(40, self._pump)

    def _set_noactivate(self) -> None:
        """Окно не должно всплывать поверх клиента и забирать фокус кликом.

        Без этого оверлей при показе/клике уводил фокус от игры — посреди
        катки случайный тик по билду мог украсть управление, а клиент игры
        этого не прощает. WS_EX_NOACTIVATE запрещает окну становиться
        активным: оно остаётся наверху (topmost), но фокус не трогает.
        Настройки и меню — отдельные Toplevel, их это не касается.
        """
        if os.name != "nt":
            return
        try:
            import ctypes

            hwnd = self.root.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  style | WS_EX_NOACTIVATE)
        except Exception:                       # noqa: BLE001
            log.debug("не могу снять активацию окна", exc_info=True)

    def show(self):
        if self.dead or self.visible:
            return
        try:
            self.root.deiconify()
        except Exception:                   # noqa: BLE001
            log.debug("show() after destroy, пропускаем")
            return
        self.visible = True

    def hide(self):
        if self.dead:
            return
        self.root.withdraw()
        self.visible = False

    def toggle(self):
        self.hide() if self.visible else self.show()

    def post(self, picks, header: str, build=None, extra=None):
        self.queue.put(("picks", picks, header, build, extra))

    def post_error(self, text: str, extra=None):
        self.queue.put(("error", [], text, None, extra))

    def post_status(self, text: str, extra=None):
        """Нейтральное сообщение: автосинк, ожидание, подсказка."""
        self.queue.put(("status", [], text, None, extra))

    # ---------------- отрисовка ----------------
    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _render(self, kind, picks, header: str, build=None, extra=None):
        self.header.configure(text=header)
        self._clear()
        extra = extra or {}
        # Статус и доска ролей идут первыми: без них остальной текст
        # нечитаем — непонятно, читаем ли мы вообще правильный состав.
        self._render_status(extra)
        self._render_role(extra)
        self._render_board(extra)
        self._render_bans(extra)
        self._render_ban_advice(extra)
        self._render_build(build)
        if not picks:
            # билд предметов — тоже содержимое, поэтому «нет данных» оставляем
            # только когда показать нечего вообще
            if build:
                return
            if kind == "error":
                msg, fg = header, TONE_LOW
            elif kind == "status":
                msg, fg = header, MUTED
            else:
                msg, fg = "нет данных — обнови статистику", MUTED
            tk.Label(self.body, text=msg, bg=BG, fg=fg,
                     font=("Segoe UI", 10), justify="left",
                     wraplength=self._wrap()).pack(anchor="w")
            self._render_footer(extra)
            self._fit_width()
            return
        for i, p in enumerate(picks, 1):
            self._render_row(i, p)
        offs = extra.get("off_pool") or []
        if offs:
            tk.Label(self.body, text="вне пула", bg=BG, fg=ACCENT,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w",
                                                        padx=6, pady=(6, 0))
            if not any(p.data_ok for p in offs):
                tk.Label(self.body, text="пока по мете роли — враги не видны",
                         bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(
                             anchor="w", padx=6)
            for i, p in enumerate(offs, 1):
                self._render_row(i, p)
        self._render_footer(extra)
        self._fit_width()

    def _render_status(self, extra) -> None:
        """Виден ли клиент и в какой он фазе.

        Раньше это выяснялось только из текста ошибки, а она звучала одинаково
        и для «клиент не запущен», и для «сейчас не драфт». Теперь состояние
        видно всегда, даже когда драфт читается нормально.
        """
        st = extra.get("status")
        if not st:
            return
        fg = {"ok": TONE_HIGH, "warn": TONE_MID}.get(st.get("level"),
                                                     TONE_LOW)
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", pady=(0, 2))
        tk.Label(row, text="●", bg=BG, fg=fg,
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Label(row, text=st.get("detail", ""), bg=BG, fg=fg,
                 font=("Segoe UI", 8), anchor="w",
                 wraplength=max(160, self._wrap() - 14),
                 justify="left").pack(side="left", fill="x", expand=True)
        if st.get("level") == "error" and st.get("path"):
            tk.Label(row, text="⚙ путь", bg=BG, fg=ACCENT,
                     font=("Segoe UI", 8),
                     cursor="hand2").pack(side="right")

    def _render_role(self, extra) -> None:
        """Какая роль считается сейчас и откуда она взята."""
        role = extra.get("role")
        if not role:
            return
        from .lcu import ROLE_RU

        auto = extra.get("role_auto", False)
        src = "по драфту" if auto else "вручную"
        manual = extra.get("role_manual", "")
        txt = f"роль: {ROLE_RU.get(role, role)} · {src}"
        if extra.get("role_fallback"):
            txt = f"роль: {ROLE_RU.get(role, role)} · пула нет, взят {manual}"
        tk.Label(self.body, text=txt, bg=BG,
                 fg=ACCENT if auto else MUTED, font=("Segoe UI", 9),
                 anchor="w", wraplength=self._wrap(),
                 justify="left").pack(anchor="w")

    def _board_line(self, title, rows, accent_row=-1) -> None:
        """Строка «наши»/«враги»: позиция + чемпион, пре-пик помечен."""
        from .lcu import ROLE_RU

        parts = []
        for i, r in enumerate(rows):
            name, role, prepick, mine = r
            tag = ROLE_RU.get(role, "?")
            if name:
                cell = f"{tag} {name}"
                if mine:
                    cell += " ◀"
                elif prepick:
                    cell += " ~"
            else:
                cell = f"{tag} —"
            parts.append((i == accent_row, cell))
        line = tk.Frame(self.body, bg=BG)
        line.pack(fill="x")
        tk.Label(line, text=f"{title} ", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        body = " · ".join(c for _, c in parts)
        tk.Label(line, text=body, bg=BG,
                 fg=TONE_MID if prepick else FG,
                 font=("Segoe UI", 9), anchor="w",
                 wraplength=max(160, self._wrap() - 40),
                 justify="left").pack(side="left", fill="x", expand=True)

    def _render_board(self, extra) -> None:
        """Состав обеих команд по ролям: кто уже выбран, кто пре-пик."""
        board = extra.get("board") or {}
        allies = board.get("allies") or []
        enemies = board.get("enemies") or []
        if not allies and not enemies:
            return
        mine_cell = board.get("my_cell", -1)
        self._board_line("наши", allies, accent_row=mine_cell)
        self._board_line("враги", enemies)

    def _render_bans(self, extra) -> None:
        """Баны обеих команд.

        Бан союзников тоже влияет на выбор: предложить пика, которого ваша
        команда сама запретила, бессмысленно.
        """
        bans = extra.get("bans") or {}
        ally = bans.get("ally") or []
        enemy = bans.get("enemy") or []
        if not ally and not enemy:
            return
        parts = []
        if ally:
            parts.append(("наши", ", ".join(ally), MUTED))
        if enemy:
            parts.append(("враги", ", ".join(enemy), TONE_LOW))
        line = tk.Frame(self.body, bg=BG)
        line.pack(fill="x", pady=(2, 0))
        tk.Label(line, text="баны ", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        for i, (who, who_list, fg) in enumerate(parts):
            if i:
                tk.Label(line, text=" · ", bg=BG, fg=MUTED,
                         font=("Segoe UI", 9)).pack(side="left")
            tk.Label(line, text=who, bg=BG, fg=MUTED,
                     font=("Segoe UI", 8)).pack(side="left")
            tk.Label(line, text=who_list, bg=BG, fg=fg,
                     font=("Segoe UI", 8), wraplength=max(140,
                                                          self._wrap() - 60),
                     justify="left").pack(side="left", fill="x",
                                          expand=True)

    def _render_ban_advice(self, extra) -> None:
        """Кого банить на своей позиции.

        Бан — единственный ход вне своего пула, поэтому здесь нужен отдельный
        список: показывать мейны, которых запретить нельзя, незачем.
        """
        advice = extra.get("ban_advice") or []
        if not advice:
            return
        box = tk.Frame(self.body, bg="#1a2130")
        box.pack(fill="x", pady=(6, 2))
        tk.Label(box, text="кого банить", bg="#1a2130", fg=ACCENT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w",
                                                     padx=6, pady=(4, 2))
        for a in advice:
            row = tk.Frame(box, bg="#1a2130")
            row.pack(fill="x", padx=6, pady=1)
            url = a.get("icon") or ""
            photo = (self.icons.photo(f"b{a['cid']}", url, size=20)
                     if url and a.get("cid") else None)
            if photo is not None:
                holder = tk.Label(row, image=photo, bg="#1a2130")
                holder.image = photo
                holder.pack(side="left")
            else:
                tk.Label(row, text="•", bg="#1a2130", fg=MUTED,
                         font=("Segoe UI", 10)).pack(side="left")
            tk.Label(row, text=a.get("name", ""), bg="#1a2130", fg=FG,
                     font=("Segoe UI", 10, "bold")).pack(side="left",
                                                         padx=(4, 0))
            note = a.get("note") or ""
            if note:
                tk.Label(row, text=note, bg="#1a2130", fg=MUTED,
                         font=("Segoe UI", 8), anchor="w",
                         wraplength=max(120, self._wrap() - 90),
                         justify="left").pack(side="left", padx=(6, 0))

    def _render_footer(self, extra=None) -> None:
        """Нижний бар: свежесть данных и ход обновления.

        Пара «данные/лига» отвечает на вопрос «не устарел ли расчёт», строка
        с прогрессом — «делает ли фон сейчас что-то». Раньше было только
        «обновляю статистику» текстом без дат и чисел, поэтому запущенный
        сразу после патча синк выглядел как молчащее приложение.
        """
        from .version import VERSION

        footer = (extra or {}).get("footer") or {}
        if footer:
            data_patch = str(footer.get("data_patch") or "—")
            league_patch = str(footer.get("league_patch") or "—")
            running = bool(footer.get("running"))
            fresh = bool(data_patch != "—" and data_patch == league_patch)
            stage = footer.get("stage") or "обновляю статистику…"
            if stage.strip() and footer.get("net") is not False:
                stage = "⬇ OP.GG · " + stage
            try:
                done, total = int(footer.get("done") or 0), \
                    int(footer.get("total") or 0)
            except (TypeError, ValueError):
                done = total = 0
            try:
                eta = int(footer.get("eta") or 0)
            except (TypeError, ValueError):
                eta = 0

            bar = tk.Frame(self.body, bg=BG)
            bar.pack(fill="x", pady=(4, 0))
            if data_patch != "—" or league_patch != "—":
                row = tk.Frame(bar, bg=BG)
                row.pack(fill="x")
                tk.Label(row, text="данные ", bg=BG, fg=MUTED,
                         font=("Segoe UI", 8)).pack(side="left")
                fg = ACCENT if fresh else TONE_LOW
                tk.Label(row, text=data_patch, bg=BG, fg=fg,
                         font=("Segoe UI", 8, "bold")).pack(side="left")
                tk.Label(row, text="  лига ", bg=BG, fg=MUTED,
                         font=("Segoe UI", 8)).pack(side="left")
                tk.Label(row, text=league_patch, bg=BG, fg=fg,
                         font=("Segoe UI", 8, "bold")).pack(side="left")
                if not fresh and not running:
                    tk.Label(row, text="  ⚡ обнови", bg=BG, fg=TONE_LOW,
                             font=("Segoe UI", 8, "bold")).pack(side="left")
            reason = footer.get("stale_reason")
            if reason and not running:
                tk.Label(bar, text=str(reason), bg=BG, fg=TONE_MID,
                         font=("Segoe UI", 8), anchor="w",
                         wraplength=self._wrap(),
                         justify="left").pack(anchor="w", pady=(2, 0))
            if running:
                tk.Label(bar, text=stage, bg=BG, fg=MUTED,
                         font=("Segoe UI", 8), anchor="w",
                         wraplength=self._wrap(),
                         justify="left").pack(anchor="w")
                # Без таймера start(): бар пересоздаётся на каждый рендер,
                # а внутренний after() снесённого виджета роняет Tcl-ошибку.
                # Нет счётчиков (фаза пула) — полоса просто стоит пустой,
                # стадию показывает текст выше.
                pbar = ttk.Progressbar(bar, maximum=max(total, 1),
                                       value=done,
                                       length=max(160, self._wrap() - 20))
                pbar.pack(fill="x", pady=(2, 0))
                if total:
                    right = f"{done}/{total}"
                    if running and eta:
                        if eta >= 60:
                            right += f" · осталось ≈{eta // 60}:{eta % 60:02d}"
                        else:
                            right += f" · осталось ≈{eta} сек"
                    tk.Label(bar, text=right, bg=BG, fg=MUTED,
                             font=("Segoe UI", 7)).pack(anchor="e")

        tk.Label(self.body, text=f"v{VERSION}", bg=BG, fg="#4a5262",
                 font=("Segoe UI", 7)).pack(anchor="e", pady=(6, 0))

    def _render_build(self, build) -> None:
        """Блок предметов для пика, который уйдёт в игру.

        Блоки появляются сразу после пика, а не в конце драфта: пока ты
        выбираешь чемпиона, пересобирать список смысла нет.
        """
        if not build:
            return
        line = tk.Frame(self.body, bg=BG)
        line.pack(fill="x", pady=(0, 6))
        tag = "пик" if build.get("locked") else "наведение"
        tk.Label(line, text=f"{build.get('name') or ''}  ·  {tag}",
                 bg=BG, fg=ACCENT if build.get("locked") else MUTED,
                 font=("Segoe UI", 9, "bold")).pack(side="left")

        entries = list(build.get("items") or [])
        boots = build.get("boots")
        if boots:
            entries = entries + [boots]
        for n, it in enumerate(entries, 1):
            self._render_item(line, it, last=boots is not None and n == len(entries))

        notes = build.get("situational") or []
        if notes:
            tag_txt = {
                "anti_heal": "антихил против",
                "anti_shield": "щиты против",
                "vs_ap": "магический состав",
                "vs_ad": "физ. состав",
            }
            sub = tk.Frame(self.body, bg=BG)
            sub.pack(fill="x", pady=(0, 2))
            tk.Label(sub, text="под драфт ▸", bg=BG, fg=ACCENT,
                     font=("Segoe UI", 8, "bold")).pack(side="left",
                                                        padx=(6, 0))
            first = True
            for note in notes:
                head = tag_txt.get(note.get("tag"), note.get("tag", ""))
                if not first:
                    tk.Label(sub, text=" ‖ ", bg=BG, fg=MUTED,
                             font=("Segoe UI", 8)).pack(side="left")
                tk.Label(sub, text=f"{head}: {note.get('why', '')}",
                         bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(
                             side="left")
                tk.Label(sub, text=f"→ {note.get('item', '')}", bg=BG,
                         fg=TONE_HIGH, font=("Segoe UI", 8)).pack(side="left")
                first = False

    def _render_item(self, parent, item: dict, last: bool = False) -> None:
        iid = item.get("item_id") or 0
        cell = tk.Frame(parent, bg=BG)
        cell.pack(side="left", padx=(6 if iid else 0, 0))
        url = item.get("url") or ""
        photo = (self.icons.photo(f"it{iid}", url, size=22)
                 if url and iid else None)
        if photo is not None:
            tk.Label(cell, image=photo, bg=BG).pack()
        else:
            # иконка не скачалась — не молчим, а пишем номер предмета
            tk.Label(cell, text=str(iid) if iid else "?", bg=BG, fg=MUTED,
                     font=("Consolas", 8), width=3).pack()
        tk.Label(cell, text=item.get("name") or "", bg=BG, fg=MUTED,
                 font=("Segoe UI", 7)).pack()
        if not last:
            tk.Label(cell, text="+", bg=BG, fg=MUTED,
                     font=("Segoe UI", 8)).pack()

    def _render_row(self, rank: int, p):
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", pady=2)

        wr = None if not p.data_ok else p.est_winrate
        color = _tone(wr)

        bar = tk.Frame(row, bg=color, width=3, height=44)
        bar.pack(side="left", fill="y")

        icon_box = tk.Frame(row, bg=BG, width=ICON_PX + 4,
                            height=ICON_BOX_H)
        icon_box.pack(side="left")
        icon_box.pack_propagate(False)          # без этого процент под иконкой
        img = self.icons.photo(p.cid, p.icon, ICON_PX) if \
            self.config.show.get("icons", True) else None
        holder = tk.Label(icon_box, bg=BG, width=ICON_PX, height=ICON_PX,
                          image=img or "", text="" if img else "?")
        if img:
            holder.image = img
        holder.pack(anchor="w")

        wr_text = "—" if wr is None else f"{wr:.1f}%"
        tk.Label(icon_box, text=wr_text, bg=BG, fg=color,
                 font=("Consolas", 9, "bold")).pack()

        mid = tk.Frame(row, bg=BG)
        mid.pack(side="left", fill="x", expand=True, padx=(8, 0))

        name = p.name
        if p.banned_by_enemy:
            name += "  [забанен]"
        tk.Label(mid, text=name, bg=BG, fg=FG if not p.banned_by_enemy else TONE_LOW,
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(anchor="w")

        if self.config.show.get("comp_notes", True):
            sub = _subtitle(p)
            if sub:
                tk.Label(mid, text=sub, bg=BG, fg=MUTED,
                         font=("Segoe UI", 8), anchor="w").pack(anchor="w")

        right = tk.Frame(row, bg=BG)
        right.pack(side="right")
        tk.Label(right, text=f"#{rank}", bg=BG, fg=MUTED,
                 font=("Consolas", 9)).pack()


def _subtitle(p) -> str:
    bits = []
    if p.banned_by_enemy:
        bits.append("забанен врагами")
    elif p.banned_by_ally:
        bits.append("забанен нами")
    elif p.ban_risk >= 15:
        bits.append(f"банят {p.ban_risk:.0f}%")
    if p.notes:
        bits.extend(p.notes)
    if p.confidence < 0.5 and p.data_ok:
        bits.append(f"данных {p.confidence_pct}%")
    return " · ".join(bits[:3])
