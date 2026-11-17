@echo off
chcp 65001 >nul
rem 쌀숭이 랭킹 서버를 내 Cloudflare 계정에 올립니다 (처음 한 번)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && (python server\deploy.py & pause & exit /b)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && (py -3 server\deploy.py & pause & exit /b)
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && (python3 server\deploy.py & pause & exit /b)
for %%P in ("%USERPROFILE%\anaconda3\python.exe" "%USERPROFILE%\miniconda3\python.exe") do if exist "%%~P" ("%%~P" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && ("%%~P" server\deploy.py & pause & exit /b))
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%~D\python.exe" ("%%~D\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && ("%%~D\python.exe" server\deploy.py & pause & exit /b))
echo Python 3.10 이상을 실행할 수 없어요. https://www.python.org/downloads/ 에서 설치하세요.
pause
