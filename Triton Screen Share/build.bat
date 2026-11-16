@echo off
rem Builds TritonScreenShare.exe and assembles a copy-ready folder in dist\TritonScreenShare.
setlocal
cd /d "%~dp0"

if not defined GSTREAMER_1_0_ROOT_MSVC_X86_64 (
  echo GStreamer MSVC 64-bit runtime + development packages are required to build.
  exit /b 1
)

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq delims=" %%i in (`call "%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSDIR=%%i"
if not defined VSDIR (
  echo Visual Studio with the "Desktop development with C++" workload is required.
  exit /b 1
)
set "PATH=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer;%PATH%"
call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1

cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release || exit /b 1
cmake --build build || exit /b 1

set "OUT=dist\TritonScreenShare"
if not exist "%OUT%" mkdir "%OUT%"
copy /y build\TritonScreenShare.exe "%OUT%\" >nul
copy /y build\TritonCamera.dll "%OUT%\" >nul
copy /y scripts\*.bat "%OUT%\" >nul
copy /y scripts\README.txt "%OUT%\" >nul
copy /y scripts\*.ps1 "%OUT%\" >nul
rem VB-Audio Virtual Cable (the mic on the sharing PC), downloaded from vb-audio.com into third_party\.
rem Donationware for personal use: keep this folder to your own PCs, don't publish it.
if exist third_party\vbcable\pack\VBCABLE_Setup_x64.exe robocopy third_party\vbcable\pack "%OUT%\vbcable" /e /njh /njs /nfl /ndl /np >nul

rem For "Extend screen" on PCs without winget: carry the VDD Control installer that winget downloaded
rem (and hash-checked) on this PC. Install-Extend-Driver.bat runs it from extend-driver\.
set "VDD_PKG=%LOCALAPPDATA%\Microsoft\WinGet\Packages\VirtualDrivers.Virtual-Display-Driver_Microsoft.Winget.Source_8wekyb3d8bbwe"
if exist "%VDD_PKG%\VDD Control.exe" robocopy "%VDD_PKG%" "%OUT%\extend-driver" /e /njh /njs /nfl /ndl /np >nul

rem Bundle the GStreamer runtime so other devices need nothing installed.
where python >nul 2>&1 || goto nobundle
set "CRT_DIR="
for /d %%d in ("%VCToolsRedistDir%x64\Microsoft.VC*.CRT") do set "CRT_DIR=%%d"
python tools\bundle_gstreamer.py "%GSTREAMER_1_0_ROOT_MSVC_X86_64%." "%OUT%\gstreamer" build\TritonScreenShare.exe "%CRT_DIR%" || exit /b 1
goto bundled
:nobundle
echo Python not found: skipped bundling GStreamer; other devices will need it installed.
:bundled
echo.
echo Built %OUT%\TritonScreenShare.exe
echo Copy the whole %OUT% folder to the other device.
