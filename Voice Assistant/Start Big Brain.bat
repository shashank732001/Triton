@echo off
rem Laptop only: starts Triton's big brain (brain_server.py) so the N95 can ask a bigger model.
rem Keep this window open while you want the N95 to use it; close it (or Ctrl+C) to stop.
title Triton big brain
cd /d "%~dp0"
".venv\Scripts\python.exe" brain_server.py
pause
