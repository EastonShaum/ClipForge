"""Discord reminders: when to post, which clip, campaigns about to end, and post links that still
need submitting to the campaign. Each reminder fires once (keyed in settings_kv), and a missed
one is dropped rather than sent late, so a Pi reboot doesn't replay a day of pings."""

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from clipforge import notify
from clipforge.config import Settings, get_settings

LATE_GRACE = timedelta(hours=2)  # a slot missed by more than this is skipped, not sent late
ENDING_SOON_DAYS = 3
SUBMIT_AFTER = timedelta(minutes=30)  # give a fresh post time to be submitted before nagging


def _once(conn: sqlite3.Connection, key: str) -> bool:
    """True the first time a key is claimed."""
    return conn.execute("INSERT INTO settings_kv (key, value) VALUES (?, ?)"
                        " ON CONFLICT(key) DO NOTHING",
                        (key, datetime.now().isoformat())).rowcount == 1


def _due(now: datetime, hhmm: str) -> bool:
    h, m = (int(x) for x in hhmm.split(":"))
    at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return at <= now < at + LATE_GRACE


def next_to_post(conn: sqlite3.Connection) -> list[dict]:
    """Approved or part-posted clips with platforms still to go: campaigns ending soonest first,
    then the highest score."""
    rows = conn.execute(
        """SELECT k.id, k.status, c.hook_text, c.score, cp.name campaign, cp.end_date,
                  cp.allowed_platforms, GROUP_CONCAT(p.platform) done
           FROM clips k JOIN candidates c ON c.id=k.candidate_id
           JOIN sources s ON s.id=c.source_id JOIN campaigns cp ON cp.id=s.campaign_id
           LEFT JOIN posts p ON p.clip_id=k.id
           WHERE k.status IN ('approved','posted') AND cp.status='active'
           GROUP BY k.id
           ORDER BY cp.end_date IS NULL, cp.end_date, c.score DESC, k.id""").fetchall()
    out = []
    for r in rows:
        done = set((r["done"] or "").split(","))
        todo = [p for p in r["allowed_platforms"].split(",") if p.strip() and p.strip() not in done]
        if todo:
            out.append({**dict(r), "todo": [p.strip() for p in todo]})
    return out


def unsubmitted(conn: sqlite3.Connection, now_utc: datetime) -> list[sqlite3.Row]:
    cutoff = (now_utc - SUBMIT_AFTER).strftime("%Y-%m-%dT%H:%M:%SZ")
    return conn.execute(
        """SELECT p.id, p.platform, p.url, c.hook_text, cp.name campaign
           FROM posts p JOIN clips k ON k.id=p.clip_id JOIN candidates c ON c.id=k.candidate_id
           JOIN sources s ON s.id=c.source_id JOIN campaigns cp ON cp.id=s.campaign_id
           WHERE p.submitted_at IS NULL AND p.posted_at <= ? ORDER BY p.id""",
        (cutoff,)).fetchall()


def ending_soon(conn: sqlite3.Connection, today: date) -> list[sqlite3.Row]:
    last = (today + timedelta(days=ENDING_SOON_DAYS)).isoformat()
    return conn.execute(
        "SELECT name, end_date FROM campaigns WHERE status='active' AND end_date IS NOT NULL"
        " AND end_date BETWEEN ? AND ? ORDER BY end_date", (today.isoformat(), last)).fetchall()


def _submit_lines(rows, base: str) -> list[str]:
    if not rows:
        return []
    lines = [f"📝 **{len(rows)} post link(s) not submitted to the campaign yet** "
             f"(mark them done at {base}/approved):"]
    lines += [f"• {r['campaign']} · {r['platform']}: {r['url']}" for r in rows[:8]]
    return lines


def slot_message(conn: sqlite3.Connection, s: Settings, now_utc: datetime) -> str | None:
    base = s.public_base_url
    queue = next_to_post(conn)
    if queue:
        c = queue[0]
        lines = [f"⏰ **Time to post:** \"{c['hook_text']}\" ({c['campaign']}) → "
                 f"{', '.join(c['todo'])}", f"{base}/approved#clip{c['id']}"]
        if len(queue) > 1:
            lines.append(f"{len(queue) - 1} more clip(s) waiting after this one.")
    else:
        waiting = conn.execute(
            "SELECT COUNT(*) FROM clips WHERE status='in_review'").fetchone()[0]
        if not waiting:
            lines = []
        else:
            lines = [f"⏰ Post slot, but nothing is approved. {waiting} clip(s) to review: "
                     f"{base}/review"]
    lines += _submit_lines(unsubmitted(conn, now_utc), base)
    return "\n".join(lines) or None


def digest_message(conn: sqlite3.Connection, s: Settings, now_utc: datetime,
                   today: date) -> str | None:
    lines = []
    for c in ending_soon(conn, today):
        days = (date.fromisoformat(c["end_date"]) - today).days
        when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        lines.append(f"⌛ **{c['name']}** ends {when} ({c['end_date']}). Post and submit "
                     "everything for it before then.")
    lines += _submit_lines(unsubmitted(conn, now_utc), s.public_base_url)
    return "\n".join(lines) or None


def tick(conn: sqlite3.Connection, now_utc: datetime | None = None) -> list[str]:
    """Send whatever reminders are due. Returns what was sent (for tests and logs)."""
    s = get_settings()
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    local = now_utc.astimezone(ZoneInfo(s.timezone))
    sent = []
    due = [("slot", t) for t in s.post_slots] + [("digest", s.digest_time)]
    for kind, hhmm in due:
        if not _due(local, hhmm) or not _once(
                conn, f"remind:{kind}:{local.date().isoformat()}:{hhmm}"):
            continue
        msg = (slot_message(conn, s, now_utc) if kind == "slot"
               else digest_message(conn, s, now_utc, local.date()))
        if msg:
            notify.discord(msg)
            sent.append(msg)
    return sent
