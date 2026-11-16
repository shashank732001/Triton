# Triton Screen Share

A low-latency alternative to Miracast / the Windows *Wireless Display* app. One Windows app with
two modes: **Share this screen** (sender) and **Show a screen here** (receiver), plus mouse and
keyboard sent back from the receiver.

## How it works

```
SENDER                                              RECEIVER
d3d11screencapturesrc (GPU)                         udpsrc  (no jitter buffer)
 -> 1-frame leaky queue (drops stale frames)         -> rtph264depay -> h264parse
 -> d3d11convert to NV12 (GPU)                       -> avdec_h264, slice threads (~8 ms)
 -> qsvh264enc low-latency, CBR, no B-frames,        -> d3d11videosink sync=false
    4 slices                                             (draws the moment a frame is ready)
 -> SPS rewritten to "zero reorder"
 -> rtph264pay mtu=1200 -> udpsink  ==== UDP ====>
                       <==== TCP control: stats, keyframe requests, ping, mouse/keyboard ====
```

- **Adaptive bitrate.** Every 500 ms the receiver reports throughput, loss and jitter. The sender
  cuts the bitrate quickly on loss or queueing and raises it slowly while the network is clean.
- **Staying live (protocol v3).** The receiver measures how late each frame's first packet arrives
  against a 30 s no-queue baseline (`Stats::queueDelayUs`). The sender cuts the bitrate on delay,
  not just on loss, and pauses sending for about as long as any backlog over 250 ms. Socket buffers
  are small (256 KB send, 1 MB receive); the old 4 MB buffers held 4–6 s of video on a slow link.
  Captured frames are restamped with real capture time, because the source counts at a nominal
  60.000 fps against a 59.95 Hz display, which looked like a 0.8 ms/s growing lag. A 1-frame leaky
  queue before the sink keeps decoding from being paced by the display. If the picture is still
  over 400 ms late, the receiver skips ahead. Tested with a link simulator (`slow_link.py`):

  | Link                    | Before                         | Now                               |
  |-------------------------|--------------------------------|-----------------------------------|
  | 8 Mbps, 4 MB queue      | lag up to 4 s, then 70% loss   | ~10 ms avg, max ~0.2 s, no loss   |
  | 30 → 6 Mbps suddenly    | seconds                        | peak < 1 s once, ~10 ms after 2 s |
  | unlimited               | 40 Mbps                        | 40 Mbps, 2–5 ms                   |
- **Freeze, don't smear.** After any lost packet, decoded frames are held back (the last good frame
  stays up) until the next keyframe, which the receiver requests at once.
- **Loss recovery.** Any gap in the RTP sequence makes the receiver ask for a keyframe, at most
  every 250 ms.
- **Audio.** Opus, 10 ms packets, into an adaptive playout buffer (40–160 ms): it grows by 20 ms
  whenever a Wi-Fi stall makes the jitter buffer drop packets and shrinks by 10 ms after 30 calm
  seconds. A fixed 30 ms buffer dropped ~4% of packets in a stall simulation; the adaptive one ~0.
- **Local cursor.** While input is forwarded the receiver draws the cursor itself, in the shape
  the sender reports (`MSG_CURSOR`), and tells the sender to leave it out of the capture. The cursor
  then moves with zero network/video delay. The input hooks only queue events; a separate thread
  sends them, collapsing queued mouse moves into the newest one.
- **Extend (protocol v4).** `src/vdd.cpp` drives the separately installed, signed
  [Virtual Display Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver) through its
  control pipe `\\.\pipe\MTTVirtualDisplayPipe`. The driver always keeps one virtual monitor, so the
  app attaches it (`ChangeDisplaySettingsEx`, to the right of the other screens) in the receiver's
  size, which arrives in `Ready`. If that size is missing from `vdd_settings.xml`, the app adds it
  when the file is writable; otherwise it uses the closest mode. The app then shares that monitor,
  sends its real size (`MSG_SIZE`), and detaches it on exit (and at launcher start, after a crash).
  Input needs nothing new: mouse coordinates are already relative to the shared monitor.
- **Mirror/extend switch (protocol v5).** The receiver's panel sends `MSG_SET_MODE`; the sender
  stops its video pipeline, attaches or detaches the virtual screen, sends `MSG_SIZE`, and starts a
  new pipeline on the other screen. Audio, input and the connection keep running. The receiver
  notices the new RTP SSRC and resets its sequence and delay tracking, so the restart doesn't look
  like loss or lag. The sender reports `MSG_MODE` (current mode, and whether it has the driver).
