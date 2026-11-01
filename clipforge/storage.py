"""The Files page (D37): every file ClipForge made, how big it is, when it's deleted on its own,
and a way to delete it now. Easton's own footage (the Campaigns folders) is never touched."""

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import PureWindowsPath

from clipforge.config import Settings
from clipforge.db import utcnow
from clipforge.live import WAITING_CLIPS, WAITING_JOBS
from clipforge.stages.fetch import cleanup_raw

CLIP_GROUPS = {"rejected": "Rejected", "posted": "Posted"}  # whose videos can be deleted


def _count(exists_sql: str) -> str:
    return exists_sql.replace("SELECT 1", "SELECT COUNT(*)", 1)


def _after(iso: str, hours: int) -> str:
    t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) + timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def recordings(conn: sqlite3.Connection, s: Settings) -> list[dict]:
    """Stream recordings on the laptop not deleted yet, newest first, each with `when` (the
    UTC time it's deleted, if known) and `note` (why it's kept, or what happens next)."""
    rows = conn.execute(
        f"""SELECT ls.*, c.name streamer,
                   (SELECT k.path FROM live_chunks k WHERE k.session_id=ls.id LIMIT 1) chunk,
                   ({_count(WAITING_JOBS)}) jobs, ({_count(WAITING_CLIPS)}) held
            FROM live_sessions ls JOIN campaigns c ON c.id=ls.campaign_id
            WHERE ls.purged_at IS NULL ORDER BY ls.id DESC""").fetchall()
    keep = f"{s.live_keep_hours // 24} days" if s.live_keep_hours % 24 == 0 \
        else f"{s.live_keep_hours} h"
    now = utcnow()
    out = []
    for r in rows:
        d = dict(r)
        # Shown where it really is: the laptop folder a chunk sits in.
        d["path"] = str(PureWindowsPath(r["chunk"]).parent) if r["chunk"] and "\\" in r["chunk"] \
            else f"{s.live_dir.name}/{r['dir']}"
        d["when"], d["can_delete"] = None, r["state"] == "ended" or bool(r["stopped_at"])
        if r["delete_asked_at"]:
            d["note"], d["can_delete"] = "Deleting the next time the laptop checks in.", False
        elif r["state"] == "live":
            d["note"] = (f"{'Recording off' if r['stopped_at'] else 'Recording now'}."
                         f" Deleted {keep} after the stream ends.")
        elif not r["finalized_at"]:
            d["note"] = "The laptop is finishing the last chunk."
        elif r["jobs"]:
            d["note"] = f"Kept until {r['jobs']} chunk job(s) finish, then {keep} after the end."
        elif r["held"]:
            d["note"] = (f"Kept while {r['held']} picked clip(s) wait to render (the bounty"
                         " holds renders until its rules are in).")
        else:
            d["when"] = _after(r["ended_at"], s.live_keep_hours)
            d["note"] = ("Due now: deleted the next time the laptop checks in."
                         if d["when"] <= now else "")
        out.append(d)
    return out


def downloads(conn: sqlite3.Connection, s: Settings) -> list[dict]:
    """Videos ClipForge downloaded from a link to this machine (data/raw), not deleted yet."""
    rows = conn.execute(
        """SELECT s.id, s.title, s.status, s.updated_at, s.url, s.bounty_id, b.campaign_id cid
           FROM sources s LEFT JOIN bounties b ON b.id=s.bounty_id
           WHERE s.media_host=? AND s.raw_deleted_at IS NULL ORDER BY s.id DESC""",
        (s.host_name,)).fetchall()
    out = []
    for r in rows:
        d = s.raw_dir / str(r["id"])
        if not d.is_dir():
            continue  # clipped in place from a folder, or never downloaded
        done = r["status"] in ("rendered", "failed")
        out.append({**dict(r), "path": str(d),
                    "bytes": sum(f.stat().st_size for f in d.rglob("*") if f.is_file()),
                    "when": _after(r["updated_at"], s.raw_retention_hours) if done else None,
                    "note": "" if done else
                    f"Deleted {s.raw_retention_hours} h after its clips are made.",
                    "can_delete": done})
    return out


def clip_files(conn: sqlite3.Connection, s: Settings) -> list[dict]:
    """Rendered clip videos on this machine, per bounty: count and size at each stage."""
    rows = conn.execute(
        """SELECT b.id bid, b.campaign_id cid, b.name, b.live_session_id, cp.name campaign,
                  k.status, COUNT(*) n, COALESCE(SUM(k.size_bytes), 0) bytes
           FROM clips k JOIN candidates c ON c.id=k.candidate_id JOIN sources s ON s.id=c.source_id
           JOIN bounties b ON b.id=s.bounty_id JOIN campaigns cp ON cp.id=b.campaign_id
           WHERE k.file_deleted_at IS NULL GROUP BY b.id, k.status ORDER BY b.id DESC""").fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        g = out.setdefault(r["bid"], {
            "bid": r["bid"], "cid": r["cid"], "campaign": r["campaign"], "name": r["name"],
            "live_session_id": r["live_session_id"], "path": str(s.clips_dir), "stages": {},
            "bytes": 0})
        g["stages"][r["status"]] = {"n": r["n"], "bytes": r["bytes"]}
        g["bytes"] += r["bytes"]
    return list(out.values())


def delete_download(conn: sqlite3.Connection, s: Settings, source_id: int) -> int:
    """Delete one downloaded video whose clips are made. Returns bytes freed."""
    r = conn.execute("SELECT id FROM sources WHERE id=? AND media_host=? AND raw_deleted_at IS"
                     " NULL AND status IN ('rendered','failed')",
                     (source_id, s.host_name)).fetchone()
    if not r:
        return 0
    freed = cleanup_raw(s, [source_id])
    conn.execute("UPDATE sources SET raw_deleted_at=? WHERE id=?", (utcnow(), source_id))
    return freed


def delete_clip_files(conn: sqlite3.Connection, s: Settings, bounty_id: int,
                      status: str) -> tuple[int, int]:
    """Delete the videos of one bounty's rejected or posted clips. The clips, their reviews,
    posts and views stay. Returns (videos deleted, bytes freed)."""
    if status not in CLIP_GROUPS:
        raise ValueError(status)
    rows = conn.execute(
        """SELECT k.id, k.path FROM clips k JOIN candidates c ON c.id=k.candidate_id
           JOIN sources s ON s.id=c.source_id WHERE s.bounty_id=? AND k.status=?
           AND k.file_deleted_at IS NULL""", (bounty_id, status)).fetchall()
    freed = 0
    for r in rows:
        f = s.clips_dir / r["path"]
        if f.is_file():
            freed += f.stat().st_size
            f.unlink()
        conn.execute("UPDATE clips SET file_deleted_at=? WHERE id=?", (utcnow(), r["id"]))
    return len(rows), freed


def disk(s: Settings) -> dict:
    u = shutil.disk_usage(s.data_dir)
    return {"free": u.free, "total": u.total}
