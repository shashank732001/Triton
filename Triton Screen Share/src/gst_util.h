// GStreamer helpers: picking hardware encoders/decoders and tuning them for low latency.
#pragma once

#include <gst/gst.h>

#include <string>
#include <vector>

#include "common.h"

namespace lfrd {

bool HasElement(const char* name);
// Sets a property from its string form, but only if the element has it.
void TrySet(GstElement* e, const char* prop, const std::string& value);
bool AcceptsD3D11Input(const std::string& factoryName);

const std::vector<const char*>& EncoderCandidates(Codec c);
const std::vector<const char*>& DecoderCandidates(Codec c);
std::string ChooseEncoder(Codec c, const std::string& forced);
std::string ChooseDecoder(Codec c);

// The wasapi2sink "device" id of the first audio output whose name contains any of `names`
// (case-sensitive), e.g. "Triton Mic Input" (VB-Audio's cable). Empty if there is none. Fills `found` with its name.
// A microphone or webcam on this PC: what to show, and the id the capture element opens it by
// (wasapi2src "device" / mfvideosrc "device-path").
struct MediaDevice {
  std::string name, id;
};
// Real microphones (video=false) or webcams (video=true). Leaves out speaker loopbacks and this app's
// own virtual devices ("Triton Camera", "Triton Mic", VB-Audio's cable).
std::vector<MediaDevice> ListCaptureDevices(bool video);

std::string FindAudioOutput(std::initializer_list<const char*> names, std::string* found = nullptr);

// The format Windows shares a microphone in (its mix format, e.g. "audio/x-raw,format=F32LE,
// rate=48000,channels=2"); id empty = the default mic. wasapi2src also advertises formats it then
// can't open (AUDCLNT_E_UNSUPPORTED_FORMAT, seen with a Rapoo webcam mic), so a capture pipeline must
// ask for exactly this one. Empty if the mic isn't found.
std::string MicFormat(const std::string& id);

// Encrypts every RTP packet leaving through `element`'s `pad` (the udpsink's "sink"), or decrypts
// every one arriving there (the udpsrc's "src"; forged or damaged ones are dropped), with a stream key
// (secure.h RtpCipher, kSrtpKeyBytes).
void EncryptRtp(GstElement* pipeline, const char* element, const char* pad, const uint8_t* key);
void DecryptRtp(GstElement* pipeline, const char* element, const char* pad, const uint8_t* key);

void ConfigureEncoder(GstElement* enc, const std::string& name, int kbps, int fps);
void SetEncoderBitrate(GstElement* enc, int kbps);

// Prints pending errors/warnings. Returns false once the pipeline errored or hit EOS.
bool PollBus(GstElement* pipeline, const char* label);

}  // namespace lfrd
