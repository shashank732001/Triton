// TritonCamera.dll: "Triton Camera", a Windows 11 virtual camera on the sharing PC whose picture
// is the receiver's webcam.
//
// Windows' camera service (Frame Server) loads this DLL through COM (CLSID in shared_frame.h) and
// asks it for a Media Foundation media source: one live 1280x720 NV12 30 fps video stream. Each
// requested frame is copied from the shared-memory section that Triton Screen Share fills with the
// decoded webcam stream; while nothing arrives, a dark frame is sent so apps keep running.
//
// Structure follows Microsoft's virtual camera requirements as shown by smourier/VCamSample:
// Activator (IMFActivate) -> MediaSource (IMFMediaSourceEx, IMFSampleAllocatorControl, IKsControl)
// -> one MediaStream (IMFMediaStream2, IKsControl). All three also expose IMFAttributes.
#define NOMINMAX
#include <windows.h>
#include <initguid.h>
#include <ks.h>
#include <ksproxy.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfvirtualcamera.h>
#include <sddl.h>

#include <algorithm>
#include <atomic>
#include <cstring>
#include <mutex>
#include <string>

#include "shared_frame.h"

namespace {

using namespace lfrd_cam;

// {7B2E6A1C-3F4D-4E8B-9A5C-2D1E0F6B8C47}
const CLSID kClsid = {0x7b2e6a1c, 0x3f4d, 0x4e8b, {0x9a, 0x5c, 0x2d, 0x1e, 0x0f, 0x6b, 0x8c, 0x47}};
// PINNAME_VIDEO_CAPTURE {FB6C4281-0353-11d1-905F-0000C0CC16BA}
const GUID kPinVideoCapture = {0xfb6c4281, 0x0353, 0x11d1, {0x90, 0x5f, 0x00, 0x00, 0xc0, 0xcc, 0x16, 0xba}};

HMODULE g_module = nullptr;
std::atomic<long> g_objects{0};

// ---------------------------------------------------------------------------
// Frames from Triton Screen Share

class FrameSource {
 public:
  // Copies the newest frame into an NV12 destination (Y plane, then UV plane `uvOffset` bytes after
  // the start), or a dark frame if none arrived in the last 1.5 s.
  void Copy(uint8_t* dst, LONG pitch, size_t uvOffset) {
    std::lock_guard<std::mutex> lock(mu_);
    Open();
    const SharedFrame* f = view_;
    bool ok = false;
    if (f && f->magic == kMagic && GetTickCount64() - f->writtenMs < 1500) {
      for (int attempt = 0; attempt < 3 && !ok; ++attempt) {
        const long before = f->seq;
        if (before & 1) {
          Sleep(1);
          continue;
        }
        CopyPlanes(f->nv12, dst, pitch, uvOffset);
        ok = f->seq == before;
      }
    }
    if (!ok) Dark(dst, pitch, uvOffset);
  }

 private:
  void Open() {
    if (view_ || GetTickCount64() - lastTry_ < 1000) return;
    lastTry_ = GetTickCount64();
    // SYSTEM, LOCAL SERVICE (us) and logged-on users (Triton Screen Share) may use it.
    PSECURITY_DESCRIPTOR sd = nullptr;
    SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, FALSE};
    if (ConvertStringSecurityDescriptorToSecurityDescriptorW(L"D:(A;;GA;;;SY)(A;;GA;;;LS)(A;;GA;;;AU)",
                                                             SDDL_REVISION_1, &sd, nullptr)) {
      sa.lpSecurityDescriptor = sd;
    }
    map_ = CreateFileMappingW(INVALID_HANDLE_VALUE, &sa, PAGE_READWRITE, 0, sizeof(SharedFrame), kSectionName);
    const bool created = map_ && GetLastError() != ERROR_ALREADY_EXISTS;
    if (!map_) map_ = OpenFileMappingW(FILE_MAP_READ, FALSE, kSectionName);  // e.g. no right to create
    if (sd) LocalFree(sd);
    if (!map_) return;
    view_ = static_cast<SharedFrame*>(
        MapViewOfFile(map_, created ? FILE_MAP_WRITE : FILE_MAP_READ, 0, 0, sizeof(SharedFrame)));
    if (view_ && created) view_->magic = kMagic;
  }

