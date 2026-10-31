"""Processing history (SQLite, in the app data folder).

Every file Mosaic Photo-Editor hands over (download, save to folder, batch) is recorded with
its source file name, a fingerprint of the source, and the output's format, shape and size.
That lets the app warn when a photo was already processed with the same output spec recently.

Nothing leaves your computer. The history holds file names and numbers, never image data.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .paths import HISTORY_DB

RECENT_DAYS = 7          # "processed before" warning window
KEEP_DAYS = 365          # older records are pruned
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    id             INTEGER PRIMARY KEY,
    created_at     TEXT NOT NULL,            -- UTC, ISO 8601
    session        TEXT,                     -- app session that recorded it
    action         TEXT NOT NULL,            -- download | save | batch | zip
    source_name    TEXT NOT NULL,
    source_fp      TEXT NOT NULL,            -- fingerprint of the source file
    source_bytes   INTEGER,
    source_width   INTEGER,
    source_height  INTEGER,
    spec           TEXT NOT NULL,            -- format|shape|target size, e.g. webp|profile|800x1200
    output_name    TEXT,
    output_format  TEXT,
    output_width   INTEGER,
    output_height  INTEGER,
    output_bytes   INTEGER,
    quality        INTEGER,
    crop           TEXT,
    output_path    TEXT
);
CREATE INDEX IF NOT EXISTS processed_lookup ON processed (source_fp, spec, created_at);
"""


@contextmanager
def _db(path: Path = HISTORY_DB):
    with _lock:
        con = sqlite3.connect(path, timeout=5)
        try:
            con.executescript(SCHEMA)
            yield con
            con.commit()
        finally:
            con.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record(rows: list[dict], path: Path = HISTORY_DB) -> None:
    """Add records. Each row: action, source_name, source_fp, spec and optional output fields."""
    if not rows:
        return
    cols = ("created_at", "session", "action", "source_name", "source_fp", "source_bytes", "source_width",
            "source_height", "spec", "output_name", "output_format", "output_width", "output_height",
            "output_bytes", "quality", "crop", "output_path")
    ts = _now().isoformat(timespec="seconds")
    values = [tuple(r.get(c, ts if c == "created_at" else None) for c in cols) for r in rows]
    try:
        with _db(path) as con:
            con.executemany(f"INSERT INTO processed ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            values)
    except sqlite3.Error:
        pass                                    # history is a convenience: never block the work


def recent(fingerprints: list[str], spec: str, exclude_session: str | None = None,
           days: int = RECENT_DAYS, path: Path = HISTORY_DB) -> dict[str, dict]:
    """Latest record per fingerprint with the same spec in the last `days` days (other sessions)."""
    if not fingerprints:
        return {}
    since = (_now() - timedelta(days=days)).isoformat(timespec="seconds")
    out: dict[str, dict] = {}
    try:
        with _db(path) as con:
            con.row_factory = sqlite3.Row
            for i in range(0, len(fingerprints), 500):
                part = fingerprints[i:i + 500]
                q = (f"SELECT * FROM processed WHERE source_fp IN ({', '.join('?' * len(part))}) "
                     "AND spec = ? AND created_at >= ? AND (session IS NULL OR session != ?) "
                     "ORDER BY created_at")
                for row in con.execute(q, (*part, spec, since, exclude_session or "")):
                    out[row["source_fp"]] = dict(row)
    except sqlite3.Error:
        return {}
    return out


def describe(row: dict) -> str:
    """Tooltip text for a 'processed before' warning."""
    try:
        when = datetime.fromisoformat(row["created_at"]).astimezone()
        when_s = when.strftime("%-d %b %Y, %H:%M")
    except Exception:
        when_s = row.get("created_at", "")
    verb = {"download": "downloaded", "save": "saved", "batch": "converted in a batch",
            "zip": "downloaded in a zip"}.get(row.get("action"), "processed")
    return (f"This image was previously processed: {verb} on {when_s} as {row.get('output_name') or 'a file'} "
            f"with the same format and size. You can still download it.")


def stats(path: Path = HISTORY_DB) -> tuple[int, str | None]:
    try:
        with _db(path) as con:
            n, first = con.execute("SELECT COUNT(*), MIN(created_at) FROM processed").fetchone()
            return n, first
    except sqlite3.Error:
        return 0, None


def prune(days: int = KEEP_DAYS, path: Path = HISTORY_DB) -> None:
    try:
        with _db(path) as con:
            con.execute("DELETE FROM processed WHERE created_at < ?",
                        ((_now() - timedelta(days=days)).isoformat(timespec="seconds"),))
    except sqlite3.Error:
        pass


def clear(path: Path = HISTORY_DB) -> None:
    try:
        with _db(path) as con:
            con.execute("DELETE FROM processed")
        with sqlite3.connect(path) as con:
            con.execute("VACUUM")
    except sqlite3.Error:
        pass
