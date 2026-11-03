"""YouTube channels as footage (D41): a bounty whose brief allows "any video from this channel"
lists the channel's uploads (newest or most viewed, Shorts hidden), Easton ticks the ones to clip,
and a watched channel's new uploads are queued (auto-queue) or announced on Discord.

Only public listings: yt-dlp's flat channel tab (title, length, views, and a date worked out
from "3 days ago"; nothing is downloaded). YouTube's RSS feeds return 404 since at least
2026-11-02, so they aren't used."""

import contextlib
import re
import sqlite3
from datetime import UTC, datetime

from clipforge.config import get_settings
from clipforge.db import utcnow

# youtube.com/@handle, /channel/UC…, /c/name, /user/name, with an optional tab after it.
CHANNEL = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/(@[\w.\-]+|channel/UC[\w-]{22}|c/[\w.\-]+|user/[\w.\-]+)"
    r"(?:/(?:videos|streams|featured|shorts|live|playlists)?)?/?(?:\?.*)?$", re.I)
TABS = {"videos": "Uploads", "streams": "Past live streams"}
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def channel_base(url: str) -> str | None:
    """The channel's base URL if this is a channel link (any tab), else None."""
    m = CHANNEL.match(url.strip())
    return f"https://www.youtube.com/{m.group(1)}" if m else None


def video_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


# ---------- listing (network; no DB) ----------

class NoSuchTab(ValueError):
    pass


def _flat(url: str, limit: int) -> dict:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist", "playlistend": limit,
            "extractor_args": {"youtubetab": {"approximate_date": [""]}}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            return ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            if "does not have a" in str(e):
                raise NoSuchTab("This channel has no past live streams") from e
            raise