  static void CopyPlanes(const uint8_t* src, uint8_t* dst, LONG pitch, size_t uvOffset) {
    for (uint32_t y = 0; y < kHeight; ++y) std::memcpy(dst + y * pitch, src + y * kWidth, kWidth);
    const uint8_t* uv = src + kWidth * kHeight;
    for (uint32_t y = 0; y < kHeight / 2; ++y) std::memcpy(dst + uvOffset + y * pitch, uv + y * kWidth, kWidth);
  }

  static void Dark(uint8_t* dst, LONG pitch, size_t uvOffset) {
    for (uint32_t y = 0; y < kHeight; ++y) std::memset(dst + y * pitch, 24, kWidth);
    for (uint32_t y = 0; y < kHeight / 2; ++y) std::memset(dst + uvOffset + y * pitch, 128, kWidth);
  }

  std::mutex mu_;
  HANDLE map_ = nullptr;
  SharedFrame* view_ = nullptr;
  uint64_t lastTry_ = 0;
};

FrameSource g_frames;

// ---------------------------------------------------------------------------
// COM plumbing

template <class T>
void SafeRelease(T*& p) {
  if (p) p->Release();
  p = nullptr;
}

// IMFAttributes, forwarded to an attribute store created with MFCreateAttributes.
#define LFRD_FORWARD_ATTRIBUTES(store)                                                                         \
  STDMETHODIMP GetItem(REFGUID k, PROPVARIANT* v) override { return store->GetItem(k, v); }                     \
  STDMETHODIMP GetItemType(REFGUID k, MF_ATTRIBUTE_TYPE* t) override { return store->GetItemType(k, t); }       \
  STDMETHODIMP CompareItem(REFGUID k, REFPROPVARIANT v, BOOL* r) override { return store->CompareItem(k, v, r); } \
  STDMETHODIMP Compare(IMFAttributes* o, MF_ATTRIBUTES_MATCH_TYPE m, BOOL* r) override {                       \
    return store->Compare(o, m, r);                                                                            \
  }                                                                                                            \
  STDMETHODIMP GetUINT32(REFGUID k, UINT32* v) override { return store->GetUINT32(k, v); }                     \
  STDMETHODIMP GetUINT64(REFGUID k, UINT64* v) override { return store->GetUINT64(k, v); }                     \
  STDMETHODIMP GetDouble(REFGUID k, double* v) override { return store->GetDouble(k, v); }                     \
  STDMETHODIMP GetGUID(REFGUID k, GUID* v) override { return store->GetGUID(k, v); }                           \
  STDMETHODIMP GetStringLength(REFGUID k, UINT32* n) override { return store->GetStringLength(k, n); }         \
  STDMETHODIMP GetString(REFGUID k, LPWSTR s, UINT32 c, UINT32* n) override { return store->GetString(k, s, c, n); } \
  STDMETHODIMP GetAllocatedString(REFGUID k, LPWSTR* s, UINT32* n) override {                                  \
    return store->GetAllocatedString(k, s, n);                                                                 \
  }                                                                                                            \
  STDMETHODIMP GetBlobSize(REFGUID k, UINT32* n) override { return store->GetBlobSize(k, n); }                 \
  STDMETHODIMP GetBlob(REFGUID k, UINT8* b, UINT32 c, UINT32* n) override { return store->GetBlob(k, b, c, n); } \
  STDMETHODIMP GetAllocatedBlob(REFGUID k, UINT8** b, UINT32* n) override { return store->GetAllocatedBlob(k, b, n); } \
  STDMETHODIMP GetUnknown(REFGUID k, REFIID i, LPVOID* p) override { return store->GetUnknown(k, i, p); }       \
  STDMETHODIMP SetItem(REFGUID k, REFPROPVARIANT v) override { return store->SetItem(k, v); }                  \
  STDMETHODIMP DeleteItem(REFGUID k) override { return store->DeleteItem(k); }                                 \
  STDMETHODIMP DeleteAllItems() override { return store->DeleteAllItems(); }                                   \
  STDMETHODIMP SetUINT32(REFGUID k, UINT32 v) override { return store->SetUINT32(k, v); }                      \
  STDMETHODIMP SetUINT64(REFGUID k, UINT64 v) override { return store->SetUINT64(k, v); }                      \
  STDMETHODIMP SetDouble(REFGUID k, double v) override { return store->SetDouble(k, v); }                      \
  STDMETHODIMP SetGUID(REFGUID k, REFGUID v) override { return store->SetGUID(k, v); }                         \
  STDMETHODIMP SetString(REFGUID k, LPCWSTR v) override { return store->SetString(k, v); }                     \
  STDMETHODIMP SetBlob(REFGUID k, const UINT8* b, UINT32 n) override { return store->SetBlob(k, b, n); }       \
  STDMETHODIMP SetUnknown(REFGUID k, IUnknown* u) override { return store->SetUnknown(k, u); }                 \
  STDMETHODIMP LockStore() override { return store->LockStore(); }                                             \
  STDMETHODIMP UnlockStore() override { return store->UnlockStore(); }                                         \
  STDMETHODIMP GetCount(UINT32* n) override { return store->GetCount(n); }                                     \
  STDMETHODIMP GetItemByIndex(UINT32 i, GUID* k, PROPVARIANT* v) override { return store->GetItemByIndex(i, k, v); } \
  STDMETHODIMP CopyAllItems(IMFAttributes* d) override { return store->CopyAllItems(d); }

