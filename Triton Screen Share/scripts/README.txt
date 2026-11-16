TRITON SCREEN SHARE - quick start
=====================================

EASIEST: INSTALL EVERYTHING AT ONCE (on each PC, once)
  Double-click Install-Everything.bat and say Yes to the administrator prompt. It sets up, skipping
  whatever is already there:
    - the firewall rule (works on Public and Private Wi-Fi)
    - the extend driver (for "Extend screen")
    - "Triton Camera" (the other PC's webcam as a camera here; Windows 11 only)
    - "Triton Mic" = VB-Audio Virtual Cable (the other PC's mic as a microphone here; asks you to accept
      VB-Audio's licence first) - then RESTART the PC once.
  Windows may show "Would you like to install this device software?" for the drivers: click Install.
  Install-Everything.bat -DryRun only shows what it would do.
  Install-Everything.bat -Skip extend,camera,mic leaves those out (any of them). On a company laptop that
  only SHARES its screen, the firewall rule alone is enough: Install-Everything.bat -Skip extend,camera,mic
  VB-CABLE is donationware for personal use: keep this folder to your own PCs, don't publish it.

PAIRING AND ENCRYPTION (all your PCs need this same version)
  Strict by default: a PC that isn't paired is refused without even asking you.
  To pair two PCs (once): click "Pair a new PC" (bottom of the panel) on BOTH, then within 2 minutes
  share or show between them. Both panels show "Pair with <PC>?" and a 6-digit code. Check that both
  show the SAME code, then click "Codes match - pair" on both (Cancel if they differ: something is in
  between). From then on they connect without asking. Only pair your own PCs.
  Everything between paired PCs is encrypted with AES-256-GCM (a tamper check on every packet): video,
  sound, mic, camera, mouse, keyboard and commands. It uses the CPU's AES instructions, so it costs
  next to nothing. Keys are new for every connection. "Share automatically" only ever shares to PCs this one is paired with.
  To unpair everything: Uninstall-Everything.bat (or delete HKCU\Software\TritonScreenShare\Paired).

LOST PACKETS (Wi-Fi)
  After each frame the sharing PC sends ~12% extra "parity" packets (Reed-Solomon, as Sunshine/Moonlight
  do). A packet lost on the way is rebuilt from them instead of freezing the picture until the next
  keyframe. The stats line shows it: "loss 0.0% (0 total, 1366 rebuilt)". No delay is added unless a
  packet is lost, and then at most 40 ms for that frame.

UNINSTALL (removes everything, then delete this folder)
  Double-click Uninstall-Everything.bat and say Yes to the administrator prompt. It closes the app and
  removes: "Start with Windows", the app's settings (trusted / known PCs) and cache, Windows' records of
  it (mic/camera permissions, tray icon, app names), its firewall rules, Triton Camera, VB-Audio Virtual
  Cable ("Triton Mic") and the extend driver with its C:\VirtualDisplayDriver folder and certificate.
  Restart once afterwards, then delete this folder.
  Uninstall-Everything.bat -KeepDrivers   keeps VB-Cable and the extend driver (if other apps use them)
  Uninstall-Everything.bat -DryRun        only lists what it would remove

ONE-TIME SETUP (on every device)
  Nothing to install: GStreamer and the Visual C++ runtime are bundled in the "gstreamer"
  subfolder. Works on any 64-bit Windows 10/11 PC.
  1. Copy this whole TritonScreenShare folder anywhere (Desktop is fine). Keep the gstreamer
     subfolder next to TritonScreenShare.exe.
  2. Double-click TritonScreenShare.exe. A small round bubble appears on the edge of the screen and
     stays on top of everything. If the panel says the firewall would block the other PC, click
     "Allow through firewall" and say Yes. This works with your Wi-Fi left on PUBLIC.
     (Allow-Firewall.bat, run as administrator, does the same.)
  3. Both devices must be on the same Wi-Fi / network.

PICK THE PC FROM EITHER SIDE
  From the PC that SHARES:  bubble -> pick the other PC -> "Share screen" (as before).
  From the PC that SHOWS:   bubble -> "Show here" -> pick a PC under "SHOW A SCREEN FROM".
      That PC asks first: "Allow" (this time), "Always allow" (never asks this PC again) or
      "Don't allow". Your Wi-Fi is Public, so only allow PCs you know.
  "Start as" (under Show here) decides what the app does when it opens:
      ask each time  /  share automatically  /  always show here (goes straight to Show here).
      "share automatically": as soon as a PAIRED PC clicks "Show here", this PC shares to it by
      itself (the PC it shared with last, if several are showing). Share by hand once to pair. If that PC goes away, it shares
      again when it comes back. Clicking Stop (or Show here) pauses this until the app restarts.
  "Start with Windows" (switch under Start as): opens the app, just the bubble, when you sign in.
      Together with "share automatically" you don't have to touch anything on this PC.
  Both PCs need this version for picking from the showing side.

THE BUBBLE
  Click it         open / close the small panel. Clicking anywhere else (or Esc) closes it too.
  Drag it          move it; it snaps to the nearest side of the screen.
  Right-click it   quit. (The panel also has "Quit app".)
  Green ring       sharing or showing is running.
  The bubble and panel never appear in the shared picture.

EVERY TIME
  On the device that should SHOW the screen:   bubble -> "Show here"
  On the device whose screen you want to SHARE: bubble -> pick the other PC in the list (or type
      its IP) -> "Share screen".
  While running, the bubble's panel shows who is connected plus speed and fps, and has Stop.
  The old .bat files still work if you prefer a console.

EXTEND (use the other PC as a second screen instead of a copy of this one)
  One-time, on the PC that SHARES only: double-click Install-Extend-Driver.bat. It opens the
      "VDD Control" app from the extend-driver folder (no internet or winget needed); click Install.
      (Only PCs that use Extend need the extend-driver folder - you can leave it out elsewhere.) Note: that installer adds the
      driver's own certificate ("CN=Virtual Display Driver") to Windows' trusted certificates.
  Then: bubble -> pick the other PC -> "Extend screen". A new screen the size of the other PC's
  appears to the RIGHT of your screen; drag windows onto it. Mouse and keyboard work on it like
  on a real monitor. When you stop, the extra screen disappears and its windows come back.

USE THIS PC'S MIC ON THE OTHER PC (e.g. for Teams / Zoom on the sharing PC)
  One-time, on the PC that SHARES: run Install-Everything.bat (installs VB-Audio Virtual Cable and
      names it "Triton Mic"), then restart that PC once.
  While showing: bubble -> "Share my mic" (off by default; click again to stop).
  On the sharing PC, pick the microphone "Triton Mic (VB-Audio Virtual Cable)" in your app.
      Windows keeps the "(VB-Audio Virtual Cable)" part; it belongs to the driver and can't be renamed.
      If VB-Cable is ever reinstalled, run Install-Everything.bat again to bring the name back.
  Tip: use headphones on this PC, or the other side may hear an echo of itself.

USE THIS PC'S WEBCAM ON THE OTHER PC (Windows 11 on the sharing PC)
  One-time, on the PC that SHARES: right-click Install-Camera.bat -> Run as administrator.
      (Removes again with: Install-Camera.bat /uninstall, as administrator.)
  While showing: bubble -> "Share my camera" (off by default; click again to stop).
  On the sharing PC, pick the camera "Triton Camera" in Teams / Zoom / Camera / a browser.
  It shows a dark picture while this PC's camera isn't shared.

WHILE SHOWING (the picture has no title bar or buttons)
  The picture is always full screen. The panel stays open while you use its buttons.
  Bubble -> "Hide / show screen"     tuck the picture away and bring it back (it keeps playing)
  Bubble -> Mic / Camera dropdowns   pick which mic / webcam to share (switches over at once while
                                     sharing); the Share buttons are off when none is connected.
                                     Some webcam mics (e.g. Rapoo) refuse Windows' shared mode: the app
                                     then opens them exclusively by itself (the log says so), so other
                                     apps on THIS PC can't use that mic while it's shared.
  Bubble -> "Mute sound"             silence the sound here; it keeps streaming, so unmute is instant
  Bubble -> "Switch to extend / mirror"  change what you see without reconnecting: the other PC's
                                     screen (mirror) or an extra screen beside it (extend). Takes
                                     1-2 s. Needs the driver on the PC that SHARES (see EXTEND).
  Ctrl+Alt+Shift+I   turn mouse/keyboard forwarding on/off
  Ctrl+Alt+Shift+F   full screen / window      Ctrl+Alt+Shift+M   mute / unmute
  Ctrl+Alt+Shift+Q   disconnect

FOR THE LOWEST LAG
  - Plug one device into the router with Ethernet if you can.
  - Use 5 GHz / 6 GHz Wi-Fi, close to the router.
  - Disconnect VPNs while streaming.
  - Power Options -> Wireless Adapter Settings -> Power Saving Mode -> Maximum Performance.

READING THE NUMBERS (the grey line in the bubble's panel, or the console)
  Sender:           "received" far below "target" on a still screen is normal - the target is
                    a ceiling, a static desktop needs only 1-2 Mbps.
                    "delay" = how far behind live the picture is running because of the network
                    (normally 0-15 ms). If Wi-Fi gets slower the app lowers the quality to keep it
                    small; if it ever jumps (you'll see "pausing video"), it skips ahead so the
                    picture never runs seconds behind.
                    RTT should stay under ~10 ms. Big jumps (100+ ms) = the SENDER's Wi-Fi is
                    power-saving: set its Wireless Adapter Power Saving Mode to Maximum Performance.
  Receiver:         "audio in 100/s played 100/s" = sound is flowing. "in 0/s" = nothing is
                    playing on the sender (or audio blocked).
                    "buffer 40 ms" = the audio safety buffer. When a Wi-Fi hiccup drops sound it grows
                    by 20 ms (you'll see "audio buffer raised"), then shrinks again after 30 calm
                    seconds. If it often sits at 100+ ms, your Wi-Fi stalls a lot: see the tips above.

MOUSE
  While input forwarding is on, the receiver draws the cursor itself, in the same shape as the
  sender's (arrow, text I-beam, hand, resize...), so it moves instantly. Only what you click or
  hover over waits for the video. Turn forwarding off and the sender's cursor shows in the video.
  Both devices must run this same version (older versions refuse to connect).

TROUBLESHOOTING
  - Sound recovers by itself: if Windows resets the sound device (or audio stops for any reason)
    the app restarts its audio within ~2 s ("Sound stopped playing; restarting audio").
  - The shared picture has no taskbar button and stays on top of other windows; use the bubble's
    "Hide / show screen" to get to your own apps.
  - Picture freezes for a moment instead of showing grey/green blocks: that is on purpose. After a
    lost packet the app keeps the last good frame until a clean one arrives (usually < 0.1 s).
  - Receiver not listed: VPN or firewall. Type the receiver's IP instead; the app remembers it and
    asks that PC directly from then on, so it shows up in the list by itself next time.
  - Company laptops (firewall managed by IT) usually block everything coming IN. They can still SHARE
    to another PC (all their connections go out), but they can't be the screen that shows, and
    "Share my mic/camera" to them doesn't arrive. Picking them from the other PC ("Show a screen
    from") doesn't work either: start the share on the company laptop.
  - "Could not reach": click "Allow through firewall" in the panel on BOTH devices.
  - Run Check-This-Device.bat to see which encoder/decoder each device will use.
  - Options: Share-This-Screen.bat --bitrate 15000 --fps 60 --monitor 1 --no-audio
             Show-Screen-Here.bat --fullscreen --jitter-ms 10 --decoder d3d11h264dec
