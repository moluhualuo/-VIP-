#!/usr/bin/env python3
"""
Media playback helpers.

Step 1 of the "live playback" feature: no embedded player, no extra
dependency. We simply hand a local file to whatever the OS says plays that
kind of file, plus a best-effort hunt for common players so the user can pick
a specific one.

An embedded player (VLC) is planned as step 2; keeping this in its own module
means that upgrade will not disturb the download core.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

# Ordered by preference. First match on PATH wins when auto-detecting.
KNOWN_PLAYERS = [
    ("VLC", ["vlc"]),
    ("mpv", ["mpv"]),
    ("MPC-HC", ["mpc-hc64", "mpc-hc"]),
    ("PotPlayer", ["PotPlayerMini64", "PotPlayerMini"]),
    ("Windows Media Player", ["wmplayer"]),
]

# Common install locations that are usually absent from PATH.
_SEARCH_DIRS = [
    r"C:\Program Files\VideoLAN\VLC",
    r"C:\Program Files (x86)\VideoLAN\VLC",
    r"C:\Program Files\DAUM\PotPlayer",
    r"C:\Program Files (x86)\DAUM\PotPlayer",
    r"C:\Program Files\MPC-HC\MPC-HC64",
    r"C:\Program Files\mpv",
]


def find_players() -> list[tuple[str, str]]:
    """Return [(display_name, executable_path)] for every player we can locate."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, exes in KNOWN_PLAYERS:
        for exe in exes:
            hit = shutil.which(exe)
            if hit and hit.lower() not in seen:
                seen.add(hit.lower())
                found.append((name, hit))
                break
    for d in _SEARCH_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.lower().endswith(".exe"):
                continue
            full = os.path.join(d, fn)
            if full.lower() in seen:
                continue
            stem = os.path.splitext(fn)[0].lower()
            if not any(k in stem for k in ("vlc", "mpv", "potplayer", "mpc-hc")):
                continue
            seen.add(full.lower())
            found.append((os.path.splitext(fn)[0], full))
    return found


def open_with(path: str, player: str | None = None) -> None:
    """Open *path* in *player*, or in the OS default when player is None."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if player:
        subprocess.Popen([player, path])
        return
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - Windows default association
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def reveal_in_explorer(path: str) -> None:
    """Select a file in Explorer (Windows) or open its folder elsewhere."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        return
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    open_with(folder)
