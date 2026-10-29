"""Live streams (D30): watch streamers' Kick/Twitch channels, record them on the laptop, and clip
the recording in chunks while the stream is still going.

The Pi checks who is live (scheduler, every live_check_s) and keeps one live_sessions row per
broadcast. The laptop's recorder (clipforge/recorder.py) asks which sessions to record, saves
the stream as 2-minute pieces under live_dir, and lists them in 30-minute .ffconcat chunks that
overlap by one piece, so a moment on a boundary is whole in one of them. Each chunk it reports
becomes a source of the campaign's live bounty and runs through the normal pipeline; the
overlap is skipped when keeping candidates (sources.skip_ms). Watch-only campaigns are recorded
so the footage is ready, but nothing is clipped (or paid for) until a live bounty exists."""

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from clipforge import jobqueue
from clipforge.bounties import hold_reasons, label
from clipforge.campaigns import folder_name
from clipforge.config import get_settings
from clipforge.db import tx, utc_in, utcnow
from clipforge.ingest import add_source

log = logging.getLogger(__name__)

PIECE_S = 120  # recorder piece length; one piece of overlap covers any clip (max_clip_s <= 120)
OFFLINE_GRACE_S = 600  # a stream that drops for less than this is the same session
_CHANNEL = re.compile(r"^(?:https?://)?(?:www\.|m\.)?(kick\.com|twitch\.tv)/([A-Za-z0-9_]{2,40})/?$",
                      re.I)


def parse_channels(text: str) -> tuple[list[str], list[str]]:
    """"https://kick.com/N3on, twitch.tv/n3on" -> (["kick.com/n3on", "twitch.tv/n3on"], [])."""
    good, bad = [], []
    for tok in (text or "").replace(",", " ").split():
        m = _CHANNEL.match(tok)
        if m:
            good.append(f"{m.group(1).lower()}/{m.group(2).lower()}")
        else:
            bad.append(tok)
    return list(dict.fromkeys(good)), bad


def channel_url(channel: str) -> str:
    site, name = channel.split("/")
    return f"https://www.twitch.tv/{name}" if site == "twitch.tv" else f"https://kick.com/{name}"


def guesses(campaign: str) -> list[str]:
    """Channels to try when a live campaign has none: its name on Kick and on Twitch."""
    slug = re.sub(r"[^a-z0-9_]", "", campaign.lower())
    return [f"kick.com/{slug}", f"twitch.tv/{slug}"] if len(slug) >= 2 else []


class ChannelMissing(Exception):
    pass


class _Quiet:
    def debug(self, msg):
        pass

    warning = error = info = debug


def probe(channel: str) -> dict | None:
    """{"id", "title"} while the channel is live, None while it's offline. Uses yt-dlp's Kick
    and Twitch extractors (public pages, no login). Raises ChannelMissing for no such channel."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _Quiet()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(channel_url(channel), download=False, process=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "not currently live" in msg or "is offline" in msg:
            return None
        if "does not exist" in msg or "404" in msg:
            raise ChannelMissing(f"{channel} doesn't exist") from e
        raise
    if not info.get("is_live", True):
        return None
    # Twitch's "title" is "name (live)"; the streamer's own title is in "description".
    title = info.get("description") if channel.startswith("twitch") else info.get("title")
    return {"id": str(info.get("id") or ""), "title": (title or "").strip()[:200]}


def _kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings_kv (key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def channel_status(conn: sqlite3.Connection, channel: str) -> dict | None:
    r = conn.execute("SELECT value FROM settings_kv WHERE key=?",
                     (f"live-status:{channel}",)).fetchone()
    return json.loads(r["value"]) if r else None


def live_bounty(conn: sqlite3.Connection, campaign_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bounties WHERE campaign_id=? AND live_clip=1 AND"
                        " status='active' ORDER BY id DESC LIMIT 1", (campaign_id,)).fetchone()


def watched(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Active campaigns to check: watching is on, or a bounty clips their live streams."""
    return conn.execute(
        """SELECT * FROM campaigns c WHERE c.status='active' AND (c.watch_live=1 OR EXISTS
               (SELECT 1 FROM bounties b WHERE b.campaign_id=c.id AND b.live_clip=1
                AND b.status='active')) ORDER BY c.id""").fetchall()


