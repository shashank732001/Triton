@echo off
rem Puts a GStreamer runtime on PATH for TritonScreenShare.exe (called by the other .bat files).
rem Prefers the copy bundled next to the app, so nothing has to be installed on this device.
set "GST_HERE=%~dp0gstreamer"
if not exist "%GST_HERE%\bin\gstreamer-1.0-0.dll" goto installed

set "PATH=%GST_HERE%\bin;%PATH%"
set "GST_PLUGIN_SYSTEM_PATH_1_0=%GST_HERE%\lib\gstreamer-1.0"
set "GST_PLUGIN_PATH_1_0="
set "GST_PLUGIN_SCANNER_1_0=%GST_HERE%\libexec\gstreamer-1.0\gst-plugin-scanner.exe"
rem Own plugin cache, so an installed GStreamer of another version never gets mixed in.
if not exist "%LOCALAPPDATA%\TritonScreenShare" mkdir "%LOCALAPPDATA%\TritonScreenShare"
set "GST_REGISTRY_1_0=%LOCALAPPDATA%\TritonScreenShare\gst-registry.bin"
exit /b 0

:installed
if defined GSTREAMER_1_0_ROOT_MSVC_X86_64 set "GST_BIN=%GSTREAMER_1_0_ROOT_MSVC_X86_64%bin"
if not defined GST_BIN set "GST_BIN=%ProgramFiles%\gstreamer\1.0\msvc_x86_64\bin"
if not exist "%GST_BIN%\gstreamer-1.0-0.dll" goto missing
set "PATH=%GST_BIN%;%PATH%"
exit /b 0

:missing
echo.
echo The "gstreamer" folder next to this file is missing, and GStreamer isn't installed.
echo Copy the complete TritonScreenShare folder again (including its gstreamer subfolder).
echo.
exit /b 1
