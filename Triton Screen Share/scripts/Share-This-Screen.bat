@echo off
rem Sender: finds a device running Show-Screen-Here and streams this screen to it.
setlocal
call "%~dp0_env.bat" || goto end
"%~dp0TritonScreenShare.exe" share %*
:end
pause