#define LFRD_REFCOUNT                                                     \
  STDMETHODIMP_(ULONG) AddRef() override { return ++refs_; }              \
  STDMETHODIMP_(ULONG) Release() override {                               \
    const ULONG n = --refs_;                                              \
    if (!n) delete this;                                                  \
    return n;                                                             \
  }                                                                       \
  std::atomic<ULONG> refs_{1};

HRESULT NotFound() { return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND); }

// ---------------------------------------------------------------------------
// The one video stream

class MediaStream final : public IMFMediaStream2, public IMFAttributes, public IKsControl {
 public:
  MediaStream() { ++g_objects; }
  ~MediaStream() {
    SafeRelease(allocator_);
    SafeRelease(descriptor_);
    SafeRelease(queue_);
    SafeRelease(attrs_);
    --g_objects;
  }

  HRESULT Initialize(IMFMediaSource* source) {
    source_ = source;
    HRESULT hr = MFCreateAttributes(&attrs_, 8);
    if (FAILED(hr)) return hr;
    attrs_->SetGUID(MF_DEVICESTREAM_STREAM_CATEGORY, kPinVideoCapture);
    attrs_->SetUINT32(MF_DEVICESTREAM_STREAM_ID, 0);
    attrs_->SetUINT32(MF_DEVICESTREAM_FRAMESERVER_SHARED, 1);
    attrs_->SetUINT32(MF_DEVICESTREAM_ATTRIBUTE_FRAMESOURCE_TYPES, MFFrameSourceTypes_Color);
    if (FAILED(hr = MFCreateEventQueue(&queue_))) return hr;

    IMFMediaType* type = nullptr;
    if (FAILED(hr = MFCreateMediaType(&type))) return hr;
    type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
    MFSetAttributeSize(type, MF_MT_FRAME_SIZE, kWidth, kHeight);
    MFSetAttributeRatio(type, MF_MT_FRAME_RATE, kFps, 1);
    MFSetAttributeRatio(type, MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    type->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    type->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE);
    type->SetUINT32(MF_MT_DEFAULT_STRIDE, kWidth);
    type->SetUINT32(MF_MT_AVG_BITRATE, kFrameBytes * 8 * kFps);
    hr = MFCreateStreamDescriptor(0, 1, &type, &descriptor_);
    if (SUCCEEDED(hr)) {
      attrs_->CopyAllItems(descriptor_);  // the descriptor carries the same device-stream attributes
      IMFMediaTypeHandler* handler = nullptr;
      if (SUCCEEDED(hr = descriptor_->GetMediaTypeHandler(&handler))) {
        hr = handler->SetCurrentMediaType(type);
        handler->Release();
      }
    }
    type->Release();
    return hr;
  }

  HRESULT Start(IMFMediaType* type) {
    std::lock_guard<std::mutex> lock(mu_);
    if (!queue_) return MF_E_SHUTDOWN;
    if (allocator_ && type) allocator_->InitializeSampleAllocator(10, type);
    nextUs_ = 0;
    state_ = MF_STREAM_STATE_RUNNING;
    return queue_->QueueEventParamVar(MEStreamStarted, GUID_NULL, S_OK, nullptr);
  }

  HRESULT Stop() {
    std::lock_guard<std::mutex> lock(mu_);
    if (!queue_) return MF_E_SHUTDOWN;
    if (allocator_) allocator_->UninitializeSampleAllocator();
    state_ = MF_STREAM_STATE_STOPPED;
    return queue_->QueueEventParamVar(MEStreamStopped, GUID_NULL, S_OK, nullptr);
  }

