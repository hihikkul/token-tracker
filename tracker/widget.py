"""Always-on-top remaining-limits widget (tkinter, standard library only).

Shows only what is left: per tool and plan, the 5-hour and weekly windows, how much
is left, when each resets and what time the numbers are from. Usage details live in
the dashboard (▤ button, or double-click the title).

Drag the title bar to move. Right-click for the menu.
"""
import threading
import time
import tkinter as tk
import webbrowser

from . import paths

C = {
    "bg": "#1a1a19", "line": "#2c2c2a", "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781",
    "track": "#2c2c2a", "claude": "#d95926", "codex": "#3987e5", "gemini": "#199e70",
    "good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b",
    "pill": "#2c2c2a",
}
NAMES = {"claude": "Claude Code", "codex": "Codex", "gemini": "Gemini"}
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
    return f"{t.tm_mon}/{t.tm_mday} \u00b7 Resets in {until(ts)}"


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


class Widget:
    W = 300

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
        self.root = tk.Tk()
        self.root.title("Usage Limits")
        self.root.overrideredirect(True)
        self.root.configure(bg=C["bg"])
        self.root.attributes("-topmost", bool(wc.get("topmost", True)))
        try:
            self.root.attributes("-alpha", float(wc.get("opacity", 0.94)))
        except tk.TclError:
            pass
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = wc.get("x") if wc.get("x") is not None else sw - self.W - 24
        y = wc.get("y") if wc.get("y") is not None else sh - 360
        self.root.geometry(f"+{int(x)}+{int(y)}")

        self.frame = tk.Frame(self.root, bg=C["bg"], highlightthickness=1, highlightbackground="#383835")
        self.frame.pack(fill="both", expand=True)
        self._build_header()
        self.body = tk.Frame(self.frame, bg=C["bg"])
        self.body.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self._build_menu()
        self.root.bind("<Button-3>", self._popup)
        self._last_draw = 0
        self._last_min = None
        self.root.after(300, self._tick)

    # ---------------------------------------------------------------- chrome
    def _build_header(self):
        h = tk.Frame(self.frame, bg=C["bg"])
        h.pack(fill="x", padx=10, pady=(8, 4))
        self.title = tk.Label(h, text="Usage Limits", bg=C["bg"], fg=C["ink"], font=(FONT, 10, "bold"), cursor="fleur")
        self.title.pack(side="left")
        self.sub = tk.Label(h, text="", bg=C["bg"], fg=C["muted"], font=(FONT, 8))
        self.sub.pack(side="left", padx=(6, 0))
        for sym, cmd in (("✕", self.quit), ("▸" if self.compact else "▾", self.toggle_compact),
                         ("⟳", self.refresh), ("▤", self.open_dashboard)):
            b = tk.Label(h, text=sym, bg=C["bg"], fg=C["ink2"], font=(FONT, 10), cursor="hand2", padx=4)
            b.pack(side="right")
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.configure(fg=C["ink"]))
            b.bind("<Leave>", lambda e, w=b: w.configure(fg=C["ink2"]))
            if sym in ("▾", "▸"):
                self.fold_btn = b
        for w in (h, self.title, self.sub):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Double-Button-1>", lambda e: self.open_dashboard())

    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="상세 대시보드 열기", command=self.open_dashboard)
        if self.cfg.get("artifact_url"):
            m.add_command(label="아티팩트 대시보드 열기", command=lambda: webbrowser.open(self.cfg["artifact_url"]))
        m.add_command(label="지금 새로고침", command=self.refresh)
        m.add_command(label="간단히 / 자세히", command=self.toggle_compact)
        m.add_command(label="항상 위 켜기/끄기", command=self.toggle_topmost)
        m.add_separator()
        m.add_command(label="설정 폴더 열기", command=lambda: webbrowser.open(paths.data_dir().as_uri()))
        m.add_command(label="종료", command=self.quit)
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

    def _label(self, parent, text, fg="ink2", size=8, bold=False, **pack):
        lb = tk.Label(parent, text=text, bg=C["bg"], fg=C[fg], font=(FONT, size, "bold" if bold else "normal"))
        lb.pack(**pack)
        return lb

    def _pill(self, parent, text):
        tk.Label(parent, text=f" {text} ", bg=C["pill"], fg=C["ink2"], font=(FONT, 7, "bold")).pack(side="left", padx=(6, 0))

    def _bar(self, parent, left_pct, color):
        width = self.W - 26
        c = tk.Canvas(parent, width=width, height=6, bg=C["bg"], highlightthickness=0)
        c.create_rectangle(0, 0, width, 6, fill=C["track"], outline="")
        w = max(0.0, min(1.0, left_pct / 100)) * width
        if w > 0:
            c.create_rectangle(0, 0, w, 6, fill=color, outline="")
        c.pack(anchor="w", pady=(2, 0))

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
        upd = snap.get("updated")
        self.sub.configure(text="" if upd else "준비 중…")
        limits = snap.get("limits") or {}

        first = True
        for src in ("claude", "codex", "gemini"):
            if src not in self.show:
                continue
            lim = limits.get(src) or {}
            accts = self._accounts(src, lim)
            if not first:
                tk.Frame(self.body, bg=C["line"], height=1).pack(fill="x", pady=(8, 0))
            first = False

            if not accts:  # nothing to show for this tool: one muted line
                row = tk.Frame(self.body, bg=C["bg"])
                row.pack(fill="x", pady=(6, 0))
                self._label(row, "■", src, 8, side="left")
                self._label(row, NAMES[src], "ink", 9, True, side="left", padx=(3, 0))
                msg = {"no_credentials": "이 PC에 로그인 정보 없음", "expired": "로그인 만료 · Claude Code 한 번 실행",
                       "no_data": "아직 기록 없음"}.get(lim.get("status"), lim.get("message") or "정보 없음")
                self._label(row, msg, "muted", 8, side="right")
                continue

            for plan, wins, as_of in accts:
                head = tk.Frame(self.body, bg=C["bg"])
                head.pack(fill="x", pady=(6, 0))
                self._label(head, "■", src, 8, side="left")
                self._label(head, NAMES[src], "ink", 9, True, side="left", padx=(3, 0))
                if plan:
                    self._pill(head, plan)

                if self.compact:
                    # one line per account: big, severity-colored % for each window + soonest reset
                    line = tk.Frame(self.body, bg=C["bg"])
                    line.pack(fill="x", pady=(3, 0))
                    for i, w in enumerate(wins[:2]):
                        used = float(w["used_percent"])
                        self._label(line, wlabel(w["label"]), "ink2", 8, side="left", padx=(0 if i == 0 else 12, 4))
                        tk.Label(line, text=f"{100 - used:.0f}%", bg=C["bg"], fg=C[sev(used) if used >= 50 else "ink"],
                                 font=(self.num_font, 11, "bold")).pack(side="left")
                    nxt = min((w["resets_at"] for w in wins if w.get("resets_at")), default=None)
                    if nxt:
                        self._label(line, f"in {until(nxt)}", "ink2", 8, side="right")
                    continue

                for w in wins[:3]:
                    used = float(w["used_percent"])
                    left = 100 - used
                    r1 = tk.Frame(self.body, bg=C["bg"])
                    r1.pack(fill="x", pady=(5, 0))
                    tk.Label(r1, text=wlabel(w["label"]), bg=C["bg"], fg=C["ink2"], font=(FONT, 8),
                             width=7, anchor="w").pack(side="left")  # fixed column so the % line up
                    tk.Label(r1, text=f"{left:.0f}%", bg=C["bg"], fg=C["ink"], font=(self.num_font, 10, "bold")).pack(side="left")
                    if w.get("resets_at"):
                        self._label(r1, reset_in(w["resets_at"]), "ink2", 8, side="right")
                    elif w.get("note"):
                        self._label(r1, w["note"], "muted", 8, side="right")
                    self._bar(self.body, left, C[sev(used)])
            if lim.get("status") in ("rate_limited", "offline", "auth", "expired", "error") and lim.get("message"):
                tk.Label(self.body, text=lim["message"], bg=C["bg"], fg=C["muted"], font=(FONT, 7),
                         wraplength=self.W - 30, justify="left").pack(anchor="w", pady=(2, 0))
        self.root.update_idletasks()

    def run(self):
        self.root.mainloop()
