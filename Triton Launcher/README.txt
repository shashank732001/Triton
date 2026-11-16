TRITON LAUNCHER
===============

A lightweight fullscreen home screen for a Windows 11 TV PC: clock, weather,
live/video wallpaper, and big tiles for Files, Chrome, Brave and Settings.
Runs on Windows' built-in .NET Framework 4.8 + Edge WebView2 - nothing to install.


INSTALL ON THE NUC
------------------
1. Copy the whole TritonLauncher folder to the NUC, e.g. C:\TritonLauncher
   (keep all files together; don't run it from inside the zip; a path without
   spaces is best).
2. Double-click Install.cmd  -> launcher starts now and on every sign-in.
   It also installs screen sharing's extras (say Yes to the administrator prompt, and
   Install if Windows asks to trust the display driver): the firewall rule, the extend
   screen driver, "Triton Camera" and VB-Audio Virtual Cable (the other PC's mic).
   When a driver needs it, Windows restarts after 60 s and the launcher opens after
   sign-in ("shutdown /a" cancels the restart). Running it again skips what's installed.
   Upgrading from Nova Launcher / Lag-Free Display (the old names): Install.cmd closes
   them and removes everything they left - the old Nova Launcher folder (your config.json
   and wallpapers are carried over first), startup entries, settings, caches, firewall
   rules, the old camera, and Windows' records of them (mic/camera permissions, tray icon).
   Lag-Free Display's trusted PCs and "Start as" choice are reset. The old "NUC Launcher"
   is stopped and its startup entries removed too.
3. Stop Zoapi from starting too, otherwise both open at sign-in (see STARTUP).

If Windows shows "Windows protected your PC" the first time: More info > Run anyway.
(If you copied a zip downloaded from the internet: right-click the zip > Properties >
 Unblock, before extracting.)


STARTUP
-------
Triton starts together with the Windows shell, right after sign-in (before normal
startup apps), and shows a loading screen until the wallpaper, weather and fonts are
ready. It does this by adding itself after explorer.exe in the per-user shell list:
  HKCU\Software\Microsoft\Windows NT\CurrentVersion\Winlogon  Shell =
      explorer.exe,C:\TritonLauncher\TritonLauncher.exe
plus a backup entry in HKCU\...\Run (does nothing if Triton is already running).
Other entries in the Shell list (such as Zoapi's) are left as they are - remove
Zoapi from there or from Task Manager > Startup apps if you no longer want it.
The "Start on boot" switch and Uninstall.cmd remove only Triton's entries.

TV MODE (Triton instead of the Windows desktop)
In the Windows menu (top right): "Restart in TV mode". After the restart Windows starts
only Triton: no desktop, taskbar, Start menu, tray icons or startup apps, so more memory
and CPU are left for Triton, the voice assistant and screen sharing. Chrome, Netflix,
YouTube Music, Settings and the voice assistant all still work from Triton.
  - Back to Windows: the same menu then shows "Restart to Windows". The normal desktop
    comes back (Triton still opens there), and "Restart in TV mode" returns to it.
  - How: the Shell list above without explorer.exe (Shell = C:\...\TritonLauncher.exe).
  - Not there in TV mode: Windows notifications, Win+D / Win+Tab / the Wi-Fi flyout
    (use Triton's own Wi-Fi, volume and Open apps buttons), and Run-key startup apps.
  - If Triton crashes in TV mode it starts itself again.
  - Emergency way out: Ctrl+Alt+Del > Task Manager > Run new task > Browse to
    TritonLauncher.exe and add  --windows  (turns TV mode off and restarts).
    Also: TritonLauncher.exe --tv-on / --tv-off (no restart). Uninstall.cmd turns it off.

If Windows shows "Windows protected your PC" the first time: More info > Run anyway.
(If you copied a zip downloaded from the internet: right-click the zip > Properties >
 Unblock, before extracting.)


USING IT
--------
  Arrow keys / mouse   move between tiles      Enter / click   open
  Up arrow             go to the top bar (Wi-Fi, volume, Windows)
  Esc                  close a panel
  Ctrl + Alt + Home    bring the launcher back from any app

Top bar:
  Wi-Fi    opens the Windows Wi-Fi list
  Volume   slider + mute
  Windows  "Exit to Windows" (comes back on next restart) and the
           "Start on boot" switch (turn off to stay on normal Windows)

Open apps (last tile): every app window that's open - including minimized ones and ones
hidden behind the launcher - with a count badge. Pick one to bring it back to the front.

After 2 minutes idle it fades to just the clock and wallpaper; any key/mouse wakes it.
The wallpaper video/animation pauses while another app is in front.


SETTINGS (config.json - edit with Notepad, then restart the launcher)
--------------------------------------------------------------------
  name                 "Shashank" -> "Good evening, Shashank"
  city / country       e.g. "Erlangen" / "DE" to pin the weather to one place.
                       Empty = detect automatically (see WEATHER LOCATION below).
  latitude/longitude   optional exact location instead of city
  useDeviceLocation    true = use Windows location; false = only internet-based guess
  temperatureUnit      "celsius" or "fahrenheit"
  clock24h             true / false
  wallpaper            "auto"  = play every video in the wallpapers folder, one after
                                 another (a single video loops; no videos = first
                                 picture; nothing = live wallpaper)
                       or one fixed choice: "wallpapers/yourfile.mp4",
                       "live:aurora", "live:sunset", "live:ocean", "live:midnight"
  shuffleWallpapers    true = play the videos in random order
  ambientAfterSeconds  0 = never fade to clock-only mode
  lowPower             true = no glass blur (use if the video stutters)
  voiceAssistant       true = start "Hey Triton" with the launcher (see VOICE ASSISTANT)
  screenShare          true = start the Triton Screen Share bubble with the launcher (see SCREEN SHARING)
  debug                true = enables right-click > Inspect (DevTools)
  apps                 the tiles. Each: id, name, path, args. Examples:
     { "id": "youtube", "name": "YouTube", "path": "chrome.exe", "args": "--app=https://www.youtube.com" }
     { "id": "vlc", "name": "VLC", "path": "C:\\Program Files\\VideoLAN\\VLC\\vlc.exe", "args": "" }
  Known ids get proper icons: files, chrome, brave, settings, youtube.
  Others get a letter icon.


WEATHER LOCATION
----------------
With no city set, the launcher asks Windows where the NUC is (Wi-Fi positioning,
usually accurate to the neighbourhood) and re-checks every hour, so it follows the NUC
when it moves. Turn on BOTH in Settings > Privacy & security > Location:
  - Location services
  - Let desktop apps access your location
If location is off, it falls back to a guess from the internet connection, which is
often your provider's city (e.g. Berlin) - set "city" in that case.


VIDEO WALLPAPERS
----------------
Put .mp4/.webm files in the wallpapers folder. They play in name order (rename them
1-..., 2-..., to choose the order), crossfading into the next one; after the last one it
starts again from the first. While Chrome/Brave/etc. are in front the video pauses and
continues from the same spot when you come back. After a restart it continues with the
video it was on. A file that can't be played is skipped.


VOICE ASSISTANT ("Hey Triton")
----------------------------
voice\TritonVoice.exe starts together with the launcher and stops when you exit it. It is the
full Triton assistant, packaged so the NUC needs no Python: wake word "Hey Triton" (trained on
your voice), Whisper ears, a fine-tuned Qwen3-0.6B brain (models	riton-mini.gguf, run by
llama.cpp\llama-server.exe; voice\BRAIN.txt says which model), and Piper's voice. Hard
commands and questions go to the laptop's big brain over the Wi-Fi (see THE LAPTOP below).
Say "Hey Triton", wait for "Yes?", then for example:
  "open youtube" / "open netflix" / "close this window" / "go home" / "show open apps"
  "volume up" / "set the volume to 30" / "mute" / "pause" / "next one"
  "click play" / "scroll down" / "type hello world" / "search for rain sounds"
  "what time is it" / "why is the sky blue" (answered by the laptop)
After it answers it keeps listening ~5 s for a follow-up without "Hey Triton".
Screen sharing by voice (the laptop's screen on the TV; see SCREEN SHARING):
  "mirror my laptop" / "show my laptop here" / "share my screen"
  "extend my laptop screen here" / "use the TV as a second screen" / "switch to mirror"
  "share the sound" / "mute the laptop"          the laptop's sound on this TV
  "share my mic" / "stop sharing my mic"         this TV's mic -> the laptop's apps
  "share my camera" / "stop sharing my camera"   this TV's webcam -> the laptop's apps
  "hide the laptop screen" / "bring the laptop screen back" / "stop sharing"
The mic button in the top bar shows "Hey Triton" while it listens; click it to switch it off
(or on) until the launcher restarts. "voiceAssistant": false in config.json keeps it off.
It uses the Windows default microphone - the NUC needs one (the Rapoo webcam mic), set as
default under Settings > System > Sound > Input, and Settings > Privacy > Microphone >
"Allow desktop apps to access your microphone" must be on. It's trained on your voice
("personal voice check"), so it may ignore other people. Log: voicessistant.log. It also
has a tray icon (Pause / Quit) under "Show hidden icons".
New models: put a new brain in voice\models	riton-mini.gguf, or new wake-word files
(hey_triton.onnx + hey_triton_verifier.pkl) in voice\models, then restart the launcher.


THE LAPTOP (big brain + screen sharing; once, on the laptop)
-----------------------------------------------------------
On the laptop, double-click "Voice Assistant\Set Up Laptop.bat" once. It makes the big brain
start hidden at every sign-in, allows it through the firewall for the home network only, and
sets Triton Screen Share there to start with Windows and share automatically to paired PCs.
The big brain talks HTTPS with its own certificate; this bundle carries that certificate's
fingerprint and key (voice\settings.json "big_brain", written when the bundle was built), so
the TV only ever talks to the real laptop, encrypted. Rebuild the bundle if the laptop's
certificate or key is ever made again.
Pairing for screen sharing is the only click: the first time the laptop and TV connect, both
show the same 6-digit code - click "Codes match - pair" on both. After that everything works by
voice.


SCREEN SHARING (Triton Screen Share)
---------------------------------
display\TritonScreenShare.exe starts with the launcher as a small round bubble on the right edge
that stays on top of everything (the launcher, apps, the shared picture). Click it for the panel:
  "Show here"      show another PC's screen on this TV
  "Share screen"   send this screen to a PC that clicked "Show here"
Drag the bubble to move it; right-click it to quit. It closes when you exit the launcher.
Both PCs need the same Triton Screen Share version. The first time, the panel may offer
"Allow through firewall" - click it and say Yes. Full guide: display\README.txt.
While showing another PC, the panel can also share this PC's mic and webcam with that PC
("Share my mic" / "Share my camera"; the arrows pick which one when there are several).
Install.cmd sets up the drivers for all of this; display\Install-Everything.bat does it again
on its own.  "screenShare": false in config.json keeps it off.


COMMANDS (for scripts and the voice assistant)
----------------------------------------------
While the launcher runs, send it commands:
  TritonLauncher.exe --cmd open chrome
  TritonLauncher.exe --cmd volume 40        (also: volume up | down | mute | unmute)
  TritonLauncher.exe --cmd home
  TritonLauncher.exe --cmd exit
Or write one line per command to the named pipe  \\.\pipe\TritonLauncher


UNINSTALL
---------
Run Uninstall.cmd, then delete the folder.