  void Shutdown() {
    std::lock_guard<std::mutex> lock(mu_);
    if (queue_) queue_->Shutdown();
    SafeRelease(queue_);
    SafeRelease(allocator_);
    source_ = nullptr;
  }

  HRESULT SetAllocator(IUnknown* allocator) {
    std::lock_guard<std::mutex> lock(mu_);
    SafeRelease(allocator_);
    return allocator ? allocator->QueryInterface(IID_PPV_ARGS(&allocator_)) : S_OK;
  }

  // IUnknown
  STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
    if (!ppv) return E_POINTER;
    if (riid == IID_IUnknown || riid == IID_IMFMediaEventGenerator || riid == IID_IMFMediaStream ||
        riid == IID_IMFMediaStream2) {
      *ppv = static_cast<IMFMediaStream2*>(this);
    } else if (riid == IID_IMFAttributes) {
      *ppv = static_cast<IMFAttributes*>(this);
    } else if (riid == __uuidof(IKsControl)) {
      *ppv = static_cast<IKsControl*>(this);
    } else {
      *ppv = nullptr;
      return E_NOINTERFACE;
    }
    AddRef();
    return S_OK;
  }
  LFRD_REFCOUNT

  // IMFMediaEventGenerator
  STDMETHODIMP BeginGetEvent(IMFAsyncCallback* cb, IUnknown* state) override {
    std::lock_guard<std::mutex> lock(mu_);
    return queue_ ? queue_->BeginGetEvent(cb, state) : MF_E_SHUTDOWN;
  }
  STDMETHODIMP EndGetEvent(IMFAsyncResult* r, IMFMediaEvent** e) override {
    std::lock_guard<std::mutex> lock(mu_);
    return queue_ ? queue_->EndGetEvent(r, e) : MF_E_SHUTDOWN;
  }
  STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** e) override {
    IMFMediaEventQueue* q;
    {
      std::lock_guard<std::mutex> lock(mu_);
      if (!queue_) return MF_E_SHUTDOWN;
      q = queue_;
      q->AddRef();
    }
    const HRESULT hr = q->GetEvent(flags, e);  // may block: not under the lock
    q->Release();
    return hr;
  }
  STDMETHODIMP QueueEvent(MediaEventType t, REFGUID g, HRESULT s, const PROPVARIANT* v) override {
    std::lock_guard<std::mutex> lock(mu_);
    return queue_ ? queue_->QueueEventParamVar(t, g, s, v) : MF_E_SHUTDOWN;
  }

  // IMFMediaStream
  STDMETHODIMP GetMediaSource(IMFMediaSource** source) override {
    if (!source) return E_POINTER;
    std::lock_guard<std::mutex> lock(mu_);
    if (!source_) return MF_E_SHUTDOWN;
    *source = source_;
    source_->AddRef();
    return S_OK;
  }
  STDMETHODIMP GetStreamDescriptor(IMFStreamDescriptor** sd) override {
    if (!sd) return E_POINTER;
    if (!descriptor_) return MF_E_SHUTDOWN;
    *sd = descriptor_;
    descriptor_->AddRef();
    return S_OK;
  }
  STDMETHODIMP RequestSample(IUnknown* token) override {
    // Pace to 30 fps: the camera service asks again as soon as a frame is delivered.
    const uint64_t now = Microseconds();
    if (nextUs_ > now) Sleep(static_cast<DWORD>((nextUs_ - now) / 1000));
    nextUs_ = std::max<uint64_t>(nextUs_ + 1'000'000 / kFps, Microseconds());

    std::lock_guard<std::mutex> lock(mu_);
    if (!queue_) return MF_E_SHUTDOWN;
    IMFSample* sample = nullptr;
    HRESULT hr = allocator_ ? allocator_->AllocateSample(&sample) : MFCreateSample(&sample);
    if (SUCCEEDED(hr) && !allocator_) {
      IMFMediaBuffer* mem = nullptr;
      if (SUCCEEDED(hr = MFCreateMemoryBuffer(kFrameBytes, &mem))) {
        mem->SetCurrentLength(kFrameBytes);
        hr = sample->AddBuffer(mem);
        mem->Release();
      }
    }
    if (SUCCEEDED(hr)) hr = Fill(sample);
    if (SUCCEEDED(hr)) {
      sample->SetSampleTime(MFGetSystemTime());
      sample->SetSampleDuration(10'000'000 / kFps);
      if (token) sample->SetUnknown(MFSampleExtension_Token, token);
      hr = queue_->QueueEventParamUnk(MEMediaSample, GUID_NULL, S_OK, sample);
    }
    if (sample) sample->Release();
    return hr;
  }

  // IMFMediaStream2
  STDMETHODIMP SetStreamState(MF_STREAM_STATE value) override {
    if (value == state_) return S_OK;
    if (value == MF_STREAM_STATE_RUNNING) return Start(nullptr);
    if (value == MF_STREAM_STATE_STOPPED) return Stop();
    if (value == MF_STREAM_STATE_PAUSED && state_ == MF_STREAM_STATE_RUNNING) {
      state_ = value;
      return S_OK;
    }
    return MF_E_INVALID_STATE_TRANSITION;
  }
  STDMETHODIMP GetStreamState(MF_STREAM_STATE* value) override {
    if (!value) return E_POINTER;
    *value = state_;
    return S_OK;
  }

  // IKsControl: no camera controls (exposure etc.) to offer.
  STDMETHODIMP KsProperty(PKSPROPERTY, ULONG, LPVOID, ULONG, ULONG*) override { return NotFound(); }
  STDMETHODIMP KsMethod(PKSMETHOD, ULONG, LPVOID, ULONG, ULONG*) override { return NotFound(); }
  STDMETHODIMP KsEvent(PKSEVENT, ULONG, LPVOID, ULONG, ULONG*) override { return NotFound(); }

  LFRD_FORWARD_ATTRIBUTES(attrs_)

 private:
  static uint64_t Microseconds() {
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return static_cast<uint64_t>(c.QuadPart * 1'000'000 / f.QuadPart);
  }

  // Writes the newest frame into the sample's buffer, whether plain memory or a GPU surface.
  static HRESULT Fill(IMFSample* sample) {
    IMFMediaBuffer* buffer = nullptr;
    HRESULT hr = sample->GetBufferByIndex(0, &buffer);
    if (FAILED(hr)) return hr;
    IMF2DBuffer2* buf2d = nullptr;
    if (SUCCEEDED(buffer->QueryInterface(IID_PPV_ARGS(&buf2d)))) {
      BYTE *scan0 = nullptr, *start = nullptr;
      LONG pitch = 0;
      DWORD len = 0;
      if (SUCCEEDED(hr = buf2d->Lock2DSize(MF2DBuffer_LockFlags_Write, &scan0, &pitch, &start, &len))) {
        // NV12 surfaces: the UV plane follows the Y plane, which may be taller than the picture.
        const size_t planeRows = pitch > 0 ? len / (static_cast<size_t>(pitch) * 3 / 2) : kHeight;
        g_frames.Copy(scan0, pitch, static_cast<size_t>(pitch) * std::max<size_t>(planeRows, kHeight));
        buf2d->Unlock2D();
      }
      buf2d->Release();
    } else {
      BYTE* data = nullptr;
      DWORD max = 0;
      if (SUCCEEDED(hr = buffer->Lock(&data, &max, nullptr))) {
        if (max >= kFrameBytes) g_frames.Copy(data, kWidth, static_cast<size_t>(kWidth) * kHeight);
        buffer->Unlock();
        buffer->SetCurrentLength(kFrameBytes);
      }
    }
    buffer->Release();
    return hr;
  }

  std::mutex mu_;
  IMFAttributes* attrs_ = nullptr;
  IMFMediaEventQueue* queue_ = nullptr;
  IMFStreamDescriptor* descriptor_ = nullptr;
  IMFVideoSampleAllocatorEx* allocator_ = nullptr;
  IMFMediaSource* source_ = nullptr;  // not owned: the source owns this stream
  MF_STREAM_STATE state_ = MF_STREAM_STATE_STOPPED;
  uint64_t nextUs_ = 0;
};

