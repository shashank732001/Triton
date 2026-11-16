// Windows Firewall: check whether this exe can receive connections on Public and Private networks,
// and fix it with one elevated netsh call.
#pragma once

#include <windows.h>

namespace lfrd {

// True if an inbound block rule targets this exe, or its allow rules don't cover both the Public
// and Private profiles (where the firewall is on). Needs COM initialized on the calling thread.
bool FirewallNeedsFix();

// Replaces every rule for this exe (including the "block" ones Windows creates when its prompt is
// dismissed) with one inbound allow rule for all profiles. Shows the UAC prompt; blocks until done.
// Returns false if the user declined or netsh failed.
bool FixFirewall(HWND owner);

}  // namespace lfrd
