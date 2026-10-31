"""Timing log: how long the slow steps take, so the native app and Docker can be compared.

Each line of timings.csv is one measured step:
    when, version, runtime (native / docker), ai_device (mps / cpu / -), step, photo, megapixels, seconds, detail

Steps:
    app start     launcher: icon clicked to interface ready (native window only)
    ai warm-up    loading both AI models in the background
    convert       one photo in a batch, start to finished file
    edit          one photo on the Upload page, made again after a change (results reused from the
                  cache are not made again, so they are not logged)
    cut-out       one run of the background model (BiRefNet)
    extend        one run of the AI extender (LaMa)
    batch         a whole batch run (detail: photos, workers, seconds per photo)

The file lives in the logs folder (macOS: ~/Library/Logs/Mosaic Photo-Editor; Docker: /state/logs)
and can be downloaded from the sidebar (Timing log). Set MOSAIC_TIMING_LOG=0 to turn it off.
Nothing about the photo itself is recorded beyond its file name and size.
"""
from __future__ import annotations

import csv
import io
import os
import statistics
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import __version__
from .paths import IN_DOCKER, LOG_DIR

LOG_FILE = LOG_DIR / "timings.csv"
FIELDS = ["when", "version", "runtime", "ai_device", "step", "photo", "megapixels", "seconds", "detail"]
MAX_BYTES = 5 * 1024 * 1024                     # then the file is renamed timings.old.csv and started again
ENABLED = os.environ.get("MOSAIC_TIMING_LOG", "1") != "0"
RUNTIME = "docker" if IN_DOCKER else "native"
_lock = threading.Lock()


def _ai_device() -> str:
    ai = sys.modules.get("mosaic.ai")
    return (getattr(ai, "_device", None) or "-") if ai else "-"


def log(step: str, seconds: float, photo: str = "", megapixels: float | None = None, detail: str = "") -> None:
    """Add one line. Never raises: timing must not break the app."""
    if not ENABLED:
        return
    row = [datetime.now().isoformat(timespec="seconds"), __version__, RUNTIME, _ai_device(), step,
           photo, f"{megapixels:.1f}" if megapixels else "", f"{seconds:.3f}", detail]
    try:
        with _lock:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_BYTES:
                os.replace(LOG_FILE, LOG_FILE.with_name("timings.old.csv"))
            new = not LOG_FILE.exists()
            with open(LOG_FILE, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(FIELDS)
                w.writerow(row)
    except Exception:
        pass


@contextmanager
def timed(step: str, photo: str = "", megapixels: float | None = None, detail: str = ""):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        log(step, time.perf_counter() - t0, photo, megapixels, detail)


def read(days: int = 30) -> list[dict]:
    try:
        with open(LOG_FILE, newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    return [r for r in rows if r.get("when", "") >= since]


def summary(days: int = 30) -> list[dict]:
    """Median and average seconds per step, runtime and AI setting, for the last `days` days."""
    groups: dict[tuple, list[float]] = {}
    for r in read(days):
        try:
            sec = float(r["seconds"])
        except (KeyError, ValueError):
            continue
        ai = "AI on" if "ai=" in r.get("detail", "") and "ai=off" not in r["detail"] else ""
        groups.setdefault((r["step"], r["runtime"], ai), []).append(sec)
    out = []
    for (step, runtime, ai), secs in sorted(groups.items()):
        out.append({"step": step + (f" ({ai})" if ai else ""), "runtime": runtime, "times": len(secs),
                    "median s": round(statistics.median(secs), 2), "average s": round(statistics.fmean(secs), 2)})
    return out


def csv_bytes() -> bytes:
    try:
        return LOG_FILE.read_bytes()
    except OSError:
        buf = io.StringIO()
        csv.writer(buf).writerow(FIELDS)
        return buf.getvalue().encode()


def clear() -> None:
    try:
        LOG_FILE.unlink(missing_ok=True)
    except OSError:
        pass
