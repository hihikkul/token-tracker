@echo off
chcp 65001 >nul
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TokenTracker.lnk" 2>nul
echo 시작프로그램에서 뺐어요.
pause