def _discover(conn: sqlite3.Connection, c: sqlite3.Row, probe_fn) -> str | None:
    """A live campaign with no channels: try its name on Kick and Twitch, once."""
    if not conn.execute("INSERT INTO settings_kv (key, value) VALUES (?, ?) ON CONFLICT(key)"
                        " DO NOTHING", (f"live-discover:{c['id']}", utcnow())).rowcount:
        return None
    found = []
    for ch in guesses(c["name"]):
        try:
            probe_fn(ch)
            found.append(ch)
        except ChannelMissing:
            pass
        except Exception:  # noqa: BLE001 - a guess that can't be checked is skipped
            log.warning("could not check %s", ch, exc_info=True)
    base = get_settings().public_base_url
    if not found:
        return (f"📡 No Kick or Twitch channel named after **{c['name']}**. Add their channel "
                f"so ClipForge can watch it: {base}/campaigns/{c['id']}#live")
    conn.execute("UPDATE campaigns SET live_channels=? WHERE id=?", (" ".join(found), c["id"]))
    return (f"📡 Watching **{c['name']}** on {', '.join(found)} (found by name; check it's the "
            f"right streamer): {base}/campaigns/{c['id']}#live")


def check(conn: sqlite3.Connection, probe_fn: Callable = probe) -> list[str]:
    """One pass: who's live, which sessions ended, chunks to queue. Returns Discord messages."""
    msgs = []
    for c in watched(conn):
        if not c["live_channels"].strip():
            if msg := _discover(conn, c, probe_fn):
                msgs.append(msg)
            c = conn.execute("SELECT * FROM campaigns WHERE id=?", (c["id"],)).fetchone()
        for ch in parse_channels(c["live_channels"])[0]:
            try:
                info = probe_fn(ch)
                state = "live" if info else "offline"
            except ChannelMissing:
                info, state = None, "not found"
            except Exception as e:  # noqa: BLE001 - one bad check mustn't stop the rest
                log.warning("live check of %s failed: %s", ch, e)
                info, state = None, f"check failed: {str(e)[:150]}"
            _kv(conn, f"live-status:{ch}", json.dumps({"state": state, "at": utcnow()}))
            if info:
                if msg := _seen_live(conn, c, ch, info):
                    msgs.append(msg)
                break  # multistreaming to Kick and Twitch: record one of them
    _end_quiet(conn)
    attach(conn)
    queue_chunks(conn)
    return msgs


