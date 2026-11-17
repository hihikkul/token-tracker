@echo off
chcp 65001 >nul
rem 토큰 트래커 실행 (콘솔 창 없이 위젯 + 대시보드)
cd /d "%~dp0"
set "PYEXE="
for /f "delims=" %%P in ('python -c "import sys; sys.exit(1) if sys.version_info ^< (3, 10) else print(sys.executable)" 2^>nul') do set "PYEXE=%%P"
if defined PYEXE goto launch
for /f "delims=" %%P in ('py -3 -c "import sys; sys.exit(1) if sys.version_info ^< (3, 10) else print(sys.executable)" 2^>nul') do set "PYEXE=%%P"
if defined PYEXE goto launch
for /f "delims=" %%P in ('python3 -c "import sys; sys.exit(1) if sys.version_info ^< (3, 10) else print(sys.executable)" 2^>nul') do set "PYEXE=%%P"
if defined PYEXE goto launch
for %%P in ("%USERPROFILE%\anaconda3\python.exe" "%USERPROFILE%\miniconda3\python.exe") do if exist "%%~P" ("%%~P" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && (set "PYEXE=%%~P" & goto launch))
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%~D\python.exe" ("%%~D\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && (set "PYEXE=%%~D\python.exe" & goto launch))
echo Python 3.10 이상을 실행할 수 없어요. https://www.python.org/downloads/ 에서 설치하세요.
pause
exit /b 1
:launch
for %%P in ("%PYEXE%") do if exist "%%~dpPpythonw.exe" (start "" "%%~dpPpythonw.exe" app.py %* & exit /b)
start "" "%PYEXE%" app.py %*
