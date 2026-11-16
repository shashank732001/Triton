#include "gst_util.h"

#include <algorithm>

#include <cstdio>
#include <map>
#include <mutex>

#include "secure.h"

namespace lfrd {

bool HasElement(const char* name) {
  GstElementFactory* f = gst_element_factory_find(name);
  if (!f) return false;
  gst_object_unref(f);
  return true;
}

void TrySet(GstElement* e, const char* prop, const std::string& value) {
  if (g_object_class_find_property(G_OBJECT_GET_CLASS(e), prop)) {
    gst_util_set_object_arg(G_OBJECT(e), prop, value.c_str());
  }
}

bool AcceptsD3D11Input(const std::string& factoryName) {
  GstElementFactory* f = gst_element_factory_find(factoryName.c_str());
  if (!f) return false;
  bool result = false;
  for (const GList* l = gst_element_factory_get_static_pad_templates(f); l; l = l->next) {
    auto* tmpl = static_cast<GstStaticPadTemplate*>(l->data);
    if (tmpl->direction != GST_PAD_SINK) continue;
    GstCaps* caps = gst_static_pad_template_get_caps(tmpl);
    for (guint i = 0; i < gst_caps_get_size(caps); ++i) {
      GstCapsFeatures* feat = gst_caps_get_features(caps, i);
      if (feat && gst_caps_features_contains(feat, "memory:D3D11Memory")) result = true;
    }
    gst_caps_unref(caps);
  }
  gst_object_unref(f);
  return result;
}

// Hardware encoders first (Intel QuickSync, NVIDIA, AMD, Media Foundation), software last.
const std::vector<const char*>& EncoderCandidates(Codec c) {
  static const std::vector<const char*> h264 = {"qsvh264enc", "nvd3d11h264enc", "nvh264enc",
                                                "amfh264enc", "mfh264enc",      "x264enc"};
  static const std::vector<const char*> h265 = {"qsvh265enc", "nvd3d11h265enc", "nvh265enc",
                                                "amfh265enc", "mfh265enc",      "x265enc"};
  return c == Codec::H264 ? h264 : h265;
}

// The GStreamer GPU H.264 decoders hold one frame before output (measured ~16.5 ms median at
// 60 fps even with a zero-reorder SPS); libav with slice threads outputs immediately (~8 ms).
// The GPU decoder also fell behind (delay creeping 40-70 ms) when the GPU was busy capturing and
// drawing, where libav stayed at 3-8 ms. libav's cost scales with bitrate, so if a slow CPU falls
// behind, the delay-based rate control lowers the bitrate and it catches up.
// HEVC stays on the GPU because software HEVC decoding is too heavy for small receivers.
const std::vector<const char*>& DecoderCandidates(Codec c) {
  static const std::vector<const char*> h264 = {"avdec_h264", "d3d11h264dec", "d3d12h264dec",
                                                "qsvh264dec", "nvh264dec"};
  static const std::vector<const char*> h265 = {"d3d11h265dec", "d3d12h265dec", "qsvh265dec",
                                                "nvh265dec", "avdec_h265"};
  return c == Codec::H264 ? h264 : h265;
}

std::string ChooseEncoder(Codec c, const std::string& forced) {
  if (!forced.empty()) return HasElement(forced.c_str()) ? forced : std::string();
  for (const char* name : EncoderCandidates(c)) {
    if (HasElement(name)) return name;
  }
  return {};
}

std::string ChooseDecoder(Codec c) {
  for (const char* name : DecoderCandidates(c)) {
    if (HasElement(name)) return name;
  }
  return {};
}

void ConfigureEncoder(GstElement* enc, const std::string& n, int kbps, int fps) {
  const std::string br = std::to_string(kbps);
  // Periodic IDR every 5 s only as a safety net; the receiver requests one on loss.
  const std::string gop = std::to_string(fps * 5);
  auto starts = [&](const char* prefix) { return n.rfind(prefix, 0) == 0; };

  if (starts("qsv")) {
    TrySet(enc, "low-latency", "true");
    TrySet(enc, "b-frames", "0");
    // Balanced preset: measured here on 1920x1200@60 screen capture, 4 encodes as fast as 7 (median
    // 7.6 ms both, p95 9.9 vs 9.2 ms) at better quality per bit; 1 cost +1.6 ms median, +4.7 ms p95.
    TrySet(enc, "target-usage", "4");
    TrySet(enc, "rate-control", "cbr");
    TrySet(enc, "bitrate", br);
    TrySet(enc, "gop-size", gop);
    TrySet(enc, "idr-interval", "0");
    TrySet(enc, "ref-frames", "1");
    TrySet(enc, "num-slice", "4");  // a lost packet only damages a quarter of the frame
  } else if (starts("nv")) {
    TrySet(enc, "preset", "p1");
    TrySet(enc, "tune", "ultra-low-latency");
    TrySet(enc, "zerolatency", "true");
    TrySet(enc, "bframes", "0");
    TrySet(enc, "b-frames", "0");
    TrySet(enc, "rc-mode", "cbr");
    TrySet(enc, "bitrate", br);
    TrySet(enc, "gop-size", gop);
  } else if (starts("amf")) {
    TrySet(enc, "usage", "ultra-low-latency");
    TrySet(enc, "preset", "speed");
    TrySet(enc, "rate-control", "cbr");
    TrySet(enc, "bitrate", br);
    TrySet(enc, "gop-size", gop);
  } else if (starts("mf")) {
    TrySet(enc, "low-latency", "true");
    TrySet(enc, "rc-mode", "cbr");
    TrySet(enc, "bitrate", br);
    TrySet(enc, "bframes", "0");
    TrySet(enc, "gop-size", gop);
    TrySet(enc, "quality-vs-speed", "0");
  } else if (n == "x264enc") {
    TrySet(enc, "tune", "zerolatency");
    TrySet(enc, "speed-preset", "ultrafast");
    TrySet(enc, "bitrate", br);
    TrySet(enc, "key-int-max", gop);
    TrySet(enc, "bframes", "0");
    TrySet(enc, "sliced-threads", "true");
  } else if (n == "x265enc") {
    TrySet(enc, "tune", "zerolatency");
    TrySet(enc, "speed-preset", "ultrafast");
    TrySet(enc, "bitrate", br);
    TrySet(enc, "key-int-max", gop);
  }
}

void SetEncoderBitrate(GstElement* enc, int kbps) {
  // All supported encoders take kbit/s; QSV/NV/x264 apply it on the next frame.
  TrySet(enc, "bitrate", std::to_string(kbps));
}

bool PollBus(GstElement* pipeline, const char* label) {
  GstBus* bus = gst_element_get_bus(pipeline);
  bool ok = true;
  while (GstMessage* msg = gst_bus_pop_filtered(
             bus, static_cast<GstMessageType>(GST_MESSAGE_ERROR | GST_MESSAGE_WARNING |
                                              GST_MESSAGE_EOS))) {
    GError* err = nullptr;
    gchar* dbg = nullptr;
    switch (GST_MESSAGE_TYPE(msg)) {
      case GST_MESSAGE_ERROR:
        gst_message_parse_error(msg, &err, &dbg);
        std::fprintf(stderr, "[%s] error: %s\n        %s\n", label, err->message, dbg ? dbg : "");
        ok = false;
        break;
      case GST_MESSAGE_WARNING: {
        // A warning can repeat many times a second (a mic's "Can't record audio fast enough"): the
        // first is printed, then at most one a minute with how many were left out, so the logs stay
        // readable.
        gst_message_parse_warning(msg, &err, &dbg);
        static std::mutex mu;
        static std::map<std::string, std::pair<uint64_t, int>> seen;  // last printed (ms), left out since
        const std::string key = std::string(label) + "|" + err->message;
        const uint64_t now = GetTickCount64();
        std::lock_guard<std::mutex> lock(mu);
        auto& [printed, skipped] = seen[key];
        if (printed && now - printed < 60'000) {
          ++skipped;
        } else {
          if (skipped) std::fprintf(stderr, "[%s] warning: %s (and %d more in the last minute)\n", label, err->message, skipped);
          else std::fprintf(stderr, "[%s] warning: %s\n", label, err->message);
          printed = now;
          skipped = 0;
        }
        break;
      }
      case GST_MESSAGE_EOS:
        std::fprintf(stderr, "[%s] stream ended\n", label);
        ok = false;
        break;
      default:
        break;
    }
    g_clear_error(&err);
    g_free(dbg);
    gst_message_unref(msg);
  }
  gst_object_unref(bus);
  return ok;
}

std::string FindAudioOutput(std::initializer_list<const char*> names, std::string* found) {
  std::string id;
  GstDeviceMonitor* mon = gst_device_monitor_new();
  gst_device_monitor_add_filter(mon, "Audio/Sink", nullptr);
  GList* devices = gst_device_monitor_get_devices(mon);
  for (GList* l = devices; l && id.empty(); l = l->next) {
    GstDevice* dev = GST_DEVICE(l->data);
    gchar* display = gst_device_get_display_name(dev);
    const std::string name = display ? display : "";
    g_free(display);
    bool match = false;
    for (const char* n : names) match |= name.find(n) != std::string::npos;
    if (!match) continue;
    // Several APIs list the same speaker; take the one wasapi2sink can open.
    GstElement* e = gst_device_create_element(dev, nullptr);
    if (!e) continue;
    GstElementFactory* f = gst_element_get_factory(e);
    if (f && std::string(GST_OBJECT_NAME(f)) == "wasapi2sink") {
      gchar* dev_id = nullptr;
      g_object_get(e, "device", &dev_id, nullptr);
      if (dev_id) id = dev_id;
      g_free(dev_id);
      if (found) *found = name;
    }
    gst_object_unref(e);
  }
  g_list_free_full(devices, gst_object_unref);
  gst_object_unref(mon);
  return id;
}

namespace {

struct RtpCrypt {
  RtpCipher cipher;
  bool encrypt;
  RtpCrypt(const uint8_t* key, bool enc) : cipher(key), encrypt(enc) {}
};

// A new buffer with the packet encrypted / decrypted (timestamps kept), or null to drop it.
GstBuffer* Transform(RtpCrypt* c, GstBuffer* in) {
  GstMapInfo src;
  if (!gst_buffer_map(in, &src, GST_MAP_READ)) return nullptr;
  GstBuffer* out = nullptr;
  if (c->encrypt || src.size > 16) {
    const gsize size = c->encrypt ? src.size + 16 : src.size - 16;
    out = gst_buffer_new_allocate(nullptr, size, nullptr);
    GstMapInfo dst;
    gst_buffer_map(out, &dst, GST_MAP_WRITE);
    const bool ok = c->encrypt ? c->cipher.Seal(src.data, src.size, dst.data) : c->cipher.Open(src.data, src.size, dst.data);
    gst_buffer_unmap(out, &dst);
    if (ok) {
      gst_buffer_copy_into(out, in, GST_BUFFER_COPY_METADATA, 0, static_cast<gsize>(-1));
    } else {
      gst_buffer_unref(out);
      out = nullptr;
    }
  }
  gst_buffer_unmap(in, &src);
  return out;
}

GstPadProbeReturn CryptProbe(GstPad*, GstPadProbeInfo* info, gpointer data) {
  auto* c = static_cast<RtpCrypt*>(data);
  if (info->type & GST_PAD_PROBE_TYPE_BUFFER) {
    GstBuffer* in = GST_PAD_PROBE_INFO_BUFFER(info);
    GstBuffer* out = Transform(c, in);
    if (!out) return GST_PAD_PROBE_DROP;
    gst_buffer_unref(in);
    GST_PAD_PROBE_INFO_DATA(info) = out;
  } else if (info->type & GST_PAD_PROBE_TYPE_BUFFER_LIST) {
    GstBufferList* list = gst_buffer_list_make_writable(GST_PAD_PROBE_INFO_BUFFER_LIST(info));
    for (guint i = 0; i < gst_buffer_list_length(list);) {
      GstBuffer* out = Transform(c, gst_buffer_list_get(list, i));
      gst_buffer_list_remove(list, i, 1);
      if (out) gst_buffer_list_insert(list, static_cast<gint>(i++), out);
    }
    GST_PAD_PROBE_INFO_DATA(info) = list;
  }
  return GST_PAD_PROBE_OK;
}

void AddCryptProbe(GstElement* pipeline, const char* element, const char* pad, const uint8_t* key, bool encrypt) {
  GstElement* e = gst_bin_get_by_name(GST_BIN(pipeline), element);
  if (!e) return;
  if (GstPad* p = gst_element_get_static_pad(e, pad)) {
    gst_pad_add_probe(p, static_cast<GstPadProbeType>(GST_PAD_PROBE_TYPE_BUFFER | GST_PAD_PROBE_TYPE_BUFFER_LIST),
                      CryptProbe, new RtpCrypt(key, encrypt), [](gpointer d) { delete static_cast<RtpCrypt*>(d); });
    gst_object_unref(p);
  }
  gst_object_unref(e);
}

}  // namespace

void EncryptRtp(GstElement* pipeline, const char* element, const char* pad, const uint8_t* key) {
  AddCryptProbe(pipeline, element, pad, key, true);
}

void DecryptRtp(GstElement* pipeline, const char* element, const char* pad, const uint8_t* key) {
  AddCryptProbe(pipeline, element, pad, key, false);
}

std::string MicFormat(const std::string& id) {
  std::string format;
  GstDeviceMonitor* mon = gst_device_monitor_new();
  gst_device_monitor_add_filter(mon, "Audio/Source", nullptr);
  GList* devices = gst_device_monitor_get_devices(mon);
  for (GList* l = devices; l && format.empty(); l = l->next) {
    GstDevice* dev = GST_DEVICE(l->data);
    GstElement* e = gst_device_create_element(dev, nullptr);
    if (!e) continue;
    GstElementFactory* f = gst_element_get_factory(e);
    gboolean loopback = FALSE;
    if (g_object_class_find_property(G_OBJECT_GET_CLASS(e), "loopback")) g_object_get(e, "loopback", &loopback, nullptr);
    gchar* devId = nullptr;
    if (f && std::string(GST_OBJECT_NAME(f)) == "wasapi2src" && !loopback) g_object_get(e, "device", &devId, nullptr);
    gst_object_unref(e);
    bool match = devId && id == devId;
    g_free(devId);
    if (!match && id.empty()) {  // the default mic
      GstStructure* props = gst_device_get_properties(dev);
      gboolean isDefault = FALSE;
      if (props) {
        gst_structure_get_boolean(props, "device.default", &isDefault);
        gst_structure_free(props);
      }
      match = isDefault && !loopback;
    }
    if (!match) continue;
    // Its first caps are the mix format; the rest are the ones it may not open.
    GstCaps* caps = gst_device_get_caps(dev);
    if (caps && gst_caps_get_size(caps) > 0) {
      const GstStructure* s = gst_caps_get_structure(caps, 0);
      const gchar* fmt = gst_structure_get_string(s, "format");
      gint rate = 0, channels = 0;
      if (fmt && gst_structure_get_int(s, "rate", &rate) && gst_structure_get_int(s, "channels", &channels)) {
        format = std::string("audio/x-raw,format=") + fmt + ",rate=" + std::to_string(rate) +
                 ",channels=" + std::to_string(channels);
      }
    }
    if (caps) gst_caps_unref(caps);
  }
  g_list_free_full(devices, gst_object_unref);
  gst_object_unref(mon);
  return format;
}

std::vector<MediaDevice> ListCaptureDevices(bool video) {
  std::vector<MediaDevice> out;
  GstDeviceMonitor* mon = gst_device_monitor_new();
  gst_device_monitor_add_filter(mon, video ? "Video/Source" : "Audio/Source", nullptr);
  GList* devices = gst_device_monitor_get_devices(mon);
  for (GList* l = devices; l; l = l->next) {
    GstDevice* dev = GST_DEVICE(l->data);
    gchar* display = gst_device_get_display_name(dev);
    const std::string name = display ? display : "";
    g_free(display);
    // Not real devices: this app's virtual ones, and Windows' "Default Audio ..." aliases.
    if (name.find("Triton") != std::string::npos || name.find("VB-Audio") != std::string::npos ||
        name.rfind("Default Audio", 0) == 0) {
      continue;
    }
    // Several APIs list the same device; keep the one the app captures with.
    GstElement* e = gst_device_create_element(dev, nullptr);
    if (!e) continue;
    GstElementFactory* f = gst_element_get_factory(e);
    const std::string factory = f ? GST_OBJECT_NAME(f) : "";
    gboolean loopback = FALSE;
    if (!video && g_object_class_find_property(G_OBJECT_GET_CLASS(e), "loopback")) g_object_get(e, "loopback", &loopback, nullptr);
    if (factory == (video ? "mfvideosrc" : "wasapi2src") && !loopback) {
      gchar* id = nullptr;
      g_object_get(e, video ? "device-path" : "device", &id, nullptr);
      if (id && std::none_of(out.begin(), out.end(), [&](const MediaDevice& d) { return d.id == id; })) {
        out.push_back({name, id});
      }
      g_free(id);
    }
    gst_object_unref(e);
  }
  g_list_free_full(devices, gst_object_unref);
  gst_object_unref(mon);
  return out;
}

}  // namespace lfrd
