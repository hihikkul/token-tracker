"""Always-on-top remaining-limits widget (tkinter, standard library only).

Shows only what is left: per tool and plan, the 5-hour and weekly windows, how much
is left and when each resets. Usage details live in the dashboard (▤ button, or
double-click the title).

On Windows the widget also has a taskbar button. Its icon is a small gauge with the
lowest remaining % (colored by how much is used) and its title lists every tool, so
the limits stay visible while the widget is minimized (– button) or covered.

Drag the title bar to move. Right-click for the menu.
"""
import base64
import os
import threading
import time
import tkinter as tk
import webbrowser

from . import icon, paths, updater

C = {
    "bg": "#141416", "line": "#232327", "edge": "#2e2e33", "ink": "#f4f4f5", "ink2": "#a1a1aa",
    "muted": "#6b6b73", "track": "#26262b", "pill": "#222226", "hover": "#1e1e22",
    "claude": "#e8794a", "codex": "#5b9cf5", "gemini": "#34c08b",
    "good": "#3fb950", "warning": "#e3b341", "serious": "#f0883e", "critical": "#f85149",
}
NAMES = {"claude": "Claude Code", "codex": "Codex", "gemini": "Gemini"}
SHORT = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}
FONT = "Calibri"
WEEKDAYS = "월화수목금토일"


def until(ts):
    s = max(0, int(ts - time.time()))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{max(m, 1)}m"


def reset_in(ts):
    t = time.localtime(ts)
    if ts - time.time() >= 86400:
        return f"Resets in {until(ts)} · {t.tm_mon}/{t.tm_mday}({WEEKDAYS[t.tm_wday]})"
    return f"Resets in {until(ts)}"


def clock(ts, with_day=False):
    """'03:53' today, '내일 03:53', or '10/14(화) 14:05'."""
    t = time.localtime(ts)
    today = time.localtime()
    days = (time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
            - time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))) / 86400
    hm = time.strftime("%H:%M", t)
    if round(days) == 0 and not with_day:
        return hm
    if round(days) == 1:
        return f"내일 {hm}"
    if round(days) == -1:
        return f"어제 {hm}"
    return f"{t.tm_mon}/{t.tm_mday}({WEEKDAYS[t.tm_wday]}) {hm}"


def wlabel(label):
    """'5시간' -> '5h', '주간' -> 'Weekly', '주간 Opus' -> 'Weekly Opus'."""
    return label.replace("5시간", "5h").replace("주간", "Weekly").replace("시간", "h").replace("일", "d")


def sev(used):
    return "critical" if used >= 90 else "serious" if used >= 75 else "warning" if used >= 50 else "good"


def bar_color(src, used):
    """Healthy windows wear the tool's color; from half used on, the warning scale takes over."""
    return C[src] if used < 50 else C[sev(used)]


def rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ---------------------------------------------------------------- Windows chrome
class Win:
    """Small ctypes helpers; every call is a no-op off Windows or if the API is missing."""
    ok = os.name == "nt"

    @staticmethod
    def hwnd(root):
        import ctypes
        return ctypes.windll.user32.GetParent(root.winfo_id())

    @classmethod
    def app_id(cls, name="hihikkul.token-tracker"):
        """Own taskbar identity, so the button shows our icon instead of Python's."""
        if not cls.ok:
            return
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(name)
        except (AttributeError, OSError):
            pass

    @classmethod
    def show_in_taskbar(cls, root):
        """A borderless (overrideredirect) window is hidden from the taskbar; put it back."""
        if not cls.ok:
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            h = cls.hwnd(root)
            GWL_EXSTYLE, GWL_STYLE = -20, -16
            ex = u.GetWindowLongW(h, GWL_EXSTYLE)
            u.SetWindowLongW(h, GWL_EXSTYLE, (ex & ~0x00000080) | 0x00040000)  # -TOOLWINDOW +APPWINDOW
            st = u.GetWindowLongW(h, GWL_STYLE)
            u.SetWindowLongW(h, GWL_STYLE, st | 0x00020000)  # MINIMIZEBOX: taskbar click minimizes
            root.withdraw()
            root.after(10, root.deiconify)
        except (AttributeError, OSError):
            pass

    @classmethod
    def round_corners(cls, root, border="#2e2e33"):
        """Windows 11 rounded corners and a thin border color (ignored on Windows 10)."""
        if not cls.ok:
            return
        try:
            import ctypes
            h = cls.hwnd(root)
            dwm = ctypes.windll.dwmapi
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            dwm.DwmSetWindowAttribute(h, 33, ctypes.byref(pref), ctypes.sizeof(pref))
            r, g, b = rgb(border)
            col = ctypes.c_uint(r | (g << 8) | (b << 16))
            dwm.DwmSetWindowAttribute(h, 34, ctypes.byref(col), ctypes.sizeof(col))
        except (AttributeError, OSError):
            pass


