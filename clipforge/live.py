"""Live streams (D30): watch streamers' Kick/Twitch channels, record them on the laptop, and clip
the recording in chunks while the stream is still going.

The Pi checks who is live (scheduler, every live_check_s) and keeps one live_sessions row per
broadcast. The laptop's recorder (clipforge/recorder.py) asks which sessions to record, saves
the stream as 30-second pieces under live_dir, and lists them in 5-minute .ffconcat chunks that
overlap by a minute (D33), so a moment on a boundary is whole in one of them. Each chunk it reports
becomes a source and runs through the normal pipeline; the overlap is skipped when keeping
candidates (sources.skip_ms).

Each stream is its own thing (D34): it gets its own bounty row (bounties.live_session_id, shown on
the Lives tab, not under the campaign), so today's clips never land with yesterday's. When a
paid bounty comes up, Easton moves the stream into it (move_to). A stream whose title names a
gambling site or game is recorded but never clipped (blocked)."""

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from clipforge import jobqueue
from clipforge.campaigns import blocked_text, folder_name
from clipforge.config import get_settings
from clipforge.db import tx, utc_in, utcnow
from clipforge.ingest import add_source

log = logging.getLogger(__name__)

PIECE_S = 30  # recorder piece length: a finished piece is what a chunk can include
OVERLAP_S = 60  # repeated between chunks, so a moment on a boundary (live clips <= 25 s) is whole
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


