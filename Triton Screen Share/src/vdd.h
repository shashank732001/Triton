// Extend mode: drives the free "Virtual Display Driver" (github.com/VirtualDrivers/Virtual-Display-Driver,
// installed separately on the sender) to give Windows an extra, virtual monitor that gets streamed.
//
// The driver always keeps at least one virtual monitor, so instead of creating and deleting it we
// attach it to the desktop while extending and detach it afterwards (it then isn't a monitor
// Windows puts windows on).
#pragma once

#include <string>

#include "common.h"

namespace lfrd {

// True if the driver is installed and answering on its control pipe.
bool VddAvailable();

// One line for `check`: whether the driver is there, its display device, and attached or not.
std::string VddStatus();

// Attaches the virtual monitor to the right of the existing ones, in the mode closest to
// width x height @ hz (adding that exact resolution to the driver's settings if allowed), and
// waits for Windows to list it. Fills `out` with the new monitor. Prints what it did.
bool VddAttach(int width, int height, int hz, MonitorInfo& out);

// Takes the virtual monitor off the desktop again (Windows moves its windows back). Safe to call
// when it isn't attached or the driver isn't installed.
void VddDetach();

}  // namespace lfrd
