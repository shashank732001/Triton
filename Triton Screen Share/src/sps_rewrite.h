// Marks an H.264 stream as "never reorders frames" so hardware decoders output each frame
// immediately instead of holding one back (measured: ~17 ms saved at 60 fps).
#pragma once

#include <gst/gst.h>

namespace lfrd {

// Installs a buffer probe on `pad`, which must carry H.264 byte-stream, alignment=au.
void InstallZeroReorderProbe(GstPad* pad);

}  // namespace lfrd
