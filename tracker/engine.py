"""Background loop: rescan logs and refresh limits; keeps a snapshot for the widget."""
import threading
import time
import traceback

from . import paths, stats
from .collectors import scan_all
from .db import DB
from .limits import all_limits


class Engine:
    def __init__(self, db=None):
        self.db = db or DB()
        self.cfg = paths.load_config()
        self.snapshot = {"limits": {}, "today": {}, "updated": 0, "scan": {}}
        self.lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.listeners = []

    def refresh(self, force_limits=False):
        try:
            scan = scan_all(self.db)
        except Exception:
            scan = {"error": traceback.format_exc(limit=2)}
        try:
            lim = all_limits(self.db, force=force_limits)
        except Exception:
            lim = {}
        snap = {"limits": lim, "today": stats.today(self.db), "updated": time.time(), "scan": scan}
        with self.lock:
            self.snapshot = snap
        for fn in list(self.listeners):
            try:
                fn(snap)
            except Exception:
                pass
        return snap

    def poke(self):
        self._wake.set()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="tracker-loop").start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            self.refresh()
            self._wake.wait(self.cfg.get("scan_interval_sec", 60))
            self._wake.clear()
