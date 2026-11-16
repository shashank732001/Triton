@echo off
rem Triton test console: watch each step live and type commands (Triton must be running)
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" test_console.py
