@echo off
rem Makes the launcher start when you sign in, installs screen sharing's drivers, then opens it now
rem (or restarts Windows first when a driver needs it; the launcher then opens after sign-in).
rem Run this from the folder where the launcher will live (e.g. C:\TritonLauncher).
cd /d "%~dp0"
rem Retire the old "NUC Launcher" if it is still running (its startup entries are replaced by --install).
taskkill /im NucLauncher.exe /f >nul 2>&1
rem Remove everything Nova Launcher / Lag-Free Display (the old names) left behind - including the old
rem Nova Launcher folder, after carrying its config.json and wallpapers over. Must run before --install.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0remove-old.ps1"
"%~dp0TritonLauncher.exe" --install
echo.
echo  Triton Launcher will now start right after you sign in to Windows.
echo  Tip: stop Zoapi from starting too, so both don't open at sign-in.
echo.

rem Triton Screen Share's extras: firewall rule, extend driver, "Triton Camera" and VB-Audio Virtual
rem Cable (the other PC's mic). Already-installed parts are skipped, so running this again is safe.
set "DRIVERS=0"
if not exist "%~dp0display\install-all.ps1" goto start
echo  Installing screen sharing (firewall, extend screen, camera, microphone).
echo  Windows will ask for administrator, and may ask to trust the display driver: choose Install.
echo  VB-CABLE is donationware by VB-Audio for personal use: https://vb-audio.com/Services/licensing.htm
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0display\install-all.ps1" -Quiet
set "DRIVERS=%errorlevel%"
echo.
if "%DRIVERS%"=="3010" goto restart
if not "%DRIVERS%"=="0" echo  Screen sharing setup did not finish (code %DRIVERS%). Run Install.cmd again to retry.

:start
start "" "%~dp0TritonLauncher.exe"
timeout /t 4 >nul
goto :eof

:restart
echo  A driver needs Windows to restart. Restarting in 60 seconds; Triton Launcher opens after sign-in.
echo  To restart later instead, run:  shutdown /a
shutdown /r /t 60 /c "Finishing Triton Launcher's screen sharing drivers. Triton Launcher opens after sign-in."
timeout /t 15
