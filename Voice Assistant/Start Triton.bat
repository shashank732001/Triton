@echo off
rem Double-click to run Triton. The first time it sets everything up (Python environment, packages,
rem openWakeWord's files, start-at-login shortcut; 5-10 minutes, needs internet once), then starts it.
rem After that it just starts it. Needs Python 3.13 or 3.14 and the VC++ runtime installed.
title Triton Voice
cd /d "%~dp0"

if exist ".venv\triton_ready.txt" goto run

echo First run: setting up Triton. This takes 5-10 minutes and needs internet once...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_n95.ps1" -Startup
if errorlevel 1 (
    echo.
    echo Setup failed - see the messages above. Send them to Claude.
    pause
    exit /b 1
)
echo.
echo Triton is running (tray icon, under "Show hidden icons"). Say "Hey Triton".
echo It will also start by itself whenever you log in. This window closes in 15 seconds.
timeout /t 15 >nul
exit /b 0

:run
rem Already running? The assistant notices and quits the second copy, so this is always safe.
start "" wscript.exe "%~dp0start.vbs"
echo Triton started (tray icon, under "Show hidden icons"). Say "Hey Triton".
echo It takes about a minute to load everything. Log: assistant.log
timeout /t 8 >nul
