@echo off
rem Stops the launcher from starting with Windows and closes it if it is running.
"%~dp0TritonLauncher.exe" --uninstall
taskkill /im TritonLauncher.exe /f >nul 2>&1
echo.
echo  Triton Launcher removed from startup. You can now delete this folder.
echo.
timeout /t 4 >nul