class Widget:
    W = 296
    PAD = 14

    def __init__(self, engine, dashboard_url):
        self.engine = engine
        self.url = dashboard_url
        self.cfg = paths.load_config()
        wc = self.cfg.setdefault("widget", {})
        self.show = wc.get("show") or ["claude", "codex"]
        global FONT
        FONT = wc.get("font") or "Calibri"
        self.num_font = wc.get("number_font") or FONT
        self.compact = bool(wc.get("compact"))
        self.taskbar = bool(wc.get("taskbar", True)) and Win.ok
        if self.taskbar:
            Win.app_id()
        self.root = tk.Tk()
        self.root.title("Usage Limits")
        try:
            self.root.iconbitmap(default=str(paths.APP_ROOT / "assets" / "token-tracker.ico"))
        except (tk.TclError, AttributeError):
            pass
        self.root.overrideredirect(True)
        self.root.configure(bg=C["bg"])
        self.root.attributes("-topmost", bool(wc.get("topmost", True)))
        try:
            self.root.attributes("-alpha", float(wc.get("opacity", 0.96)))
        except tk.TclError:
            pass
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = wc.get("x") if wc.get("x") is not None else sw - self.W - 24
        y = wc.get("y") if wc.get("y") is not None else sh - 380
        self.root.geometry(f"+{int(x)}+{int(y)}")

        # 1px edge drawn by the frame on Windows 10; Windows 11 draws its own rounded border
        self.frame = tk.Frame(self.root, bg=C["bg"], highlightthickness=1, highlightbackground=C["edge"])
        self.frame.pack(fill="both", expand=True)
        self._build_header()
        self.body = tk.Frame(self.frame, bg=C["bg"])
        self.body.pack(fill="both", expand=True, padx=self.PAD, pady=(0, 12))
        self._build_menu()
        self.root.bind("<Button-3>", self._popup)
        self._last_draw = 0
        self._last_min = None
        self._icon_key = None
        self._icons = []
        self.root.update_idletasks()
        Win.round_corners(self.root, C["edge"])
        if self.taskbar:
            Win.show_in_taskbar(self.root)
            self.root.bind("<Map>", self._on_map)
        self.root.after(300, self._tick)
        if self.cfg.get("auto_update", True):
            self.root.after(20_000, self._auto_update)

    # ---------------------------------------------------------------- chrome
    def _build_header(self):
        h = tk.Frame(self.frame, bg=C["bg"])
        h.pack(fill="x", padx=(self.PAD - 2, 8), pady=(10, 6))
        try:
            self._logo = tk.PhotoImage(data=base64.b64encode(icon.app_png(16)))
            logo = tk.Label(h, image=self._logo, bg=C["bg"])
            logo.pack(side="left", padx=(0, 6))
        except tk.TclError:
            logo = None
        self.title = tk.Label(h, text="Usage Limits", bg=C["bg"], fg=C["ink"], font=(FONT, 10, "bold"), cursor="fleur")
        self.title.pack(side="left")
        self.sub = tk.Label(h, text="", bg=C["bg"], fg=C["muted"], font=(FONT, 8))
        self.sub.pack(side="left", padx=(8, 0), pady=(1, 0))
        buttons = [("✕", self.quit, "닫기")]
        if self.taskbar:
            buttons.append(("–", self.minimize, "작업 표시줄로"))
        buttons += [("▸" if self.compact else "▾", self.toggle_compact, "간단히/자세히"),
                    ("⟳", self.refresh, "새로고침"), ("▤", self.open_dashboard, "대시보드")]
        for sym, cmd, _tip in buttons:
            b = tk.Label(h, text=sym, bg=C["bg"], fg=C["muted"], font=(FONT, 10), cursor="hand2", width=2)
            b.pack(side="right")
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.configure(fg=C["ink"], bg=C["hover"]))
            b.bind("<Leave>", lambda e, w=b: w.configure(fg=C["muted"], bg=C["bg"]))
            if sym in ("▾", "▸"):
                self.fold_btn = b
        for w in (h, self.title, self.sub, logo):
            if w is None:
                continue
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Double-Button-1>", lambda e: self.open_dashboard())

    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0, bg="#1c1c1f", fg=C["ink"], activebackground="#2b2b30",
                    activeforeground=C["ink"], bd=0, relief="flat", font=(FONT, 9))
        m.add_command(label="상세 대시보드 열기", command=self.open_dashboard)
        if self.cfg.get("artifact_url"):
            m.add_command(label="아티팩트 대시보드 열기", command=lambda: webbrowser.open(self.cfg["artifact_url"]))
        m.add_command(label="지금 새로고침", command=self.refresh)
        m.add_command(label="간단히 / 자세히", command=self.toggle_compact)
        if self.taskbar:
            m.add_command(label="작업 표시줄로 내리기", command=self.minimize)
        m.add_command(label="항상 위 켜기/끄기", command=self.toggle_topmost)
        m.add_command(label="업데이트 확인", command=lambda: self._update(manual=True))
        m.add_separator()
        m.add_command(label="설정 폴더 열기", command=lambda: webbrowser.open(paths.data_dir().as_uri()))
        m.add_command(label="종료", command=self.quit)
        m.add_separator()
        m.add_command(label=f"token-tracker v{updater.local_version()}", state="disabled")
        self.menu = m

    def _popup(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, e):
        self.cfg["widget"]["x"], self.cfg["widget"]["y"] = self.root.winfo_x(), self.root.winfo_y()
        paths.save_config(self.cfg)

    # --------------------------------------------------------------- actions
    def open_dashboard(self):
        webbrowser.open(self.url)

    def refresh(self):
        self.sub.configure(text="조회 중…")
        threading.Thread(target=lambda: self.engine.refresh(force_limits=True), daemon=True).start()

    def minimize(self):
        """Borderless windows can't iconify, so drop the border just while minimized."""
        self.root.overrideredirect(False)
        self.root.iconify()

    def _on_map(self, e):
        if e.widget is self.root and not self.root.overrideredirect() and self.root.state() == "normal":
            self.root.overrideredirect(True)
            self.root.after(10, lambda: (Win.show_in_taskbar(self.root), Win.round_corners(self.root, C["edge"])))

    def toggle_compact(self):
        self.compact = not self.compact
        self.cfg["widget"]["compact"] = self.compact
        paths.save_config(self.cfg)
        self.fold_btn.configure(text="▸" if self.compact else "▾")
        self._draw()

    def toggle_topmost(self):
        v = not bool(self.root.attributes("-topmost"))
        self.root.attributes("-topmost", v)
        self.cfg["widget"]["topmost"] = v
        paths.save_config(self.cfg)

    # ---------------------------------------------------------------- update
    def _auto_update(self):
        self._update(manual=False)
        hours = float(self.cfg.get("update_check_hours", 6))
        self.root.after(int(hours * 3600 * 1000), self._auto_update)

    def _note(self, text, ms=6000):
        self.sub.configure(text=text)
        self.root.after(ms, lambda: self.sub.configure(text=self._stamp()) if self.sub.cget("text") == text else None)

    def _update(self, manual):
        if updater.is_git_checkout():
            if manual:
                self._note("git으로 받은 폴더예요. git pull로 업데이트하세요")
            return
        if manual:
            self.sub.configure(text="확인 중…")

        def work():
            try:
                new = updater.check()
                if not new:
                    if manual:
                        self.root.after(0, lambda: self._note(f"최신 버전이에요 (v{updater.local_version()})"))
                    return
                self.root.after(0, lambda: self.sub.configure(text=f"v{new} 업데이트 중…"))
                updater.apply(new)
                self.root.after(0, self._restart)
            except Exception as e:  # network down, GitHub busy, files locked…
                if manual:
                    msg = str(e) or e.__class__.__name__
                    self.root.after(0, lambda: self._note(f"업데이트 실패: {msg[:40]}", 10000))
        threading.Thread(target=work, daemon=True).start()

    def _restart(self):
        try:
            updater.restart()
        except OSError:
            self._note("새 버전을 받았어요. 다시 켜면 적용돼요", 15000)
            return
        self.quit()

    def quit(self):
        self.engine.stop()
        self.root.destroy()

    # ------------------------------------------------------------------ draw
    def _tick(self):
        snap = self.engine.snapshot
        minute = int(time.time() // 60)
        if snap.get("updated", 0) != self._last_draw or minute != self._last_min:
            self._draw()  # countdowns move once a minute
        self.root.after(1000, self._tick)

    def _stamp(self):
        upd = self.engine.snapshot.get("updated")
        return f"{clock(upd)} 기준" if upd else "준비 중…"

    def _label(self, parent, text, fg="ink2", size=8, bold=False, font=None, **pack):
        lb = tk.Label(parent, text=text, bg=C["bg"], fg=C[fg] if fg in C else fg,
                      font=(font or FONT, size, "bold" if bold else "normal"))
        lb.pack(**pack)
        return lb

    def _dot(self, parent, color, size=8):
        c = tk.Canvas(parent, width=size, height=size, bg=C["bg"], highlightthickness=0)
        c.create_oval(1, 1, size - 1, size - 1, fill=color, outline="")
        c.pack(side="left", pady=(1, 0))

    def _pill(self, parent, text):
        tk.Label(parent, text=text, bg=C["pill"], fg=C["ink2"], font=(FONT, 7, "bold"),
                 padx=6, pady=0).pack(side="left", padx=(8, 0))

    def _bar(self, parent, left_pct, color, height=5):
        width = self.W - 2 * self.PAD - 2
        r = height / 2
        c = tk.Canvas(parent, width=width, height=height + 2, bg=C["bg"], highlightthickness=0)
        y = height / 2 + 1
        c.create_line(r + 1, y, width - r - 1, y, fill=C["track"], width=height, capstyle="round")
        frac = max(0.0, min(1.0, left_pct / 100))
        if frac > 0:
            c.create_line(r + 1, y, max(r + 1.5, r + 1 + frac * (width - height - 2)), y,
                          fill=color, width=height, capstyle="round")
        c.pack(anchor="w", pady=(4, 0))

    def _accounts(self, src, lim):
        """[(plan, windows, as_of)] for one tool."""
        if lim.get("accounts"):
            return [(a.get("plan"), a["windows"], a.get("fetched_at")) for a in lim["accounts"]]
        if lim.get("windows"):
            return [(lim.get("plan"), lim["windows"], lim.get("fetched_at"))]
        return []

    def _draw(self):
        snap = self.engine.snapshot
        self._last_draw = snap.get("updated", 0)
        self._last_min = int(time.time() // 60)
        for w in self.body.winfo_children():
            w.destroy()
        if self.sub.cget("text") in ("", "조회 중…", "준비 중…") or self.sub.cget("text").endswith("기준"):
            self.sub.configure(text=self._stamp())
        limits = snap.get("limits") or {}
        tk.Frame(self.body, width=self.W - 2 * self.PAD, height=0, bg=C["bg"]).pack()  # same width in both modes

        first = True
        for src in ("claude", "codex", "gemini"):
            if src not in self.show:
                continue
            lim = limits.get(src) or {}
            accts = self._accounts(src, lim)
            if not first:
                tk.Frame(self.body, bg=C["line"], height=1).pack(fill="x", pady=(12, 2))
            first = False

            if not accts:  # nothing to show for this tool: one muted line
                row = tk.Frame(self.body, bg=C["bg"])
                row.pack(fill="x", pady=(6, 0))
                self._dot(row, C[src])
                self._label(row, NAMES[src], "ink", 9, True, side="left", padx=(6, 0))
                msg = {"no_credentials": "이 PC에 로그인 정보 없음", "expired": "로그인 만료 · Claude Code 한 번 실행",
                       "no_data": "아직 기록 없음"}.get(lim.get("status"), lim.get("message") or "정보 없음")
                self._label(row, msg, "muted", 8, side="right")
                continue

            for ai, (plan, wins, as_of) in enumerate(accts):
                head = tk.Frame(self.body, bg=C["bg"])
                head.pack(fill="x", pady=(6 if ai == 0 else 10, 0))
                self._dot(head, C[src])
                self._label(head, NAMES[src], "ink", 9, True, side="left", padx=(6, 0))
                if plan:
                    self._pill(head, plan)

                if self.compact:
                    # one line per account: each window's % (warning colors only when it matters) + soonest reset
                    line = tk.Frame(self.body, bg=C["bg"])
                    line.pack(fill="x", pady=(4, 0))
                    for i, w in enumerate(wins[:2]):
                        used = float(w["used_percent"])
                        self._label(line, wlabel(w["label"]), "muted", 8, side="left", padx=(0 if i == 0 else 14, 5))
                        self._label(line, f"{100 - used:.0f}%", sev(used) if used >= 50 else "ink", 11, True,
                                    font=self.num_font, side="left")
                    nxt = min((w["resets_at"] for w in wins if w.get("resets_at")), default=None)
                    if nxt:
                        self._label(line, f"in {until(nxt)}", "muted", 8, side="right")
                    continue

                for w in wins[:3]:
                    used = float(w["used_percent"])
                    left = 100 - used
                    r1 = tk.Frame(self.body, bg=C["bg"])
                    r1.pack(fill="x", pady=(8, 0))
                    tk.Label(r1, text=wlabel(w["label"]), bg=C["bg"], fg=C["ink2"], font=(FONT, 8),
                             anchor="w").pack(side="left")
                    if w.get("resets_at"):
                        self._label(r1, reset_in(w["resets_at"]), "muted", 8, side="left", padx=(8, 0))
                    elif w.get("note"):
                        self._label(r1, w["note"], "muted", 8, side="left", padx=(8, 0))
                    self._label(r1, f"{left:.0f}%", sev(used) if used >= 75 else "ink", 11, True,
                                font=self.num_font, side="right")
                    self._bar(self.body, left, bar_color(src, used))
            if lim.get("status") in ("rate_limited", "offline", "auth", "expired", "error") and lim.get("message"):
                tk.Label(self.body, text=lim["message"], bg=C["bg"], fg=C["muted"], font=(FONT, 7),
                         wraplength=self.W - 2 * self.PAD, justify="left").pack(anchor="w", pady=(4, 0))
        self.root.update_idletasks()
        self._taskbar_status(limits)

    # ------------------------------------------------------- taskbar button
    def _taskbar_status(self, limits):
        """Window title lists every tool; the icon shows the tightest window as a gauge."""
        parts, worst = [], None
        for src in ("claude", "codex", "gemini"):
            if src not in self.show:
                continue
            wins = []
            for _plan, ws, _ in self._accounts(src, limits.get(src) or {}):
                wins += ws
            if not wins:
                continue
            used = max(float(w["used_percent"]) for w in wins)
            parts.append(f"{SHORT[src]} {100 - used:.0f}%")
            if worst is None or used > worst[0]:
                worst = (used, src)
        self.root.title(" · ".join(parts) + " — Usage Limits" if parts else "Usage Limits")
        if not self.taskbar:
            return
        if worst is None:
            key = None
        else:
            left = max(0, round(100 - worst[0]))
            key = (left, worst[1])
        if key == self._icon_key:
            return
        self._icon_key = key
        try:
            if key is None:
                imgs = [tk.PhotoImage(data=base64.b64encode(icon.app_png(s))) for s in (32, 64)]
            else:
                left, src = key
                col = rgb(bar_color(src, 100 - left))
                text = str(left) if left < 100 else None
                imgs = [tk.PhotoImage(data=base64.b64encode(icon.gauge_png(s, left / 100, col, text))) for s in (32, 64)]
            self._icons = imgs  # keep references, Tk does not
            self.root.iconphoto(False, *imgs)
        except tk.TclError:
            pass

    def run(self):
        self.root.mainloop()
