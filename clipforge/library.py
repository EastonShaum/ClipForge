"""Media library: campaign footage you downloaded, one folder per bounty
(Campaigns/<campaign>/<bounty>/).

The worker that holds the files (the laptop) scans its library and reports it to the Pi; the Pi
shows each bounty the files in its folder and lets you pick files to queue. Files are clipped in place
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
# yt-dlp's in-progress pieces: "x.temp.mp4" (fixup/merge), "x.f137.mp4" (one stream before merge)
_PIECE = re.compile(r"\.(temp|f(\d|hls|dash|http)[\w-]*)\.\w+$")

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
        pieces = [n for n in filenames if _PIECE.search(n) or n.endswith((".part", ".ytdl"))]
        for name in filenames:
            p = Path(dirpath) / name
            if (p.suffix.lower() not in VIDEO_EXT or name.startswith((".", "~"))
                    or name in pieces):
                continue
            st = p.stat()
            rel = p.relative_to(root).as_posix()
            # Still downloading, or yt-dlp is still merging/repairing it in a sibling file.
            settling = (time.time() - st.st_mtime < SETTLE_S
                        or any(n.startswith(p.stem + ".") for n in pieces))
            out.append({"path": str(p), "rel": rel, "size": st.st_size, "mtime": st.st_mtime,
                        "duration_s": None if settling or not probe
                        else _duration(p, st.st_size, st.st_mtime)})
    return sorted(out, key=lambda f: f["rel"].lower())


def make_folders(root: Path, names: list[str]) -> list[str]:
    """Create each bounty's footage folder under root (on the machine holding the media), e.g.
    "ManOn/episode-4". Names come from the Pi; anything but plain relative folders is ignored."""
    made = []
    for name in names:
        parts = (name or "").split("/")
        if not name or any(p in ("", ".", "..") or any(c in p for c in '\\:') for p in parts):
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


def _dir(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def folders(conn: sqlite3.Connection) -> list[dict]:
    """Every folder that holds videos, with counts (for picking a different folder)."""
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT rel, size FROM library_files"):
        d = out.setdefault(_dir(r["rel"]), {"folder": _dir(r["rel"]), "n": 0, "bytes": 0})
        d["n"] += 1
        d["bytes"] += r["size"]
    return sorted((d for d in out.values() if d["folder"]), key=lambda d: d["folder"].lower())


def _in(rel: str, folder: str) -> bool:
    """rel is inside folder or any subfolder of it (Windows: case-insensitive)."""
    return rel.lower().startswith(folder.strip("/").lower() + "/")


def files_for(conn: sqlite3.Connection, bounty: sqlite3.Row | dict) -> list[dict]:
    """Library files in the bounty's folder (and subfolders, since unzipped downloads nest),
    each marked queued / settling / ready."""
    folder = bounty["media_folder"]
    if not folder:
        return []
    out = []
    for r in conn.execute("SELECT * FROM library_files ORDER BY rel"):
        if not _in(r["rel"], folder):
            continue
        f = dict(r)
        f["source_key"] = source_key(f["path"], f["host"])
        f["queued"] = bool(conn.execute("SELECT 1 FROM sources WHERE source_key=?",
                                        (f["source_key"],)).fetchone())
        f["settling"] = time.time() - f["mtime"] < SETTLE_S or f["duration_s"] is None
        out.append(f)
    return out


def relink_moved(conn: sqlite3.Connection, host: str) -> int:
    """A clipped file you moved (e.g. into its bounty folder) keeps its source: if a local source
    on this host points at a path that's gone and exactly one library file has the same name,
    the source follows it. Stops re-queuing (and paying for) the same footage twice."""
    moved = 0
    by_name: dict[str, list[sqlite3.Row]] = {}
    for f in conn.execute("SELECT * FROM library_files WHERE host=?", (host,)):
        by_name.setdefault(f["rel"].rsplit("/", 1)[-1].lower(), []).append(f)
    paths = {f["path"] for fs in by_name.values() for f in fs}
    for src in conn.execute("SELECT id, url, media_path FROM sources WHERE media_host=? AND"
                            " url NOT LIKE 'http%'", (host,)).fetchall():
        old = src["media_path"] or src["url"]
        if old in paths:
            continue
        hits = by_name.get(old.replace("\\", "/").rsplit("/", 1)[-1].lower(), [])
        if len(hits) != 1:
            continue
        new, key = hits[0]["path"], source_key(hits[0]["path"], host)
        if conn.execute("SELECT 1 FROM sources WHERE source_key=?", (key,)).fetchone():
            continue  # the new path was queued separately; leave both alone
        conn.execute("UPDATE sources SET url=?, media_path=?, source_key=? WHERE id=?",
                     (new, new, key, src["id"]))
        moved += 1
    return moved


def newly_ready(conn: sqlite3.Connection, host: str) -> str | None:
    """A Discord message for videos that just finished landing in an active bounty's folder
    (e.g. an overnight VOD download), or None. Each file is announced once (settings_kv).
    A bounty with auto_queue on gets them queued for clipping right away."""
    from clipforge.bounties import label
    from clipforge.ingest import add_source

    bounties = conn.execute(
        "SELECT b.id, b.campaign_id, b.name, b.media_folder, b.auto_queue, c.name campaign"
        " FROM bounties b"
        " JOIN campaigns c ON c.id=b.campaign_id WHERE b.status='active' AND c.status='active'"
        " AND b.media_folder IS NOT NULL ORDER BY length(b.media_folder) DESC").fetchall()
    ready: dict[int, list[dict]] = {}
    for b in bounties:
        for f in files_for(conn, b):
            if f["host"] != host or f["queued"] or f["settling"]:
                continue
            key = f"footage-ready:{host}:{f['rel']}:{f['size']}"
            if conn.execute("INSERT INTO settings_kv (key, value) VALUES (?, ?) ON CONFLICT(key)"
                            " DO NOTHING", (key, str(b["id"]))).rowcount:
                ready.setdefault(b["id"], []).append({**f, "bounty": b})
    if not ready:
        return None
    base = get_settings().public_base_url
    lines = []
    for fs in ready.values():
        b = fs[0]["bounty"]
        hours = sum(f["duration_s"] or 0 for f in fs) / 3600
        lines.append(f"🎬 **New footage ready: {label(b['campaign'], b['name'])}**: "
                     f"{', '.join(f['rel'].rsplit('/', 1)[-1] for f in fs)} ({hours:.1f} h)")
        if b["auto_queue"]:
            for f in fs:
                add_source(conn, b["campaign_id"], f["path"], f["host"], bounty_id=b["id"])
            lines.append(f"Auto-queued for clipping. Clips will show up in {base}/review")
        else:
            lines.append(f"Queue it for clipping: {base}/campaigns/{b['campaign_id']}"
                         f"/bounties/{b['id']}/queue")
    return "\n".join(lines)


def estimate(files: list[dict], clips_per: int | None = None) -> dict:
    """What queuing these files costs: API dollars, clips to review, laptop render time."""
    s = get_settings()
    clips_per = clips_per or s.candidates_keep
    hours = sum(f["duration_s"] or 0 for f in files) / 3600
    stt = (costs.stt_cost(s.transcribe_provider, s.transcribe_model, hours * 3600)
           if s.transcribe_provider in ("groq", "openai") else 0.0)
    # ~9K words per hour of speech -> ~12K prompt tokens, plus ~5K out per source.
    sel = (sum(costs.llm_cost(s.select_model, int((f["duration_s"] or 0) / 3600 * 12000) + 3000,
                              5000) for f in files) if s.select_provider == "anthropic" else 0.0)
    clips = len(files) * clips_per
    return {"files": len(files), "hours": hours, "usd": stt + sel, "clips": clips,
            "render_h": clips * RENDER_S_PER_CLIP / 3600}
