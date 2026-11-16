@echo off
rem Lists monitors, the encoder/decoder that will be used, and this device's IP addresses.
setlocal
call "%~dp0_env.bat" || goto end
"%~dp0TritonScreenShare.exe" check
:end
pause
