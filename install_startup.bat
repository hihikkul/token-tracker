@echo off
chcp 65001 >nul
rem 아이콘을 만들고, 로그인할 때 자동으로 켜지게 합니다
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
where python >nul 2>nul && (python app.py --install --startup & pause & exit /b)
where py >nul 2>nul && (py -3 app.py --install --startup & pause & exit /b)
if exist "%USERPROFILE%\anaconda3\python.exe" ("%USERPROFILE%\anaconda3\python.exe" app.py --install --startup & pause & exit /b)
echo Python을 찾지 못했어요. https://www.python.org 에서 설치할 때 "Add python.exe to PATH"를 체크하세요.
pause