// ---------------------------------------------------------------------------
// The media source

class MediaSource final : public IMFMediaSourceEx,
                          public IMFAttributes,
                          public IMFGetService,
                          public IKsControl,
                          public IMFSampleAllocatorControl {
 public:
  MediaSource() { ++g_objects; }
  ~MediaSource() {
    if (stream_) stream_->Release();
    SafeRelease(descriptor_);
    SafeRelease(queue_);
    SafeRelease(attrs_);
    --g_objects;
  }

  HRESULT Initialize() {
    HRESULT hr = MFCreateAttributes(&attrs_, 4);
    if (FAILED(hr)) return hr;
    stream_ = new MediaStream();
    if (FAILED(hr = stream_->Initialize(this))) return hr;
    IMFStreamDescriptor* sd = nullptr;
    stream_->GetStreamDescriptor(&sd);
    hr = MFCreatePresentationDescriptor(1, &sd, &descriptor_);
    sd->Release();
    if (FAILED(hr)) return hr;
    descriptor_->SelectStream(0);
    return MFCreateEventQueue(&queue_);
  }

  // IUnknown
  STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
    if (!ppv) return E_POINTER;
    if (riid == IID_IUnknown || riid == IID_IMFMediaEventGenerator || riid == IID_IMFMediaSource ||
        riid == IID_IMFMediaSourceEx) {
      *ppv = static_cast<IMFMediaSourceEx*>(this);
    } else if (riid == IID_IMFAttributes) {
      *ppv = static_cast<IMFAttributes*>(this);
    } else if (riid == IID_IMFGetService) {
      *ppv = static_cast<IMFGetService*>(this);
    } else if (riid == __uuidof(IKsControl)) {
      *ppv = static_cast<IKsControl*>(this);
    } else if (riid == IID_IMFSampleAllocatorControl) {
      *ppv = static_cast<IMFSampleAllocatorControl*>(this);
    } else {
      *ppv = nullptr;
      return E_NOINTERFACE;
    }
    AddRef();
    return S_OK;
  }
  LFRD_REFCOUNT

  // IMFMediaEventGenerator
  STDMETHODIMP BeginGetEvent(IMFAsyncCallback* cb, IUnknown* state) override {
    std::lock_guard<std::mutex> lock(mu_);
    return queue_ ? queue_->BeginGetEvent(cb, state) : MF_E_SHUTDOWN;
  }
  STDMETHODIMP EndGetEvent(IMFAsyncResult* r, IMFMediaEvent** e) override {
    std::lock_guard<std::mutex> lock(mu_);
    return queue_ ? queue_->EndGetEvent(r, e) : MF_E_SHUTDOWN;
  }
  STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** e) override {
    IMFMediaEventQueue* q;
    {
      std::lock_guard<std::mutex> lock(mu_);
      if (!queue_) return MF_E_SHUTDOWN;
      q = queue_;
      q->AddRef();
    }
    const HRESULT hr = q->GetEvent(flags, e);
    q->Release();
    return hr;
  }
  STDMETHODIMP QueueEvent(MediaEventType t, REFGUID g, HRESULT s, const PROPVARIANT* v) override {
    std::lock_guard<std::mutex> lock(mu_);
    return queue_ ? queue_->QueueEventParamVar(t, g, s, v) : MF_E_SHUTDOWN;
  }

  // IMFMediaSource
  STDMETHODIMP GetCharacteristics(DWORD* c) override {
    if (!c) return E_POINTER;
    *c = MFMEDIASOURCE_IS_LIVE;
    return S_OK;
  }
  STDMETHODIMP CreatePresentationDescriptor(IMFPresentationDescriptor** pd) override {
    if (!pd) return E_POINTER;
    std::lock_guard<std::mutex> lock(mu_);
    return descriptor_ ? descriptor_->Clone(pd) : MF_E_SHUTDOWN;
  }
  STDMETHODIMP Start(IMFPresentationDescriptor* pd, const GUID* timeFormat, const PROPVARIANT*) override {
    if (!pd) return E_POINTER;
    if (timeFormat && *timeFormat != GUID_NULL) return MF_E_UNSUPPORTED_TIME_FORMAT;
    std::lock_guard<std::mutex> lock(mu_);
    if (!queue_) return MF_E_SHUTDOWN;
    BOOL selected = FALSE;
    IMFStreamDescriptor* sd = nullptr;
    HRESULT hr = pd->GetStreamDescriptorByIndex(0, &selected, &sd);
    if (FAILED(hr)) return hr;
    if (selected) {
      descriptor_->SelectStream(0);
      queue_->QueueEventParamUnk(MENewStream, GUID_NULL, S_OK, static_cast<IMFMediaStream2*>(stream_));
      IMFMediaTypeHandler* handler = nullptr;
      IMFMediaType* type = nullptr;
      if (SUCCEEDED(sd->GetMediaTypeHandler(&handler))) {
        handler->GetCurrentMediaType(&type);
        handler->Release();
      }
      stream_->Start(type);
      if (type) type->Release();
    } else {
      descriptor_->DeselectStream(0);
      stream_->Stop();
    }
    sd->Release();
    PROPVARIANT time;
    PropVariantInit(&time);
    time.vt = VT_I8;
    time.hVal.QuadPart = MFGetSystemTime();
    return queue_->QueueEventParamVar(MESourceStarted, GUID_NULL, S_OK, &time);
  }
  STDMETHODIMP Stop() override {
    std::lock_guard<std::mutex> lock(mu_);
    if (!queue_) return MF_E_SHUTDOWN;
    stream_->Stop();
    descriptor_->DeselectStream(0);
    PROPVARIANT time;
    PropVariantInit(&time);
    time.vt = VT_I8;
    time.hVal.QuadPart = MFGetSystemTime();
    return queue_->QueueEventParamVar(MESourceStopped, GUID_NULL, S_OK, &time);
  }
  STDMETHODIMP Pause() override { return MF_E_INVALID_STATE_TRANSITION; }
  STDMETHODIMP Shutdown() override {
    std::lock_guard<std::mutex> lock(mu_);
    if (!queue_) return MF_E_SHUTDOWN;
    queue_->Shutdown();
    SafeRelease(queue_);
    stream_->Shutdown();
    SafeRelease(descriptor_);
    return S_OK;
  }

  // IMFMediaSourceEx
  STDMETHODIMP GetSourceAttributes(IMFAttributes** a) override {
    if (!a) return E_POINTER;
    *a = static_cast<IMFAttributes*>(this);
    AddRef();
    return S_OK;
  }
  STDMETHODIMP GetStreamAttributes(DWORD id, IMFAttributes** a) override {
    if (!a) return E_POINTER;
    if (id != 0) return E_FAIL;
    *a = static_cast<IMFAttributes*>(stream_);
    stream_->AddRef();
    return S_OK;
  }
  STDMETHODIMP SetD3DManager(IUnknown*) override { return S_OK; }  // frames are copied in on the CPU

  // IMFGetService
  STDMETHODIMP GetService(REFGUID, REFIID, LPVOID*) override { return MF_E_UNSUPPORTED_SERVICE; }

  // IKsControl
  STDMETHODIMP KsProperty(PKSPROPERTY, ULONG, LPVOID, ULONG, ULONG*) override { return NotFound(); }
  STDMETHODIMP KsMethod(PKSMETHOD, ULONG, LPVOID, ULONG, ULONG*) override { return NotFound(); }
  STDMETHODIMP KsEvent(PKSEVENT, ULONG, LPVOID, ULONG, ULONG*) override { return NotFound(); }

  // IMFSampleAllocatorControl: the camera service hands us its allocator.
  STDMETHODIMP SetDefaultAllocator(DWORD id, IUnknown* allocator) override {
    if (id != 0) return E_FAIL;
    return stream_->SetAllocator(allocator);
  }
  STDMETHODIMP GetAllocatorUsage(DWORD id, DWORD* inputId, MFSampleAllocatorUsage* usage) override {
    if (!inputId || !usage) return E_POINTER;
    if (id != 0) return E_FAIL;
    *inputId = id;
    *usage = MFSampleAllocatorUsage_UsesProvidedAllocator;
    return S_OK;
  }

  LFRD_FORWARD_ATTRIBUTES(attrs_)

 private:
  std::mutex mu_;
  IMFAttributes* attrs_ = nullptr;
  IMFMediaEventQueue* queue_ = nullptr;
  IMFPresentationDescriptor* descriptor_ = nullptr;
  MediaStream* stream_ = nullptr;
};

