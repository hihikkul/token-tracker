@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
where python >nul 2>nul && (python app.py --doctor & pause & exit /b)
where py >nul 2>nul && (py -3 app.py --doctor & pause & exit /b)
if exist "%USERPROFILE%\anaconda3\python.exe" ("%USERPROFILE%\anaconda3\python.exe" app.py --doctor & pause & exit /b)
echo Python을 찾지 못했어요.
pause