- **Voice control (control pipe, 2026-11-02).** The bubble listens on `\\.\pipe\TritonScreenShare`
  (this user and SYSTEM only, local only) for one text line and answers one JSON line
  (`launcher.cpp` `OnControl`). The Triton voice assistant uses it (`Voice Assistant\tools\screenshare.py`):

  | Line | Does |
  |---|---|
  | `status` | mode, picture, extended, sound, mic, camera, paired PCs nearby |
  | `show mirror` / `show extend` | "Show here", then wait for the paired PC: it shares by itself ("share automatically") or, after 4 s, is asked to (pull); then switch to the wanted view |
  | `share mirror` / `share extend` | share this screen to a paired PC that is showing |
  | `view mirror\|extend`, `stop`, `hide`, `unhide` | like the panel's buttons |
  | `sound on\|off`, `mic on\|off`, `camera on\|off` | mute/unmute the shown PC's sound; share this PC's mic / webcam |

  A pull from a PC whose beacon fingerprint is **paired** is now allowed without a click (the
  handshake that follows still checks the real device key). Test hook `LFRD_TEST_INSTANCE=<x>` gives a
  second copy its own lock, quit event and pipe, so it can run beside the real one.
- **Discovery.** The receiver broadcasts a beacon on UDP 47989. You can also type its IP.
- **Ports.** TCP 47990 (control), UDP 47992 (video), UDP 47994 (audio, Opus 10 ms frames).
- **Encoders.** QuickSync, then NVENC, AMF, Media Foundation, then x264.

## Measured latency (this laptop, loopback, GStreamer latency tracer)

| Stage                                   | Before tuning | Now                       |
|-----------------------------------------|---------------|---------------------------|
| Sender: capture → encode → UDP          | ~10 ms        | ~10 ms (encoder ~7–9 ms)  |
| Receiver: jitter buffer                 | ~8 ms         | removed                   |
| Receiver: decode                        | ~37 ms        | ~8 ms median              |
| Receiver: UDP in → on screen            | ~50 ms        | **~10 ms median**         |

Add network time (~1–5 ms on good 5 GHz Wi-Fi, <1 ms wired) plus up to one display refresh.

## Running it

Double-click `TritonScreenShare.exe`: an always-on-top bubble (layered window, GDI+ drawn,
`WS_EX_NOACTIVATE` so clicking it never steals focus from the video) opens a borderless panel that
hides when it loses activation. Both use `WDA_EXCLUDEFROMCAPTURE`, so they never appear in the
shared picture (`LFRD_TEST_CAPTURABLE=1` turns that off for screenshots in tests). The receiver's
video window is `WS_POPUP` (no title bar), full screen by default; the panel hides/shows and
resizes it via `ReceiverCommand`. The exe is a windowed (`/SUBSYSTEM:WINDOWS`) program with
the GStreamer DLLs **delay-loaded**, so `main()` points the process at the `gstreamer` folder next
to it before any GStreamer code loads; no `.bat` is needed. With arguments (`share`, `show`,
`check`, `menu`) it attaches to the calling console and behaves like before.

The window checks Windows Firewall through `INetFwPolicy2`. If any inbound *block* rule targets
the exe (Windows creates those when its own prompt is dismissed), or the allow rules don't cover
both Public and Private, it offers one elevated `netsh` call. That call replaces all of the exe's
rules with a single inbound allow rule for every profile, so the Wi-Fi can stay on Public.

## Build (only needed on the development PC)

Requirements: Visual Studio 2022 with "Desktop development with C++", and GStreamer 1.26 MSVC
64-bit **runtime + development** installers.

```
build.bat
```

This produces `dist\TritonScreenShare\`, the folder to copy to other devices. Nothing has to be
installed on them. `tools/bundle_gstreamer.py` copies the 20 plugins the app uses, every DLL
those plugins import (found by reading the import tables), and the Visual C++ runtime DLLs.
That comes to about 63 MB instead of the 1.5 GB GStreamer install. The launcher `.bat` files
point GStreamer at this bundle and give it its own plugin cache, so an installed GStreamer of
another version never gets mixed in. See `scripts\README.txt`, which is also copied into that
folder.

## Layout

| File                  | Purpose                                                          |
|-----------------------|------------------------------------------------------------------|
| `src/main.cpp`        | Finds the bundled GStreamer, command-line options, `check`       |
| `src/launcher.cpp`    | Always-on-top bubble + borderless panel (share / show / stop)    |
| `src/firewall.cpp`    | Checks and fixes Windows Firewall rules (Public + Private)       |
| `src/sender.cpp`      | Capture/encode pipeline, rate control, input injection           |
| `src/receiver.cpp`    | Decode/display pipeline, stats, window, input forwarding hooks   |
| `src/sps_rewrite.cpp` | Marks H.264 as zero-reorder so decoders never hold frames        |
| `src/gst_util.cpp`    | Encoder/decoder selection and low-latency tuning                 |
| `src/net.cpp`         | Framed TCP control channel, UDP discovery                        |
| `src/common.h`        | Wire protocol structs and options                                |

## Not done yet / next steps

- Forward error correction, so lost packets are rebuilt without waiting for a keyframe.
- Packet pacing on the sender, to avoid Wi-Fi burst loss.
- Encryption and PIN pairing. Right now any device on the LAN can connect.
- Miracast compatibility mode.
- A small GUI instead of the console menu.
