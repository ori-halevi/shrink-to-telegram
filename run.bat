@echo off
rem Shrink To Telegram - launcher. Drag video files onto this file to add them.
setlocal
set "APPDIR=%~dp0"
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "%APPDIR%shrink_to_telegram.py" %*
) else (
    python "%APPDIR%shrink_to_telegram.py" %*
    if errorlevel 1 pause
)
