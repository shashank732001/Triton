// The sharing PC's side of "Triton Camera": creates the Windows 11 virtual camera (served by
// TritonCamera.dll inside Windows' camera service) and hands it decoded webcam frames.
#pragma once

#include <cstdint>
#include <string>

namespace lfrd {

// True if this PC can offer the camera: Windows 11's virtual camera API is present and
// Install-Camera.bat has registered TritonCamera.dll. `why` says what is missing otherwise.
bool VirtualCameraAvailable(std::string* why = nullptr);

// "Triton Camera" exists between Start() and Stop() (or the end of this process).
bool StartVirtualCamera();
void StopVirtualCamera();

// One 1280x720 NV12 frame (lfrd_cam::kFrameBytes bytes) for the camera to show.
void PushCameraFrame(const uint8_t* nv12, size_t size);

}  // namespace lfrd
