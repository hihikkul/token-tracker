"""Where things live: source log folders, app data folder, config."""
import json
import os
import subprocess
import sys
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


def claude_project_dirs():
    out = []
    for r in claude_roots():
        p = r / "projects" if (r / "projects").exists() else r
        if p.exists() and p not in out:
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


def codex_root() -> Path:
    v = os.environ.get("CODEX_HOME")
    return Path(v) if v else HOME / ".codex"


def codex_session_dirs():
    root = codex_root()
    dirs = [root / "sessions", root / "archived_sessions"]
    dirs += [Path(p) for p in load_config().get("extra_paths", {}).get("codex", [])]
    return [d for d in dirs if d.exists()]


def gemini_root() -> Path:
    v = os.environ.get("GEMINI_HOME")
    return Path(v) if v else HOME / ".gemini"


# ---------------------------------------------------------------- config
DEFAULT_CONFIG = {
    "port": 8765,
    "scan_interval_sec": 60,
    "limits_interval_sec": 300,
    # Monthly subscription you pay, per source (USD). 0 = use the detected plan's list price
    # (Claude Pro $20, Codex Plus $20…). Gemini can't be detected, so set it here to count it.
    "subscription_usd_month": {"claude": 0, "codex": 0, "gemini": 0},
    "widget": {"x": None, "y": None, "opacity": 0.96, "topmost": True,
               "taskbar": True,  # taskbar button whose icon shows the lowest remaining %
               "show": ["claude", "codex"],  # tools shown in the widget (Gemini has no limits)
               "font": "", "number_font": ""},  # "" = Segoe UI Variable (Segoe UI on Windows 10)
    "artifact_url": None,
    # group ranking (쌀숭이 랭킹); filled in from the dashboard's ranking page
    "ranking": {"server": "", "code": None, "group_name": None, "member_id": None, "token": None, "nick": ""},
    "extra_paths": {"claude": [], "codex": [], "gemini": []},
}


def config_path() -> Path:
    return data_dir() / "config.json"


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = config_path()
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except (OSError, ValueError):
            pass
    else:
        try:
            p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return cfg


def save_config(cfg: dict):
    try:
        config_path().write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
