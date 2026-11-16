@echo off
rem Receiver: waits for another device to share its screen to this one.
setlocal
call "%~dp0_env.bat" || goto end
"%~dp0TritonScreenShare.exe" show %*
:end
pause
