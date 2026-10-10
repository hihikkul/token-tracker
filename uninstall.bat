@echo off
chcp 65001 >nul
rem 만든 아이콘(바탕화면, 시작 메뉴, 자동 실행)을 모두 지웁니다. 기록은 남아요
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
where python >nul 2>nul && (python app.py --uninstall & pause & exit /b)
where py >nul 2>nul && (py -3 app.py --uninstall & pause & exit /b)
if exist "%USERPROFILE%\anaconda3\python.exe" ("%USERPROFILE%\anaconda3\python.exe" app.py --uninstall & pause & exit /b)
echo Python을 찾지 못했어요. https://www.python.org 에서 설치할 때 "Add python.exe to PATH"를 체크하세요.
pause
