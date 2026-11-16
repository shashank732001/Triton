@echo off
rem Installs the free Virtual Display Driver, which "Extend screen" needs on the PC that SHARES.
rem (The PC that only shows a screen never needs it.)
rem The driver is installed by the "VDD Control" app's Install button. That app comes from the
rem extend-driver folder next to this file (works offline, no winget needed), else from winget.
echo.
echo  Triton Screen Share - install the driver for "Extend screen"
echo  ==========================================================
echo  Note: the driver's installer adds its own certificate ("CN=Virtual Display Driver")
echo  to Windows' trusted certificates. Close this window now if you don't want that.
echo.
pause

set "VDDC=%~dp0extend-driver\VDD Control.exe"
if exist "%VDDC%" goto run

where winget >nul 2>&1 || goto nothing
rem Interactive on purpose: winget shows its terms and you answer them yourself.
winget install --id=VirtualDrivers.Virtual-Display-Driver -e
set "VDDC=%LOCALAPPDATA%\Microsoft\WinGet\Links\VDD Control.exe"
if not exist "%VDDC%" goto failed

:run
echo.
echo  Now the last step: in the "VDD Control" window that opens, click  Install
echo  (and Yes to the admin prompt). Then close VDD Control - no settings needed.
echo.
start "" "%VDDC%"
echo  When it's done, open Triton Screen Share and use "Extend screen".
echo  To check: Check-This-Device.bat should show  Extend: ready
pause
exit /b 0

:nothing
echo  This folder has no extend-driver subfolder and winget isn't available on this PC.
echo  Download "VDD.Control" (zip) from
echo      https://github.com/VirtualDrivers/Virtual-Display-Driver/releases
echo  unzip it, run "VDD Control.exe" and click Install.
pause
exit /b 1

:failed
echo  The install didn't finish - copy the messages above and send them over.
pause
exit /b 1
