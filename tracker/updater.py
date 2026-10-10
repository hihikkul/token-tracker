"""Self-update from the GitHub repository.

The widget checks the VERSION file on GitHub a little after start and then every few hours.
When it is newer, the repository zip is downloaded, checked, and copied over this folder
(your records and settings live elsewhere, in %LOCALAPPDATA%\\TokenTracker), then the app
restarts itself. A git clone is left alone: use `git pull` there.
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
        raise RuntimeError("받은 파일이 이 앱이 아니에요")
    if zf.read(top + "VERSION").decode("utf-8").strip() != expected:
        raise RuntimeError("버전이 맞지 않아요 (잠시 후 다시 시도)")
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
