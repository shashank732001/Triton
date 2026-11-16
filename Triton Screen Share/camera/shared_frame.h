// The hand-off between Triton Screen Share (writer, on the sharing PC) and TritonCamera.dll (reader,
// running inside Windows' camera service): one NV12 frame in a named shared-memory section.
//
// The DLL creates the section: it runs as LOCAL SERVICE, which may create "Global\" objects (a
// normal app may not), and gives authenticated users write access so the app can open it.
#pragma once

#include <cstdint>

namespace lfrd_cam {

constexpr wchar_t kSectionName[] = L"Global\\TritonScreenShareCamera";
constexpr uint32_t kMagic = 0x4D414346;  // "FCAM"
constexpr uint32_t kWidth = 1280, kHeight = 720, kFps = 30;
constexpr uint32_t kFrameBytes = kWidth * kHeight * 3 / 2;  // NV12

// {7B2E6A1C-3F4D-4E8B-9A5C-2D1E0F6B8C47}: the camera's COM class (registered by Install-Camera.bat).
constexpr wchar_t kClsidString[] = L"{7B2E6A1C-3F4D-4E8B-9A5C-2D1E0F6B8C47}";

struct SharedFrame {
  uint32_t magic;
  volatile long seq;            // seqlock: odd while the writer is copying a frame in
  volatile uint64_t writtenMs;  // GetTickCount64() of the newest frame; old = camera off
  uint8_t nv12[kFrameBytes];
};

}  // namespace lfrd_cam
