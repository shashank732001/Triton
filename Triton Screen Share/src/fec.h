// Forward error correction for the video stream (Reed-Solomon per frame, like Sunshine/Moonlight),
// as two GStreamer elements registered by this app:
//   tritonfecenc  (sender, after the payloader): encrypts each RTP packet (RtpCipher) and sends it at
//                 once; after a frame's last packet (or every 100) it sends parity packets, ~12%.
//   tritonfecdec  (receiver, right after udpsrc): passes packets on as they come; after a gap it holds
//                 the rest of the frame (at most 40 ms) until parity rebuilds what was lost, then lets
//                 them go in order, decrypted. Unrecoverable gaps go on as gaps (the receiver then asks
//                 for a keyframe, as before).
// Parity is computed over the encrypted packets, so it reveals nothing, and a rebuilt packet must
// still pass the AES-GCM check.
#pragma once

#include <gst/gst.h>

#include <cstdint>

namespace lfrd {

void RegisterFecElements();  // once, after gst_init
// The stream key (kSrtpKeyBytes) for an element made from "tritonfecenc" / "tritonfecdec".
void ConfigureFec(GstElement* element, const uint8_t* key);
// Packets the receivers in this process rebuilt from parity so far.
uint64_t FecRecovered();

}  // namespace lfrd
