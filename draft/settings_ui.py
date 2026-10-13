"""Окно настроек: роль, пул мейнов, хоткей, веса, аккаунты, источники.

Отдельный Toplevel, потому что оверлей намеренно работает без заголовка
(overrideredirect) и не должен быть местом для редактирования конфига.

Вёрстка: тело окна прокручиваемое (Canvas + Frame), кнопки «Сохранить» и
«Отмена» прибиты к низу ВНЕ прокрутки. Жалоба была ровно эта: при маленьком
окне кнопки уезжали под низ и до них нельзя было добраться. Прокрутка по
колёсику мыши и по полосе.
"""
from __future__ import annotations

import tkinter as tk
import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk

from . import champions as ch
from . import ugg
from .cache import Cache
from .settings import VALID_ROLES

TONE_LOW = "#f0603a"
TONE_OK = "#7fd18a"

ROLE_RU = {
    "top": "Топ",
    "jungle": "Лес",
    "mid": "Мид",
    "adc": "Бот",
    "support": "Саппорт",
}

BG = "#12151c"
FG = "#e8ecf4"
MUTED = "#8b95a8"
ACCENT = "#f0c040"
TXT_BG = "#1b1f2a"

# Пул не обязан быть маленьким: у саппорта легко 10-12 мейнов под разные
# матчапы. Больше — чуть дольше синк и расчёт, но МЕДЛЕННЕЕ оверлей не станет:
# кандидаты считаются по одной и той же формуле, просто их больше.
MAX_POOL = 12


def attach_entry_menu(entry) -> None:
    """Правый клик по полю ввода: Копировать / Вставить / Вырезать.

    Windows-Tk не даёт Entry своего контекстного меню, а вставка привычным
    правым кликом — то, чего пользователь ждёт от любого поля. Ctrl+V при
    этом продолжает работать.
    """
    menu = tk.Menu(entry, tearoff=0, bg=TXT_BG, fg=FG)
    menu.add_command(label="Вырезать",
                     command=lambda: entry.event_generate("<<Cut>>"))
    menu.add_command(label="Копировать",
                     command=lambda: entry.event_generate("<<Copy>>"))
    menu.add_command(label="Вставить",
                     command=lambda: entry.event_generate("<<Paste>>"))

    def show(evt):
        try:
            menu.tk_popup(evt.x_root, evt.y_root)
        finally:
            menu.grab_release()

    entry.bind("<Button-3>", show)

# Источники данных: подпись в настройках -> ключ в конфиге.
SOURCES = [
    ("op.gg — контрпики, синергии, билды предметов", "opgg"),
    ("lolalytics — мета-предметы по линии (второе мнение)", "lolalytics"),
    ("u.gg — личная история (мои предметы и проигрыши)", "ugg"),
]


