@echo off
chcp 65001 >nul
rem 바탕화면과 시작 메뉴에 token-tracker 아이콘을 만듭니다 (한 번만 실행)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
where python >nul 2>nul && (python app.py --install & pause & exit /b)
where py >nul 2>nul && (py -3 app.py --install & pause & exit /b)
if exist "%USERPROFILE%\anaconda3\python.exe" ("%USERPROFILE%\anaconda3\python.exe" app.py --install & pause & exit /b)
echo Python을 찾지 못했어요. https://www.python.org 에서 설치할 때 "Add python.exe to PATH"를 체크하세요.
pause
