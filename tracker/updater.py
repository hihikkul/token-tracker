"""Self-update from the GitHub repository.

The widget checks the VERSION file on GitHub a little after start and then every few hours.
When it is newer, the repository zip is downloaded, checked, and copied over this folder
(your records and settings live elsewhere, in %LOCALAPPDATA%\\TokenTracker), then the app
restarts itself. A folder cloned with git is updated with `git pull --ff-only` instead, so
git keeps tracking it; without git installed it falls back to the zip.
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

from . import paths

REPO = "hihikkul/token-tracker"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/main/VERSION"
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/main"
UA = {"User-Agent": "token-tracker-updater"}


def local_version():
    try:
        return (paths.APP_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0"


def _tuple(v):
    out = []
    for part in v.strip().lstrip("v").split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


def is_frozen():
    """The packaged macOS app (PyInstaller) can't overwrite itself; it offers the new DMG instead."""
    return bool(getattr(sys, "frozen", False))


def is_git_checkout():
    return (paths.APP_ROOT / ".git").exists()


def check():
    """Newer version string on GitHub, or None."""
    req = urllib.request.Request(VERSION_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=10) as r:
        remote = r.read().decode("utf-8").strip()
    return remote if _tuple(remote) > _tuple(local_version()) else None


def apply(expected):
    """Download the repository and copy it over this folder. Raises on any problem."""
    req = urllib.request.Request(ZIP_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    top = names[0].split("/")[0] + "/"
    need = {top + "app.py", top + "VERSION", top + "tracker/engine.py"}
    if not need.issubset(names):
        raise RuntimeError("downloaded files are not token-tracker")
    if zf.read(top + "VERSION").decode("utf-8").strip() != expected:
        raise RuntimeError("version mismatch, try again later")
    with tempfile.TemporaryDirectory(dir=paths.data_dir()) as tmp:
        for n in names:
            rel = n[len(top):]
            if not rel or n.endswith("/") or ".." in rel.split("/"):
                continue
            dst = os.path.join(tmp, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with zf.open(n) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
        # everything is unpacked; now swap files in one quick pass
        for base, _, files in os.walk(tmp):
            for f in files:
                src = os.path.join(base, f)
                rel = os.path.relpath(src, tmp)
                dst = paths.APP_ROOT / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    from .shortcuts import refresh_existing
    refresh_existing()


def _git(*args):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    r = subprocess.run(["git", "-C", str(paths.APP_ROOT), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120, creationflags=flags)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip()
                           else f"git {args[0]} failed")
    return r.stdout


def install(expected):
    """Bring this folder to the newest version, the way that fits how it was installed."""
    if is_git_checkout() and shutil.which("git"):
        if _git("status", "--porcelain", "--untracked-files=no").strip():
            raise RuntimeError("local file changes; commit or discard them first")
        _git("pull", "--ff-only", "origin", "main")
        if local_version() != expected:
            raise RuntimeError("pulled, but the version did not change")
        from .shortcuts import refresh_existing
        refresh_existing()
        return
    apply(expected)


def restart():
    """Start a fresh copy that waits for this one to close, then let the caller exit."""
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.name == "nt" and os.path.exists(w):
        exe = w
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe, str(paths.APP_ROOT / "app.py"), "--wait-port"], cwd=str(paths.APP_ROOT),
                     creationflags=flags, close_fds=True)