class SettingsWindow:
    """Модальное по смыслу, но не блокирующее окно настроек."""

    def __init__(self, parent, config, cache: Cache, champs: dict,
                 on_save=None):
        self.config = config
        self.cache = cache
        self.champs = champs
        self.index = ch.build_index(champs)
        self.on_save = on_save
        self.selected: dict[int, str] = {}
        self._current: list[str] = []
        # строки аккаунтов: [{region, name, tag, status, mark}]
        self._acc_rows: list[dict] = []
        self._source_vars: dict[str, tk.BooleanVar] = {}

        self.top = tk.Toplevel(parent)
        self.top.title("Настройки — LoL Draft Assistant")
        self.top.configure(bg=BG)
        self.top.transient(parent)
        self.top.resizable(True, True)
        self.top.geometry("820x720")
        self.top.minsize(560, 420)
        self.top.grab_set()

        self._build_shell()
        self._build()
        self._load()

    # ---------------- каркас: скролл + зафиксированные кнопки ----------------
    def _build_shell(self) -> None:
        holder = tk.Frame(self.top, bg=BG)
        holder.pack(fill="both", expand=True)

        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=body, anchor="nw")

        def _sync_scroll(_evt=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _fit(_evt=None):
            # содержимое тянем по ширине канвы, иначе секции болтаются
            canvas.itemconfigure(win, width=_evt.width)

        body.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _fit)
        # колёсико: крутим, только когда окно настроек реально активно
        def _wheel(evt):
            try:
                canvas.yview_scroll(-evt.delta // 120, "units")
            except tk.TclError:
                pass
        canvas.bind("<Enter>", lambda e: canvas.bind_all(
            "<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        self.body = body
        self._canvas = canvas

        btns = tk.Frame(self.top, bg=BG)
        btns.pack(fill="x", padx=14, pady=(0, 12))
        b_save = tk.Label(btns, text="Сохранить", bg=ACCENT, fg="#12151c",
                          font=("Segoe UI", 10, "bold"), cursor="hand2",
                          padx=16, pady=5)
        b_save.pack(side="right")
        b_cancel = tk.Label(btns, text="Отмена", bg=TXT_BG, fg=FG,
                            cursor="hand2", font=("Segoe UI", 10), padx=16,
                            pady=5)
        b_cancel.pack(side="right", padx=(0, 8))
        b_save.bind("<Button-1>", lambda e: self._save())
        b_cancel.bind("<Button-1>", lambda e: self.top.destroy())

    def _w(self) -> int:
        """Ширина тела для wraplength — чтобы переносы not зависели от окна."""
        return max(360, self.body.winfo_width() or 700)

    # ---------------- построение ----------------
    def _build(self) -> None:
        pad = {"padx": 14, "pady": 6}

        tk.Label(self.body, text="Основная роль", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        self.role = tk.StringVar()
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", padx=14)
        for r in VALID_ROLES:
            ttk.Radiobutton(row, text=ROLE_RU[r], value=r,
                            variable=self.role).pack(side="left", padx=(0, 8))

        tk.Label(self.body, text="Дополнительная роль (автофилл)",
                 bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        self.role2 = tk.StringVar()
        r2row = tk.Frame(self.body, bg=BG)
        r2row.pack(fill="x", padx=14)
        ttk.Radiobutton(r2row, text="нет", value="",
                        variable=self.role2).pack(side="left", padx=(0, 8))
        for r in VALID_ROLES:
            ttk.Radiobutton(r2row, text=ROLE_RU[r], value=r,
                            variable=self.role2).pack(side="left",
                                                      padx=(0, 8))
        tk.Label(self.body,
                 text="заполняют тебя на вторую роль (например, саппорт) — "
                      "считаем по её пулу",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8), wraplength=self._w(),
                 justify="left").pack(anchor="w", padx=14)

        tk.Label(self.body, text="Пул", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        self.pool_mode = tk.StringVar(value="per_role")
        mrow = tk.Frame(self.body, bg=BG)
        mrow.pack(fill="x", padx=14)
        ttk.Radiobutton(mrow, text="общий для всех ролей", value="shared",
                        variable=self.pool_mode).pack(side="left",
                                                      padx=(0, 12))
        ttk.Radiobutton(mrow, text="по ролям", value="per_role",
                        variable=self.pool_mode).pack(side="left")
        self.pool_mode.trace_add("write", lambda *a: self._on_pool_mode())

        # «Пул какой роли редактируем» — виден только в режиме «по ролям»
        self.pool_section = tk.Frame(self.body, bg=BG)
        self.pool_section.pack(fill="x")
        tk.Label(self.pool_section, text="Пул какой роли редактируем",
                 bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        self.pool_role = tk.StringVar()
        prow = tk.Frame(self.pool_section, bg=BG)
        prow.pack(fill="x", padx=14)
        for r in VALID_ROLES:
            ttk.Radiobutton(prow, text=ROLE_RU[r], value=r,
                            variable=self.pool_role).pack(side="left",
                                                          padx=(0, 8))
        self.pool_role.trace_add("write", lambda *a: self._load_pool())
        tk.Label(self.pool_section,
                 text="леснику не подмешается Люкс: у каждой роли свой набор "
                      "мейнов и свой матчап",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8), wraplength=self._w(),
                 justify="left").pack(anchor="w", padx=14)

        self._main_label = tk.Label(self.body,
                                    text=f"Твои мейны (до {MAX_POOL})",
                                    bg=BG, fg=FG,
                                    font=("Segoe UI", 10, "bold"))
        self._main_label.pack(anchor="w", **pad)
        tk.Label(self.body,
                 text=f"выбрано {len(self.selected)}/{MAX_POOL} — "
                      f"порядок не важен",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w",
                                                             padx=14)

        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *a: self._filter())
        ent = tk.Entry(self.body, textvariable=self.search, bg=TXT_BG, fg=FG,
                       insertbackground=FG, font=("Segoe UI", 10),
                       relief="flat")
        attach_entry_menu(ent)
        ent.pack(fill="x", padx=14, pady=(4, 6))

        mid = tk.Frame(self.body, bg=BG)
        mid.pack(fill="both", expand=True, padx=14)

        self.listbox = tk.Listbox(mid, bg=TXT_BG, fg=FG, width=42, height=14,
                                  selectmode="extended", relief="flat",
                                  font=("Segoe UI", 10),
                                  highlightthickness=1,
                                  highlightbackground="#2a3040",
                                  exportselection=False)
        self.listbox.pack(side="left", fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        side = tk.Frame(mid, bg=BG)
        side.pack(side="left", fill="y", padx=(8, 0))
        self.btn_add = tk.Label(side, text="Добавить →", bg=TXT_BG, fg=FG,
                                font=("Segoe UI", 9), cursor="hand2", padx=8,
                                pady=3)
        self.btn_add.pack(fill="x")
        self.btn_add.bind("<Button-1>", lambda e: self._add(self._selection()))
        self.btn_rm = tk.Label(side, text="← Убрать", bg=TXT_BG, fg=FG,
                               font=("Segoe UI", 9), cursor="hand2", padx=8,
                               pady=3)
        self.btn_rm.pack(fill="x", pady=4)
        self.btn_rm.bind("<Button-1>", lambda e: self._remove())

        tk.Label(self.body, text="Выбранные мейны", bg=BG, fg=FG,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14,
                                                   pady=(10, 0))
        chosen_wrap = tk.Frame(self.body, bg=BG)
        chosen_wrap.pack(fill="x", padx=14)
        self.chosen = tk.Listbox(chosen_wrap, bg=TXT_BG, fg=ACCENT,
                                 height=8, relief="flat",
                                 font=("Segoe UI", 10),
                                 highlightthickness=1,
                                 highlightbackground="#2a3040",
                                 exportselection=False)
        self.chosen.pack(side="left", fill="x", expand=True)
        sb = tk.Scrollbar(chosen_wrap, command=self.chosen.yview)
        sb.pack(side="right", fill="y")
        self.chosen.configure(yscrollcommand=sb.set)

        self._build_accounts()
        self._build_hotkey()
        self._build_client_section()
        self._build_items_section()
        self._build_sources()
        self._build_proxy()

    # ---------------- аккаунты игрока ----------------
    def _build_accounts(self) -> None:
        """До трёх аккаунтов: мейн + смурфы на других серверах.

        Их матчи складываются в персональные советы (мои предметы, мои
        проигрыши). Ник+тег+сервер проверяются кнопкой ✓ — профиль должен
        существовать, иначе данные будут пустыми. Клик по нику открывает
        профиль в браузере по умолчанию.
        """
        tk.Label(self.body, text="Мои аккаунты (до 3)", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14,
                                                     pady=(12, 0))
        tk.Label(self.body,
                 text="личная история: мои предметы по чемпиону и с кем я "
                      "сам проигрываю. Несколько аккаунтов складываются "
                      "вместе (мейн + смурф)",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8),
                 wraplength=self._w(), justify="left").pack(anchor="w",
                                                            padx=14)

        box = tk.Frame(self.body, bg=BG)
        box.pack(fill="x", padx=14, pady=(4, 0))

        head = tk.Frame(box, bg=BG)
        head.pack(fill="x")
        for txt, w in (("сервер", 10), ("ник", 22), ("тег", 8),
                       ("", 18)):
            tk.Label(head, text=txt, bg=BG, fg=MUTED,
                     font=("Segoe UI", 8), width=w).pack(side="left")
        tk.Label(head, text="статус", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="left", padx=(6, 0))

        saved = {f"{a['region']}|{a['name'].lower()}|{a['tag'].lower()}"
                 for a in self.config.accounts}
        for _ in range(3):
            entry = tk.Frame(box, bg=BG)
            entry.pack(fill="x", pady=2)
            region = ttk.Combobox(entry, width=10, state="readonly",
                                  font=("Segoe UI", 9))
            region["values"] = [reg for reg, _ in ugg.REGIONS]
            region.current(0)
            region.pack(side="left")
            name = tk.Entry(entry, width=22, bg=TXT_BG, fg=FG,
                            insertbackground=FG, font=("Segoe UI", 9),
                            relief="flat")
            attach_entry_menu(name)
            name.pack(side="left", padx=(4, 4))
            tag = tk.Entry(entry, width=8, bg=TXT_BG, fg=FG,
                           insertbackground=FG, font=("Segoe UI", 9),
                           relief="flat")
            attach_entry_menu(tag)
            tag.pack(side="left")
            chk = tk.Label(entry, text="✓", bg=TXT_BG, fg=MUTED,
                           cursor="hand2", font=("Segoe UI", 10, "bold"),
                           padx=8, pady=1)
            chk.pack(side="left", padx=(8, 0))
            chk.bind("<Button-1>", lambda e, r=region, n=name, t=tag,
                                          c=chk: self._check_account(r, n, t,
                                                                     c))
            status = tk.Label(entry, text="не проверен", bg=BG, fg=MUTED,
                              font=("Segoe UI", 8), cursor="hand2")
            status.pack(side="left", padx=(6, 0))
            # клик по нику открывает профиль в браузере
            status.bind("<Button-1>", lambda e, r=region, n=name,
                                            t=tag: self._open_profile(r, n, t))
            name.bind("<Return>", lambda e, r=region, n=name, t=tag,
                                       c=chk: self._check_account(r, n, t, c))
            self._acc_rows.append({"region": region, "name": name,
                                   "tag": tag, "status": status,
                                   "mark": chk, "ok": False,
                                   "url": ""})

        # заполняем уже сохранёнными
        for acc, row in zip(self.config.accounts, self._acc_rows):
            regs = [r for r, _ in ugg.REGIONS]
            if acc["region"] in regs:
                row["region"].current(regs.index(acc["region"]))
            row["name"].insert(0, acc["name"])
            row["tag"].insert(0, acc["tag"])
            row["ok"] = True
            row["status"].configure(text="сохранён", fg=MUTED)

    def _check_account(self, region_w, name_w, tag_w, mark) -> None:
        """Проверка «профиль существует» (в фоне, чтобы UI не стал)."""
        region = region_w.get().strip()
        name = name_w.get().strip()
        tag = tag_w.get().strip()
        if not (region and name and tag):
            self._status_for(mark).configure(text="заполни все поля",
                                             fg=TONE_LOW)
            return
        self._status_for(mark).configure(text="проверяю…", fg=MUTED)

        def work():
            res = ugg.check_profile(region, name, tag)
            self.top.after(0, lambda: self._check_done(res, mark))

        threading.Thread(target=work, daemon=True).start()

    def _check_done(self, res: dict, mark) -> None:
        lbl = self._status_for(mark)
        if res.get("ok"):
            prof = res.get("profile") or {}
            tier = f"{prof.get('tier') or ''} {prof.get('rank') or ''}".strip()
            wr = ""
            if prof.get("wins") is not None and prof.get("losses") is not None:
                wr = f" · {prof['wins']}/{prof['losses']}"
            lbl.configure(text=f"✓ профиль найден"
                               + (f" · {tier}" if tier else "") + wr,
                          fg=TONE_OK)
            for row in self._acc_rows:
                if row["mark"] is mark:
                    row["ok"] = True
        else:
            lbl.configure(text=f"✗ {res.get('error', 'ошибка')}", fg=TONE_LOW)

    def _open_profile(self, region_w, name_w, tag_w) -> None:
        region = region_w.get().strip()
        name = name_w.get().strip()
        tag = tag_w.get().strip()
        if not (region and name and tag):
            return
        webbrowser.open(ugg.profile_url(region, name, tag))

    def _status_for(self, mark):
        for row in self._acc_rows:
            if row["mark"] is mark:
                return row["status"]
        return tk.Label()

    # ---------------- источники ----------------
    def _build_sources(self) -> None:
        tk.Label(self.body, text="Источники данных", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14,
                                                     pady=(12, 0))
        tk.Label(self.body,
                 text="чем меньше источников, тем быстрее синк; лишнее "
                      "выключается галочкой",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8),
                 wraplength=self._w(), justify="left").pack(anchor="w",
                                                            padx=14)
        for text, key in SOURCES:
            var = tk.BooleanVar(value=self.config.source_on(key))
            self._source_vars[key] = var
            ttk.Checkbutton(self.body, text=text, variable=var).pack(
                anchor="w", padx=18)

    # ---------------- прокси/VPN ----------------
    def _build_proxy(self) -> None:
        tk.Label(self.body, text="Прокси / VPN", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14,
                                                     pady=(12, 0))
        tk.Label(self.body,
                 text="часть сайтов открывается только через VPN. Формат: "
                      "http://127.0.0.1:10808 (порт бывает 10809). "
                      "Пусто = прямое соединение.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8),
                 wraplength=self._w(), justify="left").pack(anchor="w",
                                                            padx=14)
        self.proxy = tk.StringVar(value=self.config.proxy)
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", padx=14, pady=(2, 0))
        proxy_e = tk.Entry(row, textvariable=self.proxy, bg=TXT_BG, fg=FG,
                           insertbackground=FG, font=("Consolas", 9),
                           relief="flat")
        attach_entry_menu(proxy_e)
        proxy_e.pack(side="left", fill="x", expand=True)
        btn = tk.Label(row, text="Проверить", bg=TXT_BG, fg=FG, cursor="hand2",
                       font=("Segoe UI", 9), padx=8, pady=2)
        btn.pack(side="left", padx=(6, 0))
        btn.bind("<Button-1>", lambda e: self._check_sources())

        self.proxy_lbl = tk.Label(self.body, text="", bg=BG, fg=MUTED,
                                  font=("Segoe UI", 8), anchor="w",
                                  justify="left", wraplength=self._w())
        self.proxy_lbl.pack(fill="x", padx=14)

    def _check_sources(self) -> None:
        """Проверяем доступность источников напрямую и через прокси.

        Показываем матрицу: какой сайт работает без VPN, какой — только
        через него (чтобы предложить включить), чтобы пользователь не
        выяснял это методом проб.
        """
        from . import net

        self.proxy_lbl.configure(text="проверяю источники…", fg=MUTED)
        proxy_addr = self.proxy.get().strip()

        def work():
            urls = {
                "op.gg": "https://op.gg/lol/champions/shen/counters/support",
                "lolalytics": "https://lolalytics.com/lol/shen/build/",
                "u.gg": "https://u.gg/lol/profile/euw1/Modesthate-euw",
            }
            results: dict[str, tuple[bool, bool]] = {}
            for label, url in urls.items():
                direct = self._probe(url, "")
                via = (self._probe(url, proxy_addr)
                       if proxy_addr else direct)
                results[label] = (direct, via)
            net.set_proxy(proxy_addr)
            self.top.after(0, lambda: self._show_probe(results))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _probe(url: str, proxy_addr: str) -> bool:
        from . import net

        old = net.proxy()
        try:
            net.set_proxy(proxy_addr)
            net.get_html(url, timeout=8, retries=0)
            return True
        except Exception:                             # noqa: BLE001
            return False
        finally:
            net.set_proxy(old)

    def _show_probe(self, results: dict) -> None:
        lines = []
        need_vpn = []
        for label, (direct, via) in results.items():
            if direct:
                state = "работает напрямую"
            elif via:
                state = "только через VPN"
                need_vpn.append(label)
            else:
                state = "недоступен"
            lines.append(f"{label}: {state}")
        txt = " · ".join(lines)
        if need_vpn and not self.proxy.get().strip():
            txt += ("\n…для части источников нужен VPN — вставь адрес "
                    "прокси и нажми «Проверить» ещё раз")
        self.proxy_lbl.configure(
            text=txt,
            fg=MUTED if all(d or v for d, v in results.values())
            else TONE_LOW)

    # ---------------- хоткей / предметы / клиент ----------------
    def _build_hotkey(self) -> None:
        tk.Label(self.body, text="Хоткей", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14,
                                                     pady=(12, 0))
        self.hotkey = tk.StringVar()
        hk = tk.Entry(self.body, textvariable=self.hotkey, bg=TXT_BG, fg=FG,
                      insertbackground=FG, font=("Consolas", 11), width=10,
                      relief="flat")
        attach_entry_menu(hk)
        hk.pack(anchor="w", padx=14, pady=(2, 4))
        tk.Label(self.body, text="например F8, F9, Ctrl+F8", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=14)

    def _build_items_section(self) -> None:
        """Блок предметов: показывать ли и сколько.

        Порядок покупки берётся из OP.GG по патчу, поэтому блок появляется
        сразу после пика, а не в конце драфта.
        """
        tk.Label(self.body, text="Предметы", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14,
                                                     pady=(12, 0))
        items = self.config.items
        self.item_enabled = tk.BooleanVar(
            value=bool(items.get("enabled", True)))
        self.item_boots = tk.BooleanVar(
            value=bool(items.get("show_boots", True)))
        self.item_count = tk.IntVar(
            value=int(items.get("show_count", 3) or 3))

        def cb(text, var):
            return ttk.Checkbutton(self.body, text=text, variable=var).pack(
                anchor="w", padx=14)

        cb("показывать первые предметы", self.item_enabled)
        cb("показывать обувь", self.item_boots)

        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", padx=14, pady=(2, 0))
        tk.Label(row, text="сколько предметов", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        for n in (2, 3):
            ttk.Radiobutton(row, text=str(n), value=n,
                            variable=self.item_count).pack(side="left",
                                                           padx=(8, 0))
        tk.Label(self.body,
                 text="две линии: мета по драфту + личная история. Лента "
                      "lolalytics уточняет билд по твоей линии",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8),
                 wraplength=self._w(), justify="left").pack(anchor="w",
                                                            padx=14)

        self.opacity = tk.DoubleVar()
        self.opacity.set(float(self.config.window.get("opacity", 0.94)))
        tk.Label(self.body, text="Прозрачность окна", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14,
                                                     pady=(10, 0))
        tk.Scale(self.body, from_=0.5, to=1.0, resolution=0.02,
                 orient="horizontal", variable=self.opacity, bg=BG, fg=FG,
                 troughcolor=TXT_BG, activebackground=ACCENT,
                 highlightthickness=0, bd=0).pack(fill="x", padx=14)

        self.count_lbl = tk.Label(self.body, text="", bg=BG, fg=MUTED,
                                  font=("Segoe UI", 8))
        self.count_lbl.pack(anchor="w", padx=14)

    def _build_client_section(self) -> None:
        """Путь к папке с игрой.

        Без него приложение ищет клиент через процесс, а если тот не запущен —
        перебирает диски. Ручной путь избавляет от перебора раз и навсегда.
        """
        from . import lcu

        tk.Label(self.body, text="Папка с игрой (где LeagueClient.exe)",
                 bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).pack(
                     anchor="w", padx=14, pady=(12, 0))

        self.league_path = tk.StringVar(
            value=str(lcu.detect_league_path() or ""))
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", padx=14, pady=(2, 2))
        league_e = tk.Entry(row, textvariable=self.league_path, bg=TXT_BG,
                             fg=FG, insertbackground=FG,
                             font=("Consolas", 9), relief="flat")
        attach_entry_menu(league_e)
        league_e.pack(side="left", fill="x", expand=True)

        def browse():
            d = filedialog.askdirectory(
                title="Выбери папку с League of Legends",
                initialdir=self.league_path.get() or None,
                mustexist=True, parent=self.top)
            if d:
                self.league_path.set(d)
                self._client_status()

        def autodetect():
            self.league_path.set("")
            self.league_path.set(str(lcu.detect_league_path(force=True) or ""))
            self._client_status()

        for txt, cmd in (("Обзор…", browse), ("Найти сам", autodetect)):
            b = tk.Label(row, text=txt, bg=TXT_BG, fg=FG, cursor="hand2",
                         font=("Segoe UI", 9), padx=8, pady=3)
            b.pack(side="left", padx=(6, 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())

        self.client_lbl = tk.Label(self.body, text="", bg=BG, fg=MUTED,
                                   font=("Segoe UI", 8), anchor="w",
                                   justify="left", wraplength=self._w())
        self.client_lbl.pack(fill="x", padx=14)
        self._client_status()

    def _client_status(self) -> None:
        from . import lcu

        raw = self.league_path.get().strip()
        if not raw:
            self.client_lbl.configure(
                text="путь не задан: клиент ищется автоматически при запущенном "
                     "League, иначе раз в минуту перебором дисков",
                fg=MUTED)
            return
        ok = (Path(raw) / "Lockfile").is_file()
        self.client_lbl.configure(
            text=("найден Lockfile" if ok else
                  "в этой папке нет Lockfile — проверь путь"),
            fg=TONE_OK if ok else TONE_LOW)

    # ---------------- данные ----------------
    def _load(self) -> None:
        self.role.set(self.config.role)
        self.pool_role.set(self.config.role)
        self.role2.set(self.config.role2)
        self.pool_mode.set(self.config.pool_mode)
        self.hotkey.set(self.config.hotkey)
        self._load_pool()
        self._filter()

    def _on_pool_mode(self, *_a) -> None:
        if self.pool_mode.get() == "shared":
            self.pool_section.pack_forget()
        else:
            self.pool_section.pack(fill="x", before=self._main_label)
        self._load_pool()

    def _load_pool(self) -> None:
        """Мейны редактируемой роли в список выбранных."""
        self.search.set("")
        self.selected.clear()
        if self.pool_mode.get() == "shared":
            pool = self.config.data.get("pool") or []
        else:
            role = self.pool_role.get()
            pool = self.config.pools.get(role)
            if pool is None and role == self.config.role:
                # плоский pool из старых версий наследуют мейны ручной роли
                pool = self.config.pool
        for name in (pool or []):
            cid = ch.resolve(name, self.index)
            if cid and cid in self.champs:
                self.selected[cid] = self.champs[cid].name
        self._refresh_chosen()
        self._filter()

    def _filter(self) -> None:
        q = self.search.get().strip().lower()
        self._current = []
        self.listbox.delete(0, "end")
        for c in sorted(self.champs.values(), key=lambda c: c.name):
            if q and q not in c.name.lower():
                continue
            self._current.append(c.cid)
            mark = "✓" if c.cid in self.selected else "  "
            self.listbox.insert("end", f"{mark} {c.name}")

    def _selection(self) -> list[int]:
        return [self._current[i] for i in self.listbox.curselection()]

    def _on_select(self, _evt=None) -> None:
        self.btn_add.configure(
            fg=ACCENT if len(self.selected) < MAX_POOL else TONE_LOW)

    def _add(self, cids: list[int]) -> None:
        room = MAX_POOL - len(self.selected)
        if room <= 0:
            return
        for cid in cids[:room]:
            self.selected[cid] = self.champs[cid].name
        self._refresh_chosen()
        self._filter()

    def _remove(self) -> None:
        for cid in self._selection():
            self.selected.pop(cid, None)
        self._refresh_chosen()
        self._filter()

    def _refresh_chosen(self) -> None:
        self.chosen.delete(0, "end")
        for cid, name in self.selected.items():
            self.chosen.insert("end", name)
        n = len(self.selected)
        warn = "" if n >= 1 else "   ⚠ выбери хотя бы одного мейна"
        self.count_lbl.configure(
            text=f"выбрано {n}/{MAX_POOL}{warn}")
        self._on_select()

    # ---------------- сохранение ----------------
    def _save(self) -> None:
        if not self.selected:
            from tkinter import messagebox
            messagebox.showwarning(
                "Нет мейнов",
                "Выбери хотя бы одного мейна, иначе считать будет нечего.",
                parent=self.top)
            return

        # путь к игре сохраняем отдельным файлом: пустой = искать самим
        from . import lcu
        raw = self.league_path.get().strip()
        try:
            lcu.set_league_path(raw or None)
        except lcu.LcuUnavailable as e:
            from tkinter import messagebox
            if not messagebox.askyesno(
                    "Путь не сохранён",
                    f"{e}\n\nВсё равно сохранить остальные настройки?",
                    parent=self.top):
                return

        cfg = self.config
        names = [self.selected[c] for c in self.selected]
        if self.pool_mode.get() == "shared":
            cfg.set_shared_pool(names)
        else:
            # перезаписываем пул ТОЛЬКО редактируемой роли: пулы других ролей
            # (лес, бот, …) должны пережить сохранение настроек
            pools = dict(cfg.pools)
            pools[self.pool_role.get()] = names
            cfg.set_pools(pools)
        cfg.data["role"] = self.role.get()
        cfg.data["role2"] = self.role2.get()
        cfg.data["pool_mode"] = self.pool_mode.get()
        cfg.data["hotkey"] = self.hotkey.get().strip() or "F8"
        cfg.data["items"] = {
            "enabled": bool(self.item_enabled.get()),
            "show_boots": bool(self.item_boots.get()),
            "show_count": max(1, min(5, int(self.item_count.get() or 3))),
        }
        cfg.window["opacity"] = round(float(self.opacity.get()), 2)
        # 1.3: аккаунты, источники, прокси
        accounts = []
        for row in self._acc_rows:
            reg = row["region"].get().strip()
            nm = row["name"].get().strip()
            tg = row["tag"].get().strip()
            if reg and nm and tg:
                accounts.append({"region": reg, "name": nm, "tag": tg})
        cfg.set_accounts(accounts)
        cfg.data["sources"] = {k: bool(v.get())
                               for k, v in self._source_vars.items()}
        cfg.proxy = self.proxy.get().strip()
        cfg.save()
        self.top.destroy()
        if self.on_save:
            self.on_save()
