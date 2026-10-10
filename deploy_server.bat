@echo off
chcp 65001 >nul
rem 쌀숭이 랭킹 서버를 내 Cloudflare 계정에 올립니다 (처음 한 번)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
where python >nul 2>nul && (python server\deploy.py & pause & exit /b)
where py >nul 2>nul && (py -3 server\deploy.py & pause & exit /b)
if exist "%USERPROFILE%\anaconda3\python.exe" ("%USERPROFILE%\anaconda3\python.exe" server\deploy.py & pause & exit /b)
echo Python을 찾지 못했어요.
pause
