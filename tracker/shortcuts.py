"""Windows shortcuts named after the app: desktop and Start menu (and optionally startup).

    python app.py --install            desktop + Start menu
    python app.py --install --startup  also start at login
    python app.py --uninstall          remove all of them
"""
import os
import subprocess
import sys
import shutil
from pathlib import Path

NAME = "token-tracker"
ROOT = Path(__file__).resolve().parent.parent
from . import paths
ICON = ROOT / "assets" / "token-tracker.ico"

# values travel as environment variables, so paths with spaces or quotes need no escaping
_PS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$sh = New-Object -ComObject WScript.Shell
$folders = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
if ($env:TT_STARTUP -eq '1') { $folders += [Environment]::GetFolderPath('Startup') }
$old = Join-Path ([Environment]::GetFolderPath('Startup')) 'TokenTracker.lnk'   # from older versions
if ($env:TT_MODE -ne 'refresh' -and (Test-Path $old)) { Remove-Item $old; Write-Output "removed $old" }
foreach ($f in $folders) {
  $p = Join-Path $f ($env:TT_NAME + '.lnk')
  if ($env:TT_MODE -eq 'refresh' -and !(Test-Path -LiteralPath $p)) { continue }
  if ($env:TT_MODE -eq 'remove') { if (Test-Path $p) { Remove-Item $p; Write-Output "removed $p" } ; continue }
  $s = $sh.CreateShortcut($p)
  $s.TargetPath = $env:TT_TARGET
  $s.Arguments = $env:TT_ARGS
  $s.WorkingDirectory = $env:TT_DIR
  $s.IconLocation = $env:TT_ICON + ',0'
  $s.Description = 'Claude Code / Codex usage limits and dashboard'
  $s.Save()
  Write-Output "created $p"
}
"""


def _pythonw():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def _run(mode, startup=False):
    if os.name != "nt":
        print("바로가기는 Windows에서만 만들 수 있어요.")
        return 1
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    icon_dir = paths.data_dir() / 'icons'
    icon_dir.mkdir(exist_ok=True)
    current_icon = icon_dir / f'token-tracker-{version}.ico'
    if mode != 'remove':
        shutil.copy2(ICON, current_icon)
    env = dict(os.environ, TT_MODE=mode, TT_NAME=NAME, TT_TARGET=str(_pythonw()),
               TT_ARGS=f'"{ROOT / "app.py"}"', TT_DIR=str(ROOT), TT_ICON=str(current_icon),
               TT_STARTUP="1" if (startup or mode == "remove") else "0")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _PS],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    if r.returncode:
        print("바로가기를 만들지 못했어요:", (r.stderr or "").strip()[:400])
        return 1
    for line in out.splitlines():
        if " " not in line:
            continue
        kind, path = line.split(" ", 1)
        print(("만듦: " if kind == "created" else "지움: ") + path)
    return 0


def install(startup=False):
    rc = _run("create", startup)
    if rc == 0:
        print(f"\n바탕화면과 시작 메뉴의 '{NAME}' 아이콘으로 실행하면 돼요."
              + (" 로그인할 때도 자동으로 켜져요." if startup else ""))
    return rc


def uninstall():
    return _run("remove")


def refresh_existing():
    """Repair existing links after an update, including an existing startup link."""
    if os.name == 'nt':
        return _run('refresh', startup=True)
    return 0
