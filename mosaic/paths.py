"""Where Mosaic Photo-Editor keeps files.

Photos (yours, never inside the app folder):
    ~/Pictures/Mosaic Photo-Editor/Input     originals you want to work on (optional)
    ~/Pictures/Mosaic Photo-Editor/Output    converted files

App data (models, history, settings) and logs:
    macOS   ~/Library/Application Support/Mosaic Photo-Editor   and  ~/Library/Logs/Mosaic Photo-Editor
    Linux   ~/.local/share/mosaic-photo-editor
    Docker  /state

Every location can be overridden with an environment variable (MOSAIC_PHOTOS_DIR,
MOSAIC_STATE_DIR, MOSAIC_LOG_DIR).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from . import APP_NAME, APP_SLUG

IN_DOCKER = Path("/.dockerenv").exists() or os.environ.get("MOSAIC_DOCKER") == "1"
IS_MAC = sys.platform == "darwin" and not IN_DOCKER
HOME = Path.home()


def _env_path(name: str) -> Path | None:
    v = os.environ.get(name, "").strip()
    return Path(v).expanduser() if v else None


PHOTOS_DIR = _env_path("MOSAIC_PHOTOS_DIR") or (Path("/data") if IN_DOCKER else HOME / "Pictures" / APP_NAME)
INPUT_DIR = PHOTOS_DIR / "Input"
OUTPUT_DIR = PHOTOS_DIR / "Output"

if _env_path("MOSAIC_STATE_DIR"):
    STATE_DIR = _env_path("MOSAIC_STATE_DIR")
elif IN_DOCKER:
    STATE_DIR = Path("/state")
elif IS_MAC:
    STATE_DIR = HOME / "Library" / "Application Support" / APP_NAME
else:
    STATE_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share")) / APP_SLUG

LOG_DIR = _env_path("MOSAIC_LOG_DIR") or (HOME / "Library" / "Logs" / APP_NAME if IS_MAC else STATE_DIR / "logs")
MODELS_DIR = STATE_DIR / "models"
HISTORY_DB = STATE_DIR / "history.sqlite3"
ADJ_FILE = STATE_DIR / "crop-adjustments.json"
RECENT_FILE = STATE_DIR / "recent-folders.json"
RECENT_OUT_FILE = STATE_DIR / "recent-destinations.json"

# AI model downloads go to the app data folder, not ~/.cache (easy to find and remove)
os.environ.setdefault("HF_HOME", str(MODELS_DIR / "huggingface"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def ensure() -> None:
    """Create the folders Mosaic Photo-Editor uses. Never fails."""
    for d in (INPUT_DIR, OUTPUT_DIR, STATE_DIR, MODELS_DIR, LOG_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, value) -> None:
    """Atomic write: never leaves a half-written file behind."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value))
        os.replace(tmp, path)
    except OSError:
        pass


def is_inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


def empty_folder(folder: Path, to_trash: bool = True) -> tuple[int, list[str]]:
    """Remove everything inside `folder` (the folder itself stays). Moves items to the Trash
    when possible. Returns (items removed, errors)."""
    removed, errors = 0, []
    if not folder.is_dir():
        return 0, []
    trash = None
    if to_trash and not IN_DOCKER:
        try:
            from send2trash import send2trash as trash
        except ImportError:
            trash = None
    for entry in list(folder.iterdir()):
        if entry.name == ".DS_Store":
            entry.unlink(missing_ok=True)
            continue
        try:
            if trash:
                trash(str(entry))
            elif entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except Exception as e:  # keep going, report at the end
            errors.append(f"{entry.name}: {e}")
    return removed, errors


def count_files(folder: Path, limit: int = 100_000) -> int:
    n = 0
    try:
        for _root, _dirs, files in os.walk(folder):
            n += sum(1 for f in files if f != ".DS_Store")
            if n >= limit:
                break
    except OSError:
        pass
    return n


ensure()
