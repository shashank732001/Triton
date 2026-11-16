@echo off
rem One-time setup per device: lets TritonScreenShare.exe receive connections on every network type,
rem Public included. Right-click -> "Run as administrator". (The app window has an "Allow" button
rem that does the same.) Also removes the "block" rules Windows adds when its own prompt is dismissed.
net session >nul 2>&1 || goto notadmin
netsh advfirewall firewall delete rule name=all program="%~dp0TritonScreenShare.exe" >nul 2>&1
netsh advfirewall firewall add rule name="Triton Screen Share" dir=in action=allow program="%~dp0TritonScreenShare.exe" enable=yes profile=any
echo Done. Works on Public and Private Wi-Fi.
pause
exit /b 0

:notadmin
echo Please right-click this file and choose "Run as administrator".
pause
exit /b 1