// ---------------------------------------------------------------------------
// What COM hands out for our CLSID: an activator that creates the source on demand.

class Activator final : public IMFActivate {
 public:
  Activator() { ++g_objects; }
  ~Activator() {
    SafeRelease(source_);
    SafeRelease(attrs_);
    --g_objects;
  }
  HRESULT Initialize() {
    HRESULT hr = MFCreateAttributes(&attrs_, 2);
    if (FAILED(hr)) return hr;
    attrs_->SetUINT32(MF_VIRTUALCAMERA_PROVIDE_ASSOCIATED_CAMERA_SOURCES, 1);
    attrs_->SetGUID(MFT_TRANSFORM_CLSID_Attribute, kClsid);
    return S_OK;
  }

  STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
    if (!ppv) return E_POINTER;
    if (riid == IID_IUnknown || riid == IID_IMFAttributes || riid == IID_IMFActivate) {
      *ppv = static_cast<IMFActivate*>(this);
      AddRef();
      return S_OK;
    }
    *ppv = nullptr;
    return E_NOINTERFACE;
  }
  LFRD_REFCOUNT

  STDMETHODIMP ActivateObject(REFIID riid, void** ppv) override {
    if (!ppv) return E_POINTER;
    std::lock_guard<std::mutex> lock(mu_);
    if (!source_) {
      auto* s = new MediaSource();
      const HRESULT hr = s->Initialize();
      if (FAILED(hr)) {
        s->Release();
        return hr;
      }
      attrs_->CopyAllItems(static_cast<IMFAttributes*>(s));
      source_ = static_cast<IMFMediaSourceEx*>(s);
    }
    return source_->QueryInterface(riid, ppv);
  }
  STDMETHODIMP ShutdownObject() override { return S_OK; }
  STDMETHODIMP DetachObject() override {
    std::lock_guard<std::mutex> lock(mu_);
    SafeRelease(source_);
    return S_OK;
  }

  LFRD_FORWARD_ATTRIBUTES(attrs_)

 private:
  std::mutex mu_;
  IMFAttributes* attrs_ = nullptr;
  IMFMediaSourceEx* source_ = nullptr;
};