def _entry(e: dict, order: int) -> dict | None:
    """A listed video, or None for a live/upcoming one or a bad entry."""
    vid = e.get("id") or ""
    if not VIDEO_ID.match(vid) or e.get("live_status") in ("is_live", "is_upcoming"):
        return None
    ts = e.get("timestamp")
    return {"id": vid, "url": video_url(vid), "title": e.get("title") or vid,
            "duration_s": float(e.get("duration") or 0), "views": int(e.get("view_count") or 0),
            "published": (datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d") if ts else ""),
            "order": order}


def listing(base: str, tab: str = "videos", limit: int | None = None) -> dict:
    """{channel_id, name, videos: [{id, url, title, duration_s, views, published, order}]},
    newest first. published is an approximate date (YYYY-MM-DD) or ""."""
    info = _flat(f"{base}/{tab if tab in TABS else 'videos'}",
                 limit or get_settings().channel_list_max)
    videos = [v for i, e in enumerate(info.get("entries") or []) if (v := _entry(e, i))]
    name = (info.get("channel") or info.get("uploader") or info.get("title") or "")
    return {"channel_id": info.get("channel_id") or info.get("id") or "",
            "name": name.removesuffix(" - Videos").strip(), "videos": videos}


# ---------- per-bounty channels (DB) ----------

def add(conn: sqlite3.Connection, bounty_id: int, url: str) -> int:
    """Attach a channel to a bounty (looks it up once for its id and name). Returns its row id."""
    base = channel_base(url)
    if not base:
        raise ValueError("That isn't a YouTube channel link (e.g. youtube.com/@handle)")
    info = listing(base, limit=1)
    if not info["channel_id"]:
        raise ValueError("Couldn't find that channel on YouTube")
    conn.execute("INSERT INTO bounty_channels (bounty_id, url, channel_id, name) VALUES"
                 " (?,?,?,?) ON CONFLICT(bounty_id, channel_id) DO NOTHING",
                 (bounty_id, base, info["channel_id"], info["name"]))
    return conn.execute("SELECT id FROM bounty_channels WHERE bounty_id=? AND channel_id=?",
                        (bounty_id, info["channel_id"])).fetchone()["id"]


def for_bounty(conn: sqlite3.Connection, bounty_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM bounty_channels WHERE bounty_id=? ORDER BY id", (bounty_id,))]


def set_watch(conn: sqlite3.Connection, channel_row: int, on: bool) -> None:
    """Watching starts now: what the channel already has is marked seen, so only later
    uploads count as new (no backlog flood). If YouTube can't be reached, the first poll
    does the marking instead."""
    conn.execute("UPDATE bounty_channels SET watch=?, watch_since=CASE WHEN ? THEN ? ELSE"
                 " watch_since END WHERE id=?", (int(on), int(on), utcnow(), channel_row))
    if on:
        ch = conn.execute("SELECT * FROM bounty_channels WHERE id=?", (channel_row,)).fetchone()
        with contextlib.suppress(Exception):  # poll() seeds it later
            _seen_now(conn, ch, _latest(ch["url"]))


def _latest(base: str) -> list[dict]:
    """The newest uploads and past streams, raw entries (lengths may be missing)."""
    out = []
    for tab, n in (("videos", 20), ("streams", 10)):
        try:
            out += _flat(f"{base}/{tab}", n).get("entries") or []
        except NoSuchTab:
            continue
    return out


def _seen_now(conn: sqlite3.Connection, ch, entries: list[dict]) -> None:
    for e in entries:
        conn.execute("INSERT INTO settings_kv (key, value) VALUES (?, ?) ON CONFLICT(key)"
                     " DO NOTHING", (f"channel-seen:{ch['id']}:{e.get('id')}", utcnow()))
    conn.execute("INSERT INTO settings_kv (key, value) VALUES (?, ?) ON CONFLICT(key)"
                 " DO NOTHING", (f"channel-seeded:{ch['id']}", utcnow()))


def known(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict]:
    """Which of these videos ClipForge already has: {id: {bounty, status}}."""
    if not ids:
        return {}
    keys = [f"youtube:{i}" for i in ids]
    rows = conn.execute(
        f"SELECT s.source_key, s.status, b.name bounty FROM sources s LEFT JOIN bounties b"
        f" ON b.id=s.bounty_id WHERE s.source_key IN ({','.join('?' * len(keys))})", keys)
    return {r["source_key"].split(":", 1)[1]: dict(r) for r in rows}


def poll(conn: sqlite3.Connection) -> list[str]:
    """Watched channels: queue (auto-queue on) or announce each new long upload once.
    Returns Discord messages. A video still live or not processed yet waits for the next poll;
    videos under channel_min_video_s are skipped for good. The uploads tab has no Shorts."""
    from clipforge.bounties import label
    from clipforge.ingest import add_source

    s = get_settings()
    rows = conn.execute(
        "SELECT ch.*, b.campaign_id, b.name bounty, b.auto_queue, c.name campaign"
        " FROM bounty_channels ch JOIN bounties b ON b.id=ch.bounty_id JOIN campaigns c"
        " ON c.id=b.campaign_id WHERE ch.watch=1 AND b.status='active'"
        " AND c.status='active'").fetchall()
    msgs = []
    for ch in rows:
        try:
            entries = _latest(ch["url"])
        except Exception:  # noqa: BLE001 - a network blip: try next poll
            continue
        if not conn.execute("SELECT 1 FROM settings_kv WHERE key=?",
                            (f"channel-seeded:{ch['id']}",)).fetchone():
            _seen_now(conn, ch, entries)
            continue
        found = []
        for e in entries:
            key = f"channel-seen:{ch['id']}:{e.get('id')}"
            if not VIDEO_ID.match(e.get("id") or "") or not e.get("duration") \
                    or e.get("live_status") in ("is_live", "is_upcoming") \
                    or conn.execute("SELECT 1 FROM settings_kv WHERE key=?", (key,)).fetchone():
                continue
            conn.execute("INSERT INTO settings_kv (key, value) VALUES (?, ?)", (key, utcnow()))
            if e["duration"] < s.channel_min_video_s:
                continue
            if ch["auto_queue"]:
                add_source(conn, ch["campaign_id"], video_url(e["id"]), bounty_id=ch["bounty_id"])
            found.append(f"{e.get('title') or e['id']} ({e['duration'] / 60:.0f} min)")
        if found:
            where = (f"{s.public_base_url}/campaigns/{ch['campaign_id']}/bounties/"
                     f"{ch['bounty_id']}")
            msgs.append(
                f"📺 **New on {ch['name']}** for {label(ch['campaign'], ch['bounty'])}: "
                + "; ".join(found) + "\n"
                + (f"Auto-queued for clipping. Clips will show up in {where}/review"
                   if ch["auto_queue"] else f"Pick what to clip: {where}/channels/{ch['id']}"))
    return msgs
