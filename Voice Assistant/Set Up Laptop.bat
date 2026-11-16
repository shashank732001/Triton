@echo off
rem Laptop only, once: big brain at sign-in (HTTPS), firewall for the N95, screen share shares automatically.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_laptop.ps1"
pause
