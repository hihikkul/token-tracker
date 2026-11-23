"""Where things live: source log folders, app data folder, config."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
if getattr(sys, "frozen", False):  # packaged app: data files sit next to the bundled code
    APP_ROOT = APP_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    APP_ROOT = Path(__file__).resolve().parent.parent
    APP_DIR = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    base = os.environ.get("TOKENTRACKER_HOME")
    if base:
        d = Path(base)
    elif os.name == "nt":
        d = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local")) / "TokenTracker"
    elif sys.platform == "darwin":
        d = HOME / "Library" / "Application Support" / "TokenTracker"
    else:
        d = HOME / ".local" / "share" / "tokentracker"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "usage.db"


def _split_env(name):
    v = os.environ.get(name)
    return [Path(p.strip()) for p in v.split(",") if p.strip()] if v else []


def claude_roots():
    roots = _split_env("CLAUDE_CONFIG_DIR") or [HOME / ".claude", HOME / ".config" / "claude"]
    roots += [Path(p) for p in load_config().get("extra_paths", {}).get("claude", [])]
    return roots


def claude_project_dirs(existing_only=True):
    out = []
    for r in claude_roots():
        p = r / "projects" if (r / "projects").exists() else r
        if (not existing_only or p.exists()) and p not in out:
            out.append(p)
    return out


def claude_credentials_file():
    for r in claude_roots():
        f = r / ".credentials.json"
        if f.exists():
            return f
    return None


def claude_keychain_oauth():
    """macOS: Claude Code keeps its login in the Keychain instead of .credentials.json."""
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout).get("claudeAiOauth") or {}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def clauderipple_home() -> Path:
    v = os.environ.get("CLAUDERIPPLE_HOME")
    return Path(v) if v else HOME / ".clauderipple"


def codex_root() -> Path:
    v = os.environ.get("CODEX_HOME")
    return Path(v) if v else HOME / ".codex"


def codex_session_dirs(existing_only=True):
    root = codex_root()
    dirs = [root / "sessions", root / "archived_sessions"]
    dirs += [Path(p) for p in load_config().get("extra_paths", {}).get("codex", [])]
    return [d for d in dirs if d.exists()] if existing_only else dirs


def gemini_root() -> Path:
    v = os.environ.get("GEMINI_HOME")
    return Path(v) if v else HOME / ".gemini"


# ---------------------------------------------------------------- config
DEFAULT_CONFIG = {
    "port": 8765,
    "scan_interval_sec": 60,
    "limits_interval_sec": 300,
    # Legacy monthly overrides. 0 = use the detected plan's list price.
    # Login-detected plans share the prices used by subscription_plans.
    "subscription_usd_month": {"claude": 0, "codex": 0, "gemini": 0},
    "subscription_plans": {},  # auto-detect until the user chooses a plan
    "widget": {"x": None, "y": None, "opacity": 0.96, "topmost": True,
               "taskbar": True,  # taskbar button whose icon shows the lowest remaining %
               "show": ["claude", "codex"],  # tools shown in the widget (Gemini has no limits)
               "font": "", "number_font": ""},  # "" = Segoe UI Variable (Segoe UI on Windows 10)
    "artifact_url": None,
    # group ranking (쌀숭이 랭킹); filled in from the dashboard's ranking page
    "ranking": {"server": ""},  # keep deployment/server overrides separate from membership
    "ranking_groups": [],
    "ranking_global": {},
    "extra_paths": {"claude": [], "codex": [], "gemini": []},
}


def config_path() -> Path:
    return data_dir() / "config.json"


def migrate_ranking(cfg):
    """Move legacy membership once; keep ranking.server and other server settings."""
    legacy = cfg.get('ranking') or {}
    if not (legacy.get('code') and legacy.get('token')):
        return False
    groups = cfg.setdefault('ranking_groups', [])
    code = str(legacy['code']).strip().upper()
    if not any(str(g.get('code', '')).upper() == code for g in groups):
        groups.append({key: legacy.get(key) for key in
                       ('code', 'group_name', 'member_id', 'token', 'nick')})
        groups[-1]['code'] = code
    for key in ('code', 'group_name', 'member_id', 'token', 'nick'):
        legacy.pop(key, None)
    return True


# The widget, the dashboard's request threads and the ranking code all rewrite config.json.
# Every read-modify-write runs under this lock (see update_config), and each write replaces the
# file in one step, so a reader never sees half a file and nobody saves over a newer change.
_config_lock = threading.RLock()
_last_good = None  # last config read successfully in this process


def config_lock():
    return _config_lock


def _backup_path() -> Path:
    return config_path().with_name("config.json.bak")


def _read_json(p):
    """A config dict from p; OSError/ValueError when it cannot be read as one."""
    for attempt in range(5):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            break
        except PermissionError:  # Windows: another process is replacing the file right now
            if attempt == 4:
                raise
            time.sleep(0.05)
    if not isinstance(data, dict):
        raise ValueError("config is not a JSON object")
    return data


def _keep_broken_copy(p):
    """Copy an unreadable config aside before anything is saved over it (a hand edit, say)."""
    try:
        data = p.read_bytes()
        aside = p.with_name("config.json.broken")
        if not aside.exists() or aside.read_bytes() != data:
            aside.write_bytes(data)
    except OSError:
        pass


def _user_config(p):
    """The saved settings: config.json, else its backup, else the last good copy in memory."""
    global _last_good
    try:
        user = _read_json(p)
        _last_good = json.loads(json.dumps(user))
        return user
    except ValueError:
        _keep_broken_copy(p)
    except OSError:
        pass
    try:
        return _read_json(_backup_path())
    except (OSError, ValueError):
        pass
    return json.loads(json.dumps(_last_good)) if _last_good is not None else {}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = config_path()
    with _config_lock:
        if p.exists():
            for k, v in _user_config(p).items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        else:
            save_config(cfg)
        if migrate_ranking(cfg):
            save_config(cfg)
    return cfg


def _write_atomic(path, text):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: a reader in another process holds it open
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def save_config(cfg: dict) -> bool:
    """Write the whole config; True when it reached the disk."""
    global _last_good
    text = json.dumps(cfg, indent=2, ensure_ascii=False)
    with _config_lock:
        try:
            # backup first: if config.json is ever damaged, the newest settings survive here
            _write_atomic(_backup_path(), text)
            _write_atomic(config_path(), text)
        except OSError:
            return False
        _last_good = json.loads(text)
    return True


def update_config(change):
    """Re-read the config, apply change(cfg) and save, all under the config lock.
    Use this instead of saving a copy loaded earlier, which would undo newer edits."""
    with _config_lock:
        cfg = load_config()
        change(cfg)
        return save_config(cfg)
