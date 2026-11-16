@echo off
rem Removes everything Triton Screen Share set up on this PC: startup entry, settings, Windows' records of
rem it, firewall rules, Triton Camera, VB-Audio Virtual Cable ("Triton Mic") and the extend driver. Asks for
rem administrator once. Afterwards just delete this folder. See uninstall-all.ps1 for the details.
rem   Uninstall-Everything.bat                remove everything
rem   Uninstall-Everything.bat -KeepDrivers   keep VB-Cable and the extend driver (other apps may use them)
rem   Uninstall-Everything.bat -DryRun        only show what it would remove
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall-all.ps1" %*
