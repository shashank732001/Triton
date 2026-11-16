#include "vcam_host.h"

#include "common.h"  // windows.h

#include <mfapi.h>
#include <mfvirtualcamera.h>

#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <thread>

#include "../camera/shared_frame.h"

namespace lfrd {
namespace {

using CreateFn = HRESULT(WINAPI*)(MFVirtualCameraType, MFVirtualCameraLifetime, MFVirtualCameraAccess, LPCWSTR,
                                  LPCWSTR, const GUID*, ULONG, IMFVirtualCamera**);

// Windows 11 only: looked up at run time so the app still starts on Windows 10.
CreateFn CreateFunction() {
  static CreateFn fn = [] {
    HMODULE m = LoadLibraryW(L"mfsensorgroup.dll");
    return m ? reinterpret_cast<CreateFn>(GetProcAddress(m, "MFCreateVirtualCamera")) : nullptr;
  }();
  return fn;
}

// The camera lives on its own thread (COM + Media Foundation initialised there) until stopped.
std::mutex g_mu;
std::condition_variable g_cv;
std::thread g_thread;
bool g_stopRequested = false;

// The shared-memory section the camera DLL creates; opened lazily (it appears once an app starts
// the camera), so frames are dropped until then.
HANDLE g_map = nullptr;
lfrd_cam::SharedFrame* g_view = nullptr;
uint64_t g_lastOpenTry = 0;

}  // namespace

bool VirtualCameraAvailable(std::string* why) {
  if (!CreateFunction()) {
    if (why) *why = "needs Windows 11";
    return false;
  }
  HKEY key;
  const std::wstring path = std::wstring(L"Software\\Classes\\CLSID\\") + lfrd_cam::kClsidString + L"\\InprocServer32";
  if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, path.c_str(), 0, KEY_READ, &key) != ERROR_SUCCESS) {
    if (why) *why = "run Install-Everything.bat once (as administrator)";
    return false;
  }
  wchar_t dll[MAX_PATH] = {};
  DWORD size = sizeof(dll);
  const bool read = RegGetValueW(key, nullptr, nullptr, RRF_RT_REG_SZ, nullptr, dll, &size) == ERROR_SUCCESS;
  RegCloseKey(key);
  // The camera DLL and this app must agree on the shared-memory name (kSectionName). An install from
  // before the rename (LagFreeCamera.dll) reads another one and would only ever show a dark picture.
  const wchar_t* file = wcsrchr(dll, L'\\');
  if (!read || GetFileAttributesW(dll) == INVALID_FILE_ATTRIBUTES || _wcsicmp(file ? file + 1 : dll, L"TritonCamera.dll") != 0) {
    if (why) *why = "an older or missing camera is installed - run Install-Everything.bat again (as administrator)";
    return false;
  }
  return true;
}

bool StartVirtualCamera() {
  std::unique_lock<std::mutex> lock(g_mu);
  if (g_thread.joinable()) return true;
  if (!VirtualCameraAvailable()) return false;
  g_stopRequested = false;
  HRESULT result = E_PENDING;
  g_thread = std::thread([&result] {
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    MFStartup(MF_VERSION);
    IMFVirtualCamera* camera = nullptr;
    HRESULT hr = CreateFunction()(MFVirtualCameraType_SoftwareCameraSource, MFVirtualCameraLifetime_Session,
                                  MFVirtualCameraAccess_CurrentUser, L"Triton Camera", lfrd_cam::kClsidString,
                                  nullptr, 0, &camera);
    if (SUCCEEDED(hr)) hr = camera->Start(nullptr);
    {
      std::lock_guard<std::mutex> l(g_mu);
      result = hr;
    }
    g_cv.notify_all();
    if (SUCCEEDED(hr)) {
      std::unique_lock<std::mutex> l(g_mu);
      g_cv.wait(l, [] { return g_stopRequested; });
    }
    if (camera) {
      camera->Shutdown();  // a session camera disappears with this
      camera->Release();
    }
    MFShutdown();
    CoUninitialize();
  });
  g_cv.wait(lock, [&result] { return result != E_PENDING; });
  if (FAILED(result)) {
    lock.unlock();
    g_thread.join();
    std::fprintf(stderr, "Couldn't create \"Triton Camera\" (error 0x%08lx).\n", static_cast<unsigned long>(result));
    return false;
  }
  std::printf("\"Triton Camera\" is available to apps on this PC.\n");
  return true;
}

void StopVirtualCamera() {
  {
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_thread.joinable()) return;
    g_stopRequested = true;
  }
  g_cv.notify_all();
  g_thread.join();
  if (g_view) UnmapViewOfFile(g_view);
  if (g_map) CloseHandle(g_map);
  g_view = nullptr;
  g_map = nullptr;
}

void PushCameraFrame(const uint8_t* nv12, size_t size) {
  if (size < lfrd_cam::kFrameBytes) return;
  if (!g_view) {
    if (GetTickCount64() - g_lastOpenTry < 500) return;
    g_lastOpenTry = GetTickCount64();
    g_map = OpenFileMappingW(FILE_MAP_WRITE | FILE_MAP_READ, FALSE, lfrd_cam::kSectionName);
    if (!g_map) return;
    g_view = static_cast<lfrd_cam::SharedFrame*>(MapViewOfFile(g_map, FILE_MAP_WRITE, 0, 0, sizeof(lfrd_cam::SharedFrame)));
    if (!g_view) {
      CloseHandle(g_map);
      g_map = nullptr;
      return;
    }
  }
  // Seqlock: odd while writing, so the camera never shows a half-copied frame.
  InterlockedIncrement(&g_view->seq);
  std::memcpy(g_view->nv12, nv12, lfrd_cam::kFrameBytes);
  g_view->writtenMs = GetTickCount64();
  InterlockedIncrement(&g_view->seq);
}

}  // namespace lfrd
