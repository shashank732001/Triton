@echo off
rem One click: installs everything Triton Screen Share can use on this PC - firewall rule, the extend
rem driver, "Triton Camera" (Windows 11) and VB-Audio Virtual Cable (for the mic). Skips anything
rem already installed and asks for administrator once. See install-all.ps1 for the details.
rem   Install-Everything.bat           install
rem   Install-Everything.bat -DryRun   only show what it would do
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-all.ps1" %*
