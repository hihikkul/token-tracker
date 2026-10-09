@echo off
chcp 65001 >nul
rem 로그인할 때 자동 실행되도록 시작프로그램에 바로가기를 만듭니다. 지우려면 uninstall_startup.bat
set "TARGET=%~dp0run.bat"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TokenTracker.lnk"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%'); $s.TargetPath='%TARGET%'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
echo 시작프로그램에 등록했어요: %LNK%
pause