def _seen_live(conn: sqlite3.Connection, c: sqlite3.Row, channel: str, info: dict) -> str | None:
    now = utcnow()
    key = f"{channel}:{info['id']}"
    cur = conn.execute("UPDATE live_sessions SET last_live_at=?, state='live', ended_at=NULL,"
                       " title=? WHERE stream_key=?", (now, info["title"], key))
    if cur.rowcount:
        return None
    # A stream that dropped and restarted gets a new id: same session, same recording.
    cur = conn.execute("UPDATE live_sessions SET stream_key=?, last_live_at=?, title=?"
                       " WHERE campaign_id=? AND channel=? AND state='live'",
                       (key, now, info["title"], c["id"], channel))
    if cur.rowcount:
        return None
    if conn.execute("SELECT 1 FROM live_sessions WHERE campaign_id=? AND state='live'",
                    (c["id"],)).fetchone():
        return None  # already recording this streamer on another channel
    s = get_settings()
    local = datetime.now(ZoneInfo(s.timezone))
    slug = channel.replace(".com", "").replace(".tv", "").replace("/", "-")
    b = live_bounty(conn, c["id"])
    conn.execute(
        """INSERT INTO live_sessions (campaign_id, bounty_id, channel, stream_key, title, dir,
               name, started_at, last_live_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (c["id"], b["id"] if b else None, channel, key, info["title"],
         f"{folder_name(c['name'])}/{slug} {local:%Y-%m-%d %H%M}",
         f"{folder_name(c['name'])} live {local:%m-%d}", now, now))
    return start_message(conn, c, b, channel, info["title"])


def start_message(conn: sqlite3.Connection, c: sqlite3.Row, b: sqlite3.Row | None,
                  channel: str, title: str) -> str:
    s = get_settings()
    base = s.public_base_url
    lines = [f"🔴 **{c['name']} is live** on {channel_url(channel)}"
             + (f": “{title}”" if title else "")]
    if jobqueue.remote_worker_alive(conn, s.host_name, s.laptop_grace_s):
        lines.append("Recording on the laptop.")
    else:
        lines.append("⚠️ The laptop worker is offline, so nothing is recording. Start it now.")
    if b:
        lines.append(f"Clipping into **{label(c['name'], b['name'])}** every "
                     f"{s.live_chunk_min} min.")
        missing = hold_reasons(conn, b)
        if missing:
            lines.append(f"Clips wait to render until you add: {', '.join(missing)} → "
                         f"{base}/campaigns/{c['id']}/bounties/{b['id']}#rules")
    else:
        lines.append(f"No live bounty yet, so it's only recorded (ready if a bounty opens). "
                     f"Add one from Scout with “Add & clip live”: {base}/scout")
    return "\n".join(lines)


def _end_quiet(conn: sqlite3.Connection) -> None:
    """Sessions not seen live for OFFLINE_GRACE_S have ended. One nobody recorded has nothing
    left to finish."""
    now = utcnow()
    conn.execute("UPDATE live_sessions SET state='ended', ended_at=last_live_at,"
                 " finalized_at=CASE WHEN host IS NULL THEN ? END"
                 " WHERE state='live' AND last_live_at < ?", (now, utc_in(-OFFLINE_GRACE_S)))


def attach(conn: sqlite3.Connection) -> None:
    """Recordings made before the live bounty existed (watch-only) join it once it does, if
    they're from the last day: the footage was ready, so clipping starts at once."""
    conn.execute(
        """UPDATE live_sessions SET bounty_id=(SELECT b.id FROM bounties b
               WHERE b.campaign_id=live_sessions.campaign_id AND b.live_clip=1
               AND b.status='active' ORDER BY b.id DESC LIMIT 1)
           WHERE bounty_id IS NULL AND purged_at IS NULL AND (state='live' OR ended_at > ?)""",
        (utc_in(-86400),))


def queue_chunks(conn: sqlite3.Connection, session_id: int | None = None) -> int:
    """Queue reported chunks of sessions that have an active live bounty. Idempotent."""
    s = get_settings()
    rows = conn.execute(
        """SELECT k.id, k.path, k.overlap_ms, ls.campaign_id, ls.bounty_id, ls.host, ls.title
           FROM live_chunks k JOIN live_sessions ls ON ls.id=k.session_id
           JOIN bounties b ON b.id=ls.bounty_id JOIN campaigns c ON c.id=ls.campaign_id
           WHERE k.source_id IS NULL AND b.status='active' AND b.live_clip=1
             AND c.status='active' AND (? IS NULL OR ls.id=?) ORDER BY k.id""",
        (session_id, session_id)).fetchall()
    for r in rows:
        with tx(conn):
            sid, _ = add_source(conn, r["campaign_id"], r["path"], r["host"],
                                bounty_id=r["bounty_id"], clips=s.live_clips_per_chunk)
            # The stream title ("N3on x Ryan Garcia Halloween Party") gives the selector the
            # context a chunk's file name can't.
            conn.execute("UPDATE sources SET skip_ms=?, title=? WHERE id=?",
                         (r["overlap_ms"], r["title"] or "", sid))
            conn.execute("UPDATE live_chunks SET source_id=? WHERE id=?", (sid, r["id"]))
    return len(rows)


def for_recorder(conn: sqlite3.Connection, host: str) -> dict:
    """What the laptop's recorder should do: record live sessions (claiming unclaimed ones),
    finish ended ones it recorded, delete old recordings nothing needs any more."""
    s = get_settings()
    with tx(conn):
        conn.execute("UPDATE live_sessions SET host=? WHERE host IS NULL AND state='live'",
                     (host,))
        rows = conn.execute(
            "SELECT id, channel, dir, name, state FROM live_sessions WHERE host=? AND"
            " (state='live' OR finalized_at IS NULL) ORDER BY id", (host,)).fetchall()
        old = conn.execute(
            """SELECT id, dir FROM live_sessions ls WHERE host=? AND purged_at IS NULL
                 AND finalized_at IS NOT NULL AND ended_at < ?
                 AND NOT EXISTS (SELECT 1 FROM live_chunks k JOIN sources src
                                 ON src.id=k.source_id WHERE k.session_id=ls.id
                                 AND src.status NOT IN ('rendered','failed'))""",
            (host, utc_in(-s.live_keep_hours * 3600))).fetchall()
        for r in old:
            conn.execute("UPDATE live_sessions SET purged_at=? WHERE id=?", (utcnow(), r["id"]))
    return {"sessions": [{"id": r["id"], "url": channel_url(r["channel"]), "dir": r["dir"],
                          "name": r["name"], "live": r["state"] == "live"} for r in rows],
            "purge": [r["dir"] for r in old], "piece_s": PIECE_S,
            "chunk_s": s.live_chunk_min * 60, "overlap_s": PIECE_S,
            "max_height": s.max_download_height}


def report(conn: sqlite3.Connection, session_id: int, body: dict) -> str | None:
    """Store a recorder report and queue its new chunks. Returns a Discord message when the
    session's recording is finished."""
    sess = conn.execute("SELECT * FROM live_sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        raise ValueError(f"no live session {session_id}")
    final = bool(body.get("final")) and sess["state"] == "ended"
    with tx(conn):
        conn.execute(
            """UPDATE live_sessions SET recording=?, recorded_s=?, bytes=?, rec_error=?,
                   reported_at=?, finalized_at=CASE WHEN ? THEN COALESCE(finalized_at, ?)
                   ELSE finalized_at END WHERE id=?""",
            (int(bool(body.get("recording"))), float(body.get("recorded_s") or 0),
             int(body.get("bytes") or 0), (body.get("error") or None), utcnow(), final,
             utcnow(), session_id))
        for ch in body.get("chunks", []):
            conn.execute("INSERT INTO live_chunks (session_id, path, overlap_ms) VALUES (?,?,?)"
                         " ON CONFLICT(session_id, path) DO NOTHING",
                         (session_id, ch["path"], int(ch.get("overlap_ms") or 0)))
    queue_chunks(conn, session_id)
    if not final or sess["finalized_at"]:
        return None
    n = conn.execute("SELECT COUNT(*) n, COUNT(source_id) q FROM live_chunks WHERE session_id=?",
                     (session_id,)).fetchone()
    c = conn.execute("SELECT name FROM campaigns WHERE id=?", (sess["campaign_id"],)).fetchone()
    hours = float(body.get("recorded_s") or 0) / 3600
    what = (f"{n['q']} chunk(s) sent for clipping" if n["q"]
            else f"{n['n']} chunk(s) recorded, not clipped (no live bounty)")
    return f"⚫ **{c['name']}**'s stream ended: {hours:.1f} h recorded, {what}."


def sessions_for(conn: sqlite3.Connection, campaign_id: int, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        """SELECT ls.*, (SELECT COUNT(*) FROM live_chunks k WHERE k.session_id=ls.id) chunks,
                  (SELECT COUNT(source_id) FROM live_chunks k WHERE k.session_id=ls.id) queued
           FROM live_sessions ls WHERE campaign_id=? ORDER BY id DESC LIMIT ?""",
        (campaign_id, limit)).fetchall()
    return [dict(r) for r in rows]
