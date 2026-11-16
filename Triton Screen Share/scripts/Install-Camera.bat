@echo off
rem One-time, on the PC that SHARES (Windows 11 only): installs "Triton Camera", so apps on this PC
rem (Teams, Zoom, Camera...) can use the webcam of the PC that shows the screen.
rem Needs administrator: Windows' camera service only loads camera components that are registered for
rem the whole machine and stored in a folder it can read, so it goes to %ProgramData%\TritonScreenShare.
rem   Install-Camera.bat              install / update
rem   Install-Camera.bat /uninstall   remove
rem   add /quiet (last) to skip the "press a key" at the end
set "PAUSE=pause"
if /i "%~1"=="/quiet" set "PAUSE=rem"
if /i "%~2"=="/quiet" set "PAUSE=rem"
net session >nul 2>&1 || goto notadmin
set "DEST=%ProgramData%\TritonScreenShare"
if /i "%~1"=="/uninstall" goto uninstall

if not exist "%DEST%" mkdir "%DEST%"
if exist "%DEST%\TritonCamera.dll" regsvr32 /s /u "%DEST%\TritonCamera.dll"
copy /y "%~dp0TritonCamera.dll" "%DEST%\" >nul || goto inuse
regsvr32 /s "%DEST%\TritonCamera.dll" || goto failed
echo.
echo  Done. While the other PC shares its camera, apps here can pick "Triton Camera".
echo.
%PAUSE%
exit /b 0

:uninstall
if exist "%DEST%\TritonCamera.dll" regsvr32 /s /u "%DEST%\TritonCamera.dll"
del /q "%DEST%\TritonCamera.dll" 2>nul
echo  "Triton Camera" removed.
%PAUSE%
exit /b 0

:notadmin
echo  Right-click Install-Camera.bat and choose "Run as administrator".
%PAUSE%
exit /b 1

:inuse
echo  Couldn't replace the camera file - an app is using "Triton Camera". Close camera apps
echo  (or restart the PC) and run this again.
%PAUSE%
exit /b 1

:failed
echo  Registering the camera failed.
%PAUSE%
exit /b 1