class ClassFactory final : public IClassFactory {
 public:
  STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
    if (!ppv) return E_POINTER;
    if (riid == IID_IUnknown || riid == IID_IClassFactory) {
      *ppv = static_cast<IClassFactory*>(this);
      AddRef();
      return S_OK;
    }
    *ppv = nullptr;
    return E_NOINTERFACE;
  }
  STDMETHODIMP_(ULONG) AddRef() override { return 2; }  // a static object
  STDMETHODIMP_(ULONG) Release() override { return 1; }
  STDMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** ppv) override {
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (outer) return CLASS_E_NOAGGREGATION;
    auto* a = new Activator();
    HRESULT hr = a->Initialize();
    if (SUCCEEDED(hr)) hr = a->QueryInterface(riid, ppv);
    a->Release();
    return hr;
  }
  STDMETHODIMP LockServer(BOOL lock) override {
    lock ? ++g_objects : --g_objects;
    return S_OK;
  }
};

ClassFactory g_factory;

LONG SetValue(HKEY root, const std::wstring& path, const wchar_t* name, const std::wstring& value) {
  return RegSetKeyValueW(root, path.c_str(), name, REG_SZ, value.c_str(),
                         static_cast<DWORD>((value.size() + 1) * sizeof(wchar_t)));
}

}  // namespace

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
  if (reason == DLL_PROCESS_ATTACH) {
    g_module = module;
    DisableThreadLibraryCalls(module);
  }
  return TRUE;
}

