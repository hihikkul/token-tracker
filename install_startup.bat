@echo off
chcp 65001 >nul
rem 아이콘을 만들고, 로그인할 때 자동으로 켜지게 합니다
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && (python app.py --install --startup & pause & exit /b)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && (py -3 app.py --install --startup & pause & exit /b)
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && (python3 app.py --install --startup & pause & exit /b)
for %%P in ("%USERPROFILE%\anaconda3\python.exe" "%USERPROFILE%\miniconda3\python.exe") do if exist "%%~P" ("%%~P" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && ("%%~P" app.py --install --startup & pause & exit /b))
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%~D\python.exe" ("%%~D\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && ("%%~D\python.exe" app.py --install --startup & pause & exit /b))
echo Python 3.10 이상을 실행할 수 없어요. https://www.python.org/downloads/ 에서 설치하세요.
pause