def watched(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Active campaigns to check: watching is on, or a bounty clips their live streams. Not a
    streamer nobody pays for (payer_type 'none', D38): recording them only costs disk."""
    return conn.execute(
        """SELECT * FROM campaigns c WHERE c.status='active'
             AND COALESCE(c.payer_type, '') != 'none' AND (c.watch_live=1 OR EXISTS
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
    queue_chunks(conn)
    return msgs


def _seen_live(conn: sqlite3.Connection, c: sqlite3.Row, channel: str, info: dict) -> str | None:
    now = utcnow()
    key = f"{channel}:{info['id']}"
    # Blocked sticks: a stream that names a casino once stays unclipped even if retitled.
    blocked = blocked_text(info["title"])
    cur = conn.execute("UPDATE live_sessions SET last_live_at=?, state='live', ended_at=NULL,"
                       " title=?, blocked=COALESCE(blocked, ?) WHERE stream_key=?",
                       (now, info["title"], blocked, key))
    if cur.rowcount:
        return None
    # A stream that dropped and restarted gets a new id: same session, same recording.
    cur = conn.execute("UPDATE live_sessions SET stream_key=?, last_live_at=?, title=?,"
                       " blocked=COALESCE(blocked, ?) WHERE campaign_id=? AND channel=?"
                       " AND state='live'", (key, now, info["title"], blocked, c["id"], channel))
    if cur.rowcount:
        return None
    if conn.execute("SELECT 1 FROM live_sessions WHERE campaign_id=? AND state='live'",
                    (c["id"],)).fetchone():
        return None  # already recording this streamer on another channel
    s = get_settings()
    local = datetime.now(ZoneInfo(s.timezone))
    slug = channel.replace(".com", "").replace(".tv", "").replace("/", "-")
    sid = conn.execute(
        """INSERT INTO live_sessions (campaign_id, channel, stream_key, title, dir, name,
               started_at, last_live_at, blocked) VALUES (?,?,?,?,?,?,?,?,?)""",
        (c["id"], channel, key, info["title"],
         f"{folder_name(c['name'])}/{slug} {local:%Y-%m-%d %H%M}",
         f"{folder_name(c['name'])} live {local:%m-%d}", now, now, blocked)).lastrowid
    own_bounty(conn, sid)
    return start_message(conn, c, sid, channel, info["title"])


def own_bounty(conn: sqlite3.Connection, session_id: int) -> int:
    """The stream's own bounty row: where its clips go until it's moved into a paid bounty.
    Named "live 10-29" (a second stream that day: "live 10-29 2"). Idempotent."""
    sess = conn.execute("SELECT * FROM live_sessions WHERE id=?", (session_id,)).fetchone()
    row = conn.execute("SELECT id FROM bounties WHERE live_session_id=?", (session_id,)).fetchone()
    if row:
        bid = row["id"]
    else:
        base = "live " + sess["name"].rsplit(" ", 1)[-1]
        name, n = base, 1
        while conn.execute("SELECT 1 FROM bounties WHERE campaign_id=? AND lower(name)=lower(?)",
                           (sess["campaign_id"], name)).fetchone():
            n += 1
            name = f"{base} {n}"
        bid = conn.execute("INSERT INTO bounties (campaign_id, name, notes, live_session_id)"
                           " VALUES (?,?,?,?)",
                           (sess["campaign_id"], name, sess["title"], session_id)).lastrowid
    conn.execute("UPDATE live_sessions SET bounty_id=? WHERE id=? AND bounty_id IS NULL",
                 (bid, session_id))
    return bid


def start_message(conn: sqlite3.Connection, c: sqlite3.Row, session_id: int,
                  channel: str, title: str) -> str:
    s = get_settings()
    sess = conn.execute("SELECT * FROM live_sessions WHERE id=?", (session_id,)).fetchone()
    lines = [f"🔴 **{c['name']} is live** on {channel_url(channel)}"
             + (f": “{title}”" if title else "")]
    if jobqueue.remote_worker_alive(conn, s.host_name, s.laptop_grace_s):
        lines.append("Recording on the laptop.")
    else:
        lines.append("⚠️ The laptop worker is offline, so nothing is recording. Start it now.")
    if sess["blocked"]:
        lines.append(f"🚫 Not clipping: the title looks like gambling ({sess['blocked']}).")
    else:
        lines.append(f"Clipping every {s.live_chunk_min} min. Clips land on the Lives tab; move "
                     f"the stream into a bounty there if one comes up: {s.public_base_url}/lives")
    return "\n".join(lines)


def _end_quiet(conn: sqlite3.Connection) -> None:
    """Sessions not seen live for OFFLINE_GRACE_S have ended. One nobody recorded has nothing
    left to finish."""
    now = utcnow()
    conn.execute("UPDATE live_sessions SET state='ended', ended_at=last_live_at,"
                 " finalized_at=CASE WHEN host IS NULL THEN ? END"
                 " WHERE state='live' AND last_live_at < ?", (now, utc_in(-OFFLINE_GRACE_S)))


def move_to(conn: sqlite3.Connection, session_id: int, bounty_id: int | None) -> int:
    """Put a stream's clips (and its later chunks) in a paid bounty, or back in the stream's own
    bounty when bounty_id is None. Unposted clips get the bounty's extra caption tags.
    Returns the number of clips moved."""
    own = own_bounty(conn, session_id)
    to = bounty_id or own
    b = conn.execute("SELECT * FROM bounties WHERE id=?", (to,)).fetchone()
    if not b or (b["live_session_id"] and b["live_session_id"] != session_id):
        raise ValueError("no such bounty")
    conn.execute("UPDATE live_sessions SET bounty_id=? WHERE id=?", (to, session_id))
    conn.execute("UPDATE sources SET bounty_id=? WHERE id IN (SELECT source_id FROM live_chunks"
                 " WHERE session_id=? AND source_id IS NOT NULL)", (to, session_id))
    clips = conn.execute(
        """SELECT k.id, k.caption, k.status FROM clips k JOIN candidates c ON c.id=k.candidate_id
           JOIN live_chunks lc ON lc.source_id=c.source_id WHERE lc.session_id=?""",
        (session_id,)).fetchall()
    tags = b["required_tags"].split()
    for k in clips:
        missing = [t for t in tags if t.lower() not in k["caption"].lower()]
        if missing and k["status"] in ("in_review", "approved"):
            conn.execute("UPDATE clips SET caption=? WHERE id=?",
                         (f"{k['caption']} {' '.join(missing)}".strip(), k["id"]))
    return len(clips)


def queue_chunks(conn: sqlite3.Connection, session_id: int | None = None) -> int:
    """Queue reported chunks of streams that aren't blocked, into the bounty each one is in
    now. Idempotent."""
    s = get_settings()
    rows = conn.execute(
        """SELECT k.id, k.path, k.overlap_ms, ls.campaign_id, ls.bounty_id, ls.host, ls.title
           FROM live_chunks k JOIN live_sessions ls ON ls.id=k.session_id
           JOIN bounties b ON b.id=ls.bounty_id JOIN campaigns c ON c.id=ls.campaign_id
           WHERE k.source_id IS NULL AND ls.blocked IS NULL AND ls.stopped_at IS NULL
             AND ls.delete_asked_at IS NULL AND b.status='active'
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


# What keeps a finished stream's recording past live_keep_hours: chunks still being clipped, and
# picked clips not rendered yet (a bounty holding renders until its rules are in). A chunk with
# no good moments stays at 'selected'; it doesn't keep the recording.
WAITING_JOBS = """SELECT 1 FROM live_chunks k JOIN jobs j ON j.source_id=k.source_id
                  WHERE k.session_id=ls.id AND j.state IN ('queued','running','failed')"""
WAITING_CLIPS = """SELECT 1 FROM live_chunks k JOIN candidates c ON c.source_id=k.source_id
                   WHERE k.session_id=ls.id AND c.kept=1
                   AND NOT EXISTS (SELECT 1 FROM clips cl WHERE cl.candidate_id=c.id)
                   AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.candidate_id=c.id
                                   AND j.kind='render' AND j.state='dead')"""


def _cancel_jobs(conn: sqlite3.Connection, session_id: int, why: str) -> int:
    """Drop the stream's chunks that are queued but not started."""
    return conn.execute(
        "UPDATE jobs SET state='cancelled', last_error=? WHERE state IN ('queued','failed')"
        " AND source_id IN (SELECT source_id FROM live_chunks WHERE session_id=?)",
        (why, session_id)).rowcount


def set_recording(conn: sqlite3.Connection, session_id: int, on: bool) -> bool:
    """Turn a live stream's recording and clipping off or back on. Off stops the recorder and
    drops chunks not started yet; chunks already being clipped finish. False if the stream
    isn't live (or its recording was deleted)."""
    with tx(conn):
        cur = conn.execute(
            "UPDATE live_sessions SET stopped_at=? WHERE id=? AND state='live'"
            " AND delete_asked_at IS NULL", (None if on else utcnow(), session_id))
        if cur.rowcount and not on:
            _cancel_jobs(conn, session_id, "stream turned off")
    if cur.rowcount and on:
        queue_chunks(conn, session_id)
    return bool(cur.rowcount)


def delete_recording(conn: sqlite3.Connection, session_id: int) -> bool:
    """Delete a stream's recording from the laptop the next time it checks in. Not while it's
    recording. Clips already made stay."""
    with tx(conn):
        cur = conn.execute(
            "UPDATE live_sessions SET delete_asked_at=? WHERE id=? AND purged_at IS NULL"
            " AND delete_asked_at IS NULL AND (state='ended' OR stopped_at IS NOT NULL)",
            (utcnow(), session_id))
        if cur.rowcount:
            _cancel_jobs(conn, session_id, "recording deleted")
            # Never recorded anywhere: nothing to delete.
            conn.execute("UPDATE live_sessions SET purged_at=? WHERE id=? AND host IS NULL",
                         (utcnow(), session_id))
    return bool(cur.rowcount)


def for_recorder(conn: sqlite3.Connection, host: str) -> dict:
    """What the laptop's recorder should do: record live sessions (claiming unclaimed ones),
    finish ended ones it recorded, delete old recordings nothing needs any more."""
    s = get_settings()
    with tx(conn):
        conn.execute("UPDATE live_sessions SET host=? WHERE host IS NULL AND state='live'",
                     (host,))
        rows = conn.execute(
            "SELECT id, channel, dir, name, state, stopped_at FROM live_sessions WHERE host=? AND"
            " (state='live' OR finalized_at IS NULL) AND delete_asked_at IS NULL ORDER BY id",
            (host,)).fetchall()
        old = conn.execute(
            f"""SELECT id, dir FROM live_sessions ls WHERE host=? AND purged_at IS NULL
                 AND (delete_asked_at IS NOT NULL OR (finalized_at IS NOT NULL AND ended_at < ?
                      AND NOT EXISTS ({WAITING_JOBS}) AND NOT EXISTS ({WAITING_CLIPS})))""",
            (host, utc_in(-s.live_keep_hours * 3600))).fetchall()
        for r in old:
            conn.execute("UPDATE live_sessions SET purged_at=? WHERE id=?", (utcnow(), r["id"]))
    return {"sessions": [{"id": r["id"], "url": channel_url(r["channel"]), "dir": r["dir"],
                          "name": r["name"],
                          "live": r["state"] == "live" and not r["stopped_at"]} for r in rows],
            "purge": [r["dir"] for r in old], "piece_s": PIECE_S,
            "chunk_s": s.live_chunk_min * 60, "overlap_s": OVERLAP_S,
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
            else f"{n['n']} chunk(s) recorded, not clipped"
                 + (f" ({sess['blocked']})" if sess["blocked"] else ""))
    return f"⚫ **{c['name']}**'s stream ended: {hours:.1f} h recorded, {what}."


def stream_info(conn: sqlite3.Connection, source_id: int) -> dict | None:
    """For a source cut from a live recording: the streamer (the campaign's name), the guests
    Easton listed for that stream, its title and the streamer's layout (NULL: auto). None for
    ordinary footage."""
    r = conn.execute(
        """SELECT c.name streamer, ls.guests, ls.title, c.live_layout, c.facecam
           FROM live_chunks k
           JOIN live_sessions ls ON ls.id=k.session_id JOIN campaigns c ON c.id=ls.campaign_id
           WHERE k.source_id=?""", (source_id,)).fetchone()
    return dict(r) if r else None


def guest_names(guests: str) -> list[str]:
    """"Alabama Barker, Adrien Broner (AB)" -> ["Alabama Barker", "Adrien Broner (AB)"]."""
    return [g.strip() for g in guests.replace("\n", ",").split(",") if g.strip()]


def hashtags(streamer: str, guests: str, text: str) -> list[str]:
    """The streamer's tag, plus a tag for each listed guest the hook or title names (by full
    name or the nickname in brackets): "#n3on #alabamabarker"."""
    slug = lambda n: "#" + re.sub(r"[^a-z0-9]", "", n.lower())  # noqa: E731
    tags = [slug(streamer)]
    low = text.lower()
    for g in guest_names(guests):
        name, _, nick = g.partition("(")
        nick = nick.rstrip(")").strip()
        if name.strip().lower() in low or (nick and re.search(rf"\b{re.escape(nick.lower())}\b", low)):
            tags.append(slug(name.strip()))
    return [t for t in dict.fromkeys(tags) if len(t) > 2]


def sessions(conn: sqlite3.Connection, bounty_id: int | None = None,
             limit: int = 30) -> list[dict]:
    """Recent streams, newest first (only those in one bounty, if given), each with its
    streamer, the bounty its clips are in and their counts by stage."""
    rows = conn.execute(
        f"""SELECT ls.*, c.name streamer, b.name bounty, b.live_session_id own,
                  b.campaign_id bounty_cid, bc.name bounty_campaign,
                  (SELECT COUNT(*) FROM live_chunks k WHERE k.session_id=ls.id) chunks,
                  (SELECT COUNT(source_id) FROM live_chunks k WHERE k.session_id=ls.id) queued,
                  {", ".join(_count(st) for st in ("in_review", "approved", "posted"))}
           FROM live_sessions ls JOIN campaigns c ON c.id=ls.campaign_id
           LEFT JOIN bounties b ON b.id=ls.bounty_id LEFT JOIN campaigns bc ON bc.id=b.campaign_id
           WHERE ? IS NULL OR ls.bounty_id=? ORDER BY ls.id DESC LIMIT ?""",
        (bounty_id, bounty_id, limit)).fetchall()
    return [dict(r) for r in rows]


_CHUNK_AT = re.compile(r"#\d+ (\d+)h(\d\d)-")


def timeline(conn: sqlite3.Connection, session: dict) -> dict:
    """Where a stream's clips sit along it, for the Lives tab (D35): one mark per clip, placed
    by its chunk's start (from the chunk name, "… #04 0h15-0h20") plus its offset in the chunk.
    Positions are percent of the stream so far."""
    rows = conn.execute(
        """SELECT lc.path, ca.start_ms, ca.end_ms, k.status FROM live_chunks lc
           JOIN candidates ca ON ca.source_id=lc.source_id JOIN clips k ON k.candidate_id=ca.id
           WHERE lc.session_id=? ORDER BY ca.start_ms""", (session["id"],)).fetchall()
    marks = []
    for r in rows:
        m = _CHUNK_AT.search(r["path"].replace("\\", "/").rsplit("/", 1)[-1])
        if m:
            at = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + r["start_ms"] / 1000
            marks.append((at, (r["end_ms"] - r["start_ms"]) / 1000, r["status"]))
    total = max([session["recorded_s"] or 0] + [a + d for a, d, _ in marks]) or 1
    chunk = get_settings().live_chunk_min * 60
    return {"marks": [{"left": round(a * 100 / total, 2), "width": round(d * 100 / total, 2),
                       "status": st} for a, d, st in marks],
            "seg": round(min(100.0, chunk * 100 / total), 3), "total": total}


def _count(status: str) -> str:
    return (f"(SELECT COUNT(*) FROM live_chunks lc JOIN candidates ca ON ca.source_id=lc.source_id"
            f" JOIN clips k ON k.candidate_id=ca.id WHERE lc.session_id=ls.id"
            f" AND k.status='{status}') {status}")
