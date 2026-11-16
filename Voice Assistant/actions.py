"""
Things the voice commands do that involve other programs: Chrome, music, and Triton Launcher's
tiles / home screen (through launcher.py). Music goes through Windows' media controls (the same
thing the media keys and the volume pop-up use), so "stop music" only pauses something that is
actually playing and "play music" resumes what was paused before opening anything new.
Volume and windows live in desktop.py; which command is meant is worked out in commands.py.
"""

import asyncio
import logging
import subprocess
import webbrowser
import winreg

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaSessions,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as Playback,
)

import desktop
import launcher

log = logging.getLogger("assistant")

# Your Liked Music playlist: it starts playing by itself (the homepage would just sit there)
YOUTUBE_MUSIC_URL = "https://music.youtube.com/watch?list=LM"
APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s"
# Apps that are tiles on Triton Launcher: opened through it when it's running (it brings them to the front)
LAUNCHER_TILES = {"Google Chrome": "chrome", "Brave": "brave", "File Explorer": "files", "Settings": "settings"}


# ---------- apps ----------

def _app_path(exe):
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, APP_PATHS_KEY % exe) as key:
                return winreg.QueryValue(key, None)
        except OSError:
            continue
    return None


def open_launcher_tile(app_name):
    """Open an app through Triton Launcher if it's one of its tiles and the launcher is running."""
    tile = LAUNCHER_TILES.get(app_name)
    return bool(tile) and launcher.send(f"open {tile}")


def go_home():
    return launcher.send("home")


def show_open_apps():
    """Triton Launcher's "Open apps" panel: every open or minimized window, to switch back to."""
    return launcher.send("apps")


def open_in_chrome(url):
    chrome = _app_path("chrome.exe")
    if chrome:
        # The flag (used when this starts Chrome) shows web pages to UI Automation right away, so "click X"
        # works on the first try; an already running Chrome ignores it (tools/uia.py waits for the page instead)
        subprocess.Popen([chrome, "--force-renderer-accessibility", url])
    else:
        log.warning("Chrome not found; opening %s in the default browser", url)
        webbrowser.open(url)


# ---------- music ----------

async def _sessions():
    """Media sessions (browser tabs, Spotify...), the playing one and then Chrome's first."""
    manager = await MediaSessions.request_async()
    sessions = list(manager.get_sessions())
    return sorted(sessions, key=lambda s: (s.get_playback_info().playback_status != Playback.PLAYING,
                                           "chrome" not in s.source_app_user_model_id.lower()))


async def _resume():
    sessions = await _sessions()
    if any(s.get_playback_info().playback_status == Playback.PLAYING for s in sessions):
        return "Already playing"
    for s in sessions:
        if s.get_playback_info().playback_status == Playback.PAUSED and await s.try_play_async():
            return "Playing"
    return None


async def _pause_all():
    paused = 0
    for s in await _sessions():
        if s.get_playback_info().playback_status == Playback.PLAYING and await s.try_pause_async():
            paused += 1
    return paused


async def _skip(forward):
    for s in await _sessions():
        if await (s.try_skip_next_async() if forward else s.try_skip_previous_async()):
            return True
    return False


async def _playing():
    return any(s.get_playback_info().playback_status == Playback.PLAYING for s in await _sessions())


def something_playing():
    """Is a video or song playing right now (any app that shows in Windows' media controls)?"""
    try:
        return asyncio.run(_playing())
    except Exception:
        return False


def play_music():
    result = asyncio.run(_resume())
    if result:
        return result
    open_in_chrome(YOUTUBE_MUSIC_URL)
    log.info("Nothing to resume; opened YouTube Music")
    return "Opening YouTube Music"


def stop_music():
    paused = asyncio.run(_pause_all())
    log.info("Paused %d player(s)", paused)
    return "Paused" if paused else "Nothing playing"


def skip_track(forward=True):
    if not asyncio.run(_skip(forward)):
        desktop.press("next track" if forward else "previous track")     # players that ignore the media controls
    return "Next" if forward else "Previous"
