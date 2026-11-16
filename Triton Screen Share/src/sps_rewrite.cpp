#include "sps_rewrite.h"

#define GST_USE_UNSTABLE_API  // codecparsers is a stable library but still flagged "unstable"
#include <gst/codecparsers/gsth264parser.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <vector>

// GStreamer exports its H.264 bit writer but does not install the header, so the two functions
// we need are declared here. Size arguments go through zeroed 64-bit slots, which works whether
// the library uses guint or gsize for them; every result is re-parsed and verified before use.
extern "C" {
int gst_h264_bit_writer_sps(const GstH264SPS* sps, gboolean start_code, guint8* data, guint* size);
int gst_h264_bit_writer_convert_to_nal(guint nal_prefix_size, gboolean packetized, gboolean has_startcode,
                                       gboolean add_trailings, const guint8* raw_data, gsize raw_size,
                                       guint8* nal_data, guint* nal_size);
}

namespace lfrd {
namespace {

using Bytes = std::vector<uint8_t>;

bool SameStream(const GstH264SPS& a, const GstH264SPS& b) {
  return a.profile_idc == b.profile_idc && a.level_idc == b.level_idc && a.width == b.width &&
         a.height == b.height && a.num_ref_frames == b.num_ref_frames &&
         a.chroma_format_idc == b.chroma_format_idc && a.frame_mbs_only_flag == b.frame_mbs_only_flag &&
         a.log2_max_frame_num_minus4 == b.log2_max_frame_num_minus4 &&
         a.pic_order_cnt_type == b.pic_order_cnt_type && a.frame_cropping_flag == b.frame_cropping_flag;
}

// True if `nal` (with start code) is a valid SPS describing the same stream with zero reordering.
bool Verify(const Bytes& nal, const GstH264SPS& orig) {
  GstH264NalParser* parser = gst_h264_nal_parser_new();
  GstH264NalUnit nu;
  bool ok = false;
  GstH264ParserResult r =
      gst_h264_parser_identify_nalu(parser, nal.data(), 0, static_cast<guint>(nal.size()), &nu);
  if ((r == GST_H264_PARSER_OK || r == GST_H264_PARSER_NO_NAL_END) && nu.type == GST_H264_NAL_SPS) {
    GstH264SPS check;
    if (gst_h264_parse_sps(&nu, &check) == GST_H264_PARSER_OK) {
      ok = SameStream(check, orig) && check.vui_parameters_present_flag &&
           check.vui_parameters.bitstream_restriction_flag && check.vui_parameters.num_reorder_frames == 0;
      gst_h264_sps_clear(&check);
    }
  }
  gst_h264_nal_parser_free(parser);
  return ok;
}

Bytes RewriteSps(const GstH264SPS& orig) {
  GstH264SPS sps = orig;
  GstH264VUIParams& v = sps.vui_parameters;
  if (!sps.vui_parameters_present_flag) {
    std::memset(&v, 0, sizeof(v));
    sps.vui_parameters_present_flag = 1;
  }
  if (!v.bitstream_restriction_flag) {
    // Spec defaults for the fields that come along with bitstream_restriction.
    v.bitstream_restriction_flag = 1;
    v.motion_vectors_over_pic_boundaries_flag = 1;
    v.max_bytes_per_pic_denom = 2;
    v.max_bits_per_mb_denom = 1;
    v.log2_max_mv_length_horizontal = 15;
    v.log2_max_mv_length_vertical = 15;
  }
  v.num_reorder_frames = 0;
  v.max_dec_frame_buffering = std::max<guint32>(sps.num_ref_frames, 1);

  uint8_t raw[512];
  uint64_t rawSize = sizeof(raw);
  if (gst_h264_bit_writer_sps(&sps, FALSE, raw, reinterpret_cast<guint*>(&rawSize)) == 0) {
    // convert_to_nal adds the start code and emulation-prevention bytes. Its raw size is
    // documented in bits in some versions and bytes in others; try both (bits must be whole bytes).
    for (uint64_t inSize : {rawSize * 8, rawSize}) {
      if (inSize % 8 != 0) continue;
      Bytes out(1024);
      uint64_t outSize = out.size();
      if (gst_h264_bit_writer_convert_to_nal(4, FALSE, FALSE, FALSE, raw, static_cast<gsize>(inSize), out.data(),
                                             reinterpret_cast<guint*>(&outSize)) == 0 &&
          outSize > 4 && outSize <= out.size()) {
        out.resize(static_cast<size_t>(outSize));
        if (Verify(out, orig)) return out;
      }
    }
  }
  // Some versions emit a complete NAL directly when asked for a start code.
  Bytes direct(512);
  uint64_t directSize = direct.size();
  if (gst_h264_bit_writer_sps(&sps, TRUE, direct.data(), reinterpret_cast<guint*>(&directSize)) == 0 &&
      directSize > 4 && directSize <= direct.size()) {
    direct.resize(static_cast<size_t>(directSize));
    if (Verify(direct, orig)) return direct;
  }
  return {};
}

struct ProbeState {
  GstH264NalParser* parser = gst_h264_nal_parser_new();
  Bytes lastIn, lastOut;  // the SPS repeats with every keyframe; rewrite it once
  bool reported = false;
};

void FreeState(gpointer p) {
  auto* st = static_cast<ProbeState*>(p);
  gst_h264_nal_parser_free(st->parser);
  delete st;
}

GstPadProbeReturn OnBuffer(GstPad*, GstPadProbeInfo* info, gpointer user) {
  auto* st = static_cast<ProbeState*>(user);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  GstMapInfo map;
  if (!gst_buffer_map(buf, &map, GST_MAP_READ)) return GST_PAD_PROBE_OK;

  GstH264NalUnit nu;
  guint offset = 0;
  bool found = false;
  for (;;) {
    GstH264ParserResult r =
        gst_h264_parser_identify_nalu(st->parser, map.data, offset, static_cast<guint>(map.size), &nu);
    if (r != GST_H264_PARSER_OK && r != GST_H264_PARSER_NO_NAL_END) break;
    if (nu.type == GST_H264_NAL_SPS) {
      found = true;
      break;
    }
    if (r == GST_H264_PARSER_NO_NAL_END) break;
    offset = nu.offset + nu.size;
  }
  if (!found) {
    gst_buffer_unmap(buf, &map);
    return GST_PAD_PROBE_OK;
  }

  const gsize start = nu.sc_offset, end = nu.offset + nu.size;
  Bytes in(map.data + start, map.data + end);
  if (in != st->lastIn) {
    st->lastIn = in;
    st->lastOut.clear();
    GstH264SPS sps;
    if (gst_h264_parse_sps(&nu, &sps) == GST_H264_PARSER_OK) {
      st->lastOut = RewriteSps(sps);
      gst_h264_sps_clear(&sps);
    }
    if (!st->reported) {
      std::printf(st->lastOut.empty() ? "Note: could not mark the stream zero-reorder; the receiver may add ~1 frame.\n"
                                      : "Stream marked zero-reorder (receiver shows each frame immediately).\n");
      st->reported = true;
    }
  }
  if (st->lastOut.empty()) {
    gst_buffer_unmap(buf, &map);
    return GST_PAD_PROBE_OK;
  }

  const Bytes& sps = st->lastOut;
  GstBuffer* out = gst_buffer_new_allocate(nullptr, map.size - (end - start) + sps.size(), nullptr);
  gst_buffer_fill(out, 0, map.data, start);
  gst_buffer_fill(out, start, sps.data(), sps.size());
  gst_buffer_fill(out, start + sps.size(), map.data + end, map.size - end);
  gst_buffer_unmap(buf, &map);
  gst_buffer_copy_into(out, buf, GST_BUFFER_COPY_METADATA, 0, -1);
  gst_buffer_unref(buf);
  GST_PAD_PROBE_INFO_DATA(info) = out;
  return GST_PAD_PROBE_OK;
}

}  // namespace

void InstallZeroReorderProbe(GstPad* pad) {
  gst_pad_add_probe(pad, GST_PAD_PROBE_TYPE_BUFFER, OnBuffer, new ProbeState, FreeState);
}

}  // namespace lfrd
