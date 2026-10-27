"""Media library: campaign footage you downloaded into one folder per campaign.

The worker that holds the files (the laptop) scans its library and reports it to the Pi; the Pi
matches folders to campaigns by name and lets you pick files to queue. Files are clipped in place
(D20); nothing here moves or deletes them."""

import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from clipforge import costs
from clipforge.config import get_settings
from clipforge.ingest import source_key
from clipforge.tools import ffprobe

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".ts", ".flv"}
SETTLE_S = 120  # a file modified this recently may still be downloading or unzipping
RENDER_S_PER_CLIP = 65  # laptop, measured on Man On ep-4 (D6 for the Pi)

_durations: dict[tuple[str, int, float], float | None] = {}  # worker-side probe cache


def _duration(path: Path, size: int, mtime: float) -> float | None:
    k = (str(path), size, mtime)
    if k not in _durations:
        r = subprocess.run([ffprobe(), "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True)
        try:
            _durations[k] = float(r.stdout.strip())
        except ValueError:
            _durations[k] = None  # unreadable or incomplete
    return _durations[k]


def scan(root: Path, probe: bool = True) -> list[dict]:
    """Every video file under root. Runs on the machine that holds the files; no DB."""
    out = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() not in VIDEO_EXT or name.startswith((".", "~")):
                continue
            st = p.stat()
            rel = p.relative_to(root).as_posix()
            settling = time.time() - st.st_mtime < SETTLE_S
            out.append({"path": str(p), "rel": rel, "size": st.st_size, "mtime": st.st_mtime,
                        "duration_s": None if settling or not probe
                        else _duration(p, st.st_size, st.st_mtime)})
    return sorted(out, key=lambda f: f["rel"].lower())


def make_folders(root: Path, names: list[str]) -> list[str]:
    """Create each campaign's footage folder under root (on the machine holding the media).
    Names come from the Pi; anything that isn't a plain folder name is ignored."""
    made = []
    for name in names:
        if not name or name in (".", "..") or any(c in name for c in '/\\:'):
            continue
        d = root / name
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            made.append(name)
    return made


def store(conn: sqlite3.Connection, host: str, files: list[dict]) -> int:
    conn.execute("DELETE FROM library_files WHERE host=?", (host,))
    for f in files:
        folder = f["rel"].split("/")[0] if "/" in f["rel"] else ""
        conn.execute("INSERT INTO library_files (host, path, rel, folder, size, mtime, duration_s)"
                     " VALUES (?,?,?,?,?,?,?)", (host, f["path"], f["rel"], folder, f["size"],
                                                  f["mtime"], f.get("duration_s")))
    return len(files)


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def folders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT folder, host, COUNT(*) n, SUM(size) bytes FROM library_files WHERE folder != ''"
        " GROUP BY host, folder ORDER BY folder").fetchall()


def match_folder(conn: sqlite3.Connection, campaign: sqlite3.Row | dict) -> str | None:
    """The campaign's chosen folder, else the folder whose name best matches the campaign's
    ('ManOn' matches 'Man On – episode-4'; the longest match wins)."""
    if campaign["media_folder"]:
        return campaign["media_folder"]
    name = _norm(campaign["name"])
    best = None
    for r in folders(conn):
        f = _norm(r["folder"])
        if f and (f in name or name in f) and (best is None or len(f) > len(_norm(best))):
            best = r["folder"]
    return best


def files_for(conn: sqlite3.Connection, campaign: sqlite3.Row | dict) -> list[dict]:
    """Library files in the campaign's folder, each marked queued / settling / ready."""
    folder = match_folder(conn, campaign)
    if not folder:
        return []
    rows = conn.execute("SELECT * FROM library_files WHERE folder=? ORDER BY rel",
                        (folder,)).fetchall()
    out = []
    for r in rows:
        f = dict(r)
        f["source_key"] = source_key(f["path"], f["host"])
        f["queued"] = bool(conn.execute("SELECT 1 FROM sources WHERE source_key=?",
                                        (f["source_key"],)).fetchone())
        f["settling"] = time.time() - f["mtime"] < SETTLE_S or f["duration_s"] is None
        out.append(f)
    return out


def estimate(files: list[dict]) -> dict:
    """What queuing these files costs: API dollars, clips to review, laptop render time."""
    s = get_settings()
    hours = sum(f["duration_s"] or 0 for f in files) / 3600
    stt = (costs.stt_cost(s.transcribe_provider, s.transcribe_model, hours * 3600)
           if s.transcribe_provider in ("groq", "openai") else 0.0)
    # ~9K words per hour of speech -> ~12K prompt tokens, plus ~5K out per source.
    sel = (sum(costs.llm_cost(s.select_model, int((f["duration_s"] or 0) / 3600 * 12000) + 3000,
                              5000) for f in files) if s.select_provider == "anthropic" else 0.0)
    clips = len(files) * s.candidates_keep
    return {"files": len(files), "hours": hours, "usd": stt + sel, "clips": clips,
            "render_h": clips * RENDER_S_PER_CLIP / 3600}
