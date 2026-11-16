cmake_minimum_required(VERSION 3.20)
project(TritonScreenShare CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# Static C++ runtime: the other device only needs the GStreamer runtime, not a VC++ redistributable.
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

if(NOT DEFINED ENV{GSTREAMER_1_0_ROOT_MSVC_X86_64})
  message(FATAL_ERROR "GStreamer (MSVC 64-bit, runtime + development) is not installed.")
endif()
file(TO_CMAKE_PATH "$ENV{GSTREAMER_1_0_ROOT_MSVC_X86_64}" GST_ROOT)

add_executable(TritonScreenShare
  src/main.cpp
  src/net.cpp
  src/gst_util.cpp
  src/sender.cpp
  src/receiver.cpp
  src/sps_rewrite.cpp
  src/launcher.cpp
  src/firewall.cpp
  src/vdd.cpp
  src/vcam_host.cpp
  src/secure.cpp
  src/fec.cpp
)

target_include_directories(TritonScreenShare PRIVATE
  ${GST_ROOT}/include/gstreamer-1.0
  ${GST_ROOT}/include/glib-2.0
  ${GST_ROOT}/lib/glib-2.0/include
)
target_link_directories(TritonScreenShare PRIVATE ${GST_ROOT}/lib)
target_link_libraries(TritonScreenShare PRIVATE
  gstreamer-1.0 gstvideo-1.0 gstbase-1.0 gstcodecparsers-1.0 gobject-2.0 glib-2.0
  ws2_32 iphlpapi user32 comctl32 ole32 oleaut32 shell32 gdiplus dwmapi uxtheme mfplat bcrypt crypt32 delayimp
)
# A windowed app (no console when double-clicked) that still starts at main().
# GStreamer DLLs are delay-loaded so main() can point the process at the bundled copy first.
target_link_options(TritonScreenShare PRIVATE
  /SUBSYSTEM:WINDOWS /ENTRY:mainCRTStartup
  /DELAYLOAD:gstreamer-1.0-0.dll /DELAYLOAD:gstvideo-1.0-0.dll /DELAYLOAD:gstbase-1.0-0.dll
  /DELAYLOAD:gstcodecparsers-1.0-0.dll /DELAYLOAD:gobject-2.0-0.dll /DELAYLOAD:glib-2.0-0.dll
)
target_compile_definitions(TritonScreenShare PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX _CRT_SECURE_NO_WARNINGS)
target_compile_options(TritonScreenShare PRIVATE /W3 /utf-8 /permissive-)

# TritonCamera.dll: the Windows 11 virtual camera ("Triton Camera") loaded by Windows' camera
# service on the sharing PC. Registered machine-wide by Install-Camera.bat.
add_library(TritonCamera SHARED camera/vcam.cpp camera/vcam.def)
target_compile_definitions(TritonCamera PRIVATE UNICODE _UNICODE)
target_compile_options(TritonCamera PRIVATE /W3 /utf-8 /permissive-)
target_link_libraries(TritonCamera PRIVATE mfplat mfuuid ole32 advapi32)
