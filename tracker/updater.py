"""Self-update from the GitHub repository.

The widget checks the VERSION file on GitHub a little after start and then every few hours.
When it is newer, the repository zip is downloaded, checked, and copied over this folder
(your records and settings live elsewhere, in %LOCALAPPDATA%\\TokenTracker), then the app
restarts itself. A folder cloned with git is updated with `git pull --ff-only` instead, so
git keeps tracking it; without git installed the update is refused.
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import paths

REPO = "hihikkul/token-tracker"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/main/VERSION"
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/main"
UA = {"User-Agent": "token-tracker-updater"}


def log(msg):
    """Timestamped trace of each update step in <data>/update.log (kept short)."""
    import time
    try:
        f = paths.data_dir() / "update.log"
        lines = f.read_text(encoding="utf-8").splitlines()[-200:] if f.exists() else []
        lines.append(time.strftime("%m-%d %H:%M:%S ") + msg)
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def local_version():
    try:
        return ((_root() or paths.APP_ROOT) / "VERSION").read_text(encoding="utf-8").strip()
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


def _root():
    """The folder that holds the app's source. A Windows exe built next to the source (for the
    taskbar) updates that folder and hands over to it; a packaged app on its own has none."""
    if getattr(sys, "frozen", False):
        beside = Path(sys.executable).resolve().parent
        return beside if (beside / "app.py").exists() else None
    return paths.APP_ROOT


def is_frozen():
    """A packaged app with no source next to it (the macOS DMG) can't overwrite itself;
    it offers the new download instead."""
    return _root() is None


def is_git_checkout():
    return bool(_root()) and (_root() / ".git").exists()


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
    # raw.githubusercontent.com can lag the zip by a few minutes; anything at least as new is fine
    got = zf.read(top + "VERSION").decode("utf-8").strip()
    if _tuple(got) < _tuple(expected):
        raise RuntimeError("GitHub is still publishing this version, try again in a few minutes")
    with tempfile.TemporaryDirectory(dir=paths.data_dir()) as tmp:
        new = Path(tmp) / "new"
        for n in names:
            rel = n[len(top):]
            if not rel or n.endswith("/") or ".." in rel.split("/"):
                continue
            dst = new.joinpath(*rel.split("/"))
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(n) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
        # everything is unpacked; now swap files in one quick pass
        _swap_in(new, _root(), Path(tmp) / "backup")


_LAUNCHER = 'TokenTracker.exe'


def cleanup_launcher(root):
    try:
        (root / (_LAUNCHER + '.old')).unlink()
    except OSError:
        pass


def _replace_file(src, dst):
    """Put a copy of src at dst in one step. Returns where a running exe was renamed aside
    (it can't be overwritten on Windows, but it can be renamed), else None."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    try:
        shutil.copy2(src, part)
        try:
            os.replace(part, dst)
            return None
        except PermissionError:
            if dst.name != _LAUNCHER:
                raise
            aside = dst.with_name(dst.name + ".old")
            try:
                aside.unlink()
            except OSError:
                pass
            os.replace(dst, aside)
            try:
                os.replace(part, dst)
            except OSError:
                os.replace(aside, dst)
                raise
            return aside
    finally:
        try:
            part.unlink()
        except OSError:
            pass


def _swap_in(new, root, backup):
    """Copy every file under new over root, all or nothing.

    Each file it replaces is backed up first; if any copy fails, the files already swapped are
    put back and the error is raised, so a half-updated folder never runs. VERSION goes last:
    as long as it still says the old version, the update is offered (and tried) again."""
    files = sorted((p.relative_to(new) for p in new.rglob("*") if p.is_file()),
                   key=lambda rel: rel.as_posix() == "VERSION")
    done = []  # (installed file, its backup or None when it is new, exe renamed aside or None)
    try:
        for rel in files:
            dst = root / rel
            saved = None
            if dst.exists():
                saved = backup / rel
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, saved)
            done.append((dst, saved, None))
            done[-1] = (dst, saved, _replace_file(new / rel, dst))
    except BaseException:
        for dst, saved, aside in reversed(done):
            try:
                if aside is not None:
                    os.replace(aside, dst)  # the original exe, renamed aside while it ran
                elif saved is not None:
                    _replace_file(saved, dst)
                elif dst.exists():
                    dst.unlink()
            except OSError as e:
                log(f"rollback could not restore {dst}: {e}")
        raise


def _git(*args):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    r = subprocess.run(["git", "-C", str(_root()), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120, creationflags=flags)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip()
                           else f"git {args[0]} failed")
    return r.stdout


def install(expected):
    """Bring this folder to the newest version, the way that fits how it was installed."""
    if is_git_checkout():
        if not shutil.which("git"):
            raise RuntimeError("git is not installed; update with git pull")
        if _git("status", "--porcelain", "--untracked-files=no").strip():
            raise RuntimeError("local file changes; commit or discard them first")
        _git("pull", "--ff-only", "origin", "main")
        if _tuple(local_version()) < _tuple(expected):
            raise RuntimeError("pulled, but the version did not change")
        return
    apply(expected)


def _python():
    exe = sys.executable
    if getattr(sys, "frozen", False):
        exe = shutil.which("pythonw") or shutil.which("python") or ""
        if not exe:
            raise OSError("Python not found")
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return w if os.name == "nt" and os.path.exists(w) else exe


def restart(port):
    """Start a fresh copy that waits for this one to close, then let the caller exit."""
    exe = _python()  # from an exe, continue with Python: the exe still holds the old code
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe, str(_root() / "app.py"), "--wait-port", "--port", str(port)], cwd=str(_root()),
                     creationflags=flags, close_fds=True)
