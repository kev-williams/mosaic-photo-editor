"""Talking to the Mac: folder pickers, Finder, quitting.

When Mosaic Photo-Editor runs in its own window (launcher.py), the launcher shows the folder
picker as a sheet on that window. When it runs in a browser on the Mac, AppleScript's
'choose folder' is used. In Docker there is no picker; paths are typed or pasted.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import urllib.parse
import urllib.request

from .paths import IN_DOCKER, IS_MAC

LAUNCHER = os.environ.get("MOSAIC_LAUNCHER_URL", "").rstrip("/")
LAUNCHER_TOKEN = os.environ.get("MOSAIC_LAUNCHER_TOKEN", "")


def in_window() -> bool:
    """True when running inside the app window (not a browser tab)."""
    return bool(LAUNCHER and LAUNCHER_TOKEN)


def can_pick() -> bool:
    return in_window() or IS_MAC


def _launcher(path: str, params: dict | None = None, timeout: float = 600):
    url = f"{LAUNCHER}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    req = urllib.request.Request(url, method="POST", headers={"X-Mosaic-Token": LAUNCHER_TOKEN})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _osascript_choose(kind: str, prompt: str, start: str | None):
    safe = (start or "").replace("\\", "\\\\").replace('"', '\\"')
    loc = f' default location (POSIX file "{safe}")' if start and os.path.isdir(start) else ""
    if kind == "folder":
        script = f'POSIX path of (choose folder with prompt "{prompt}"{loc})'
    else:
        script = (f'set fs to (choose file with prompt "{prompt}"{loc} of type {{"public.image"}} '
                  'with multiple selections allowed)\nset out to ""\n'
                  'repeat with f in fs\nset out to out & POSIX path of f & linefeed\nend repeat\nout')
    try:
        r = subprocess.run(["osascript", "-e", "tell me to activate", "-e", script],
                           capture_output=True, text=True, timeout=900)
    except Exception:
        return None
    if r.returncode != 0:
        return None                                  # cancelled
    paths = [p for p in r.stdout.splitlines() if p.strip()]
    return paths or None


def pick_folder(prompt: str = "Choose a folder", start: str | None = None) -> str | None:
    """Show the system folder picker. Returns a path, or None when cancelled / not available."""
    if in_window():
        try:
            res = _launcher("/pick-folder", {"start": start or ""})
            return (res.get("paths") or [None])[0]
        except Exception:
            pass
    if IS_MAC:
        p = _osascript_choose("folder", prompt, start)
        return p[0].rstrip("/") if p else None
    return None


def pick_files(prompt: str = "Choose photos", start: str | None = None) -> list[str]:
    if in_window():
        try:
            return list(_launcher("/pick-files", {"start": start or ""}).get("paths") or [])
        except Exception:
            pass
    if IS_MAC:
        return _osascript_choose("files", prompt, start) or []
    return []


def browse(prompt: str = "Choose photos or a folder", start: str | None = None) -> list[str]:
    """One picker that accepts photos and folders (several at once). Returns paths, or [] when cancelled."""
    if in_window():
        try:
            return list(_launcher("/browse", {"start": start or ""}).get("paths") or [])
        except Exception:
            pass
    if IS_MAC:                  # AppleScript can't mix files and folders: photos only here
        return _osascript_choose("files", prompt, start) or []
    return []


def reveal(path: str) -> None:
    """Show a file or folder in Finder."""
    if IS_MAC and os.path.exists(path):
        args = ["open", "-R", path] if os.path.isfile(path) else ["open", path]
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def quit_app() -> None:
    """Close the window (or stop the server when running in a browser)."""
    if in_window():
        try:
            _launcher("/quit", timeout=3)
            return
        except Exception:
            pass
    if not IN_DOCKER:
        threading.Timer(0.8, lambda: os._exit(0)).start()