STDAPI DllGetClassObject(REFCLSID clsid, REFIID riid, LPVOID* ppv) {
  if (clsid != kClsid) return CLASS_E_CLASSNOTAVAILABLE;
  return g_factory.QueryInterface(riid, ppv);
}

STDAPI DllCanUnloadNow() { return g_objects == 0 ? S_OK : S_FALSE; }

// regsvr32 (as administrator): machine-wide, because the camera service can't see per-user COM.
STDAPI DllRegisterServer() {
  wchar_t path[MAX_PATH];
  GetModuleFileNameW(g_module, path, MAX_PATH);
  const std::wstring key = std::wstring(L"Software\\Classes\\CLSID\\") + kClsidString;
  if (SetValue(HKEY_LOCAL_MACHINE, key, nullptr, L"Triton Camera") != ERROR_SUCCESS) return E_ACCESSDENIED;  // not admin
  SetValue(HKEY_LOCAL_MACHINE, key + L"\\InprocServer32", nullptr, path);
  SetValue(HKEY_LOCAL_MACHINE, key + L"\\InprocServer32", L"ThreadingModel", L"Both");
  return S_OK;
}

STDAPI DllUnregisterServer() {
  const std::wstring key = std::wstring(L"Software\\Classes\\CLSID\\") + kClsidString;
  RegDeleteTreeW(HKEY_LOCAL_MACHINE, key.c_str());
  return S_OK;
}
