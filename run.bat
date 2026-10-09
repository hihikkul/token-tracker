@echo off
chcp 65001 >nul
rem 토큰 트래커 실행 (콘솔 창 없이 위젯 + 대시보드)
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw app.py %* & exit /b)
where pyw >nul 2>nul && (start "" pyw -3 app.py %* & exit /b)
if exist "%USERPROFILE%\anaconda3\pythonw.exe" (start "" "%USERPROFILE%\anaconda3\pythonw.exe" app.py %* & exit /b)
echo Python을 찾지 못했어요. https://www.python.org 에서 설치하거나 PATH를 확인하세요.
pause
