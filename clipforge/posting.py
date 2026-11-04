"""Posting by hand (D38): Easton's accounts, the caption check before copying, the one-moment-
per-platform rule, the brand's review of each submission, and how many posts a day.

ClipForge never posts or submits anything itself: these only record what Easton did."""

import re
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from clipforge import money
from clipforge.captions import mentions, missing_names, name_list  # noqa: F401 (web app)
from clipforge.config import get_settings
from clipforge.db import utcnow
from clipforge.live import _CHUNK_AT

REVIEW_STEPS = ("pending", "approved", "rejected", "paid")


# ---------- accounts ----------

def accounts(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM accounts WHERE ? = 0 OR enabled = 1 ORDER BY platform, handle",
        (int(enabled_only),))]


def add_account(conn: sqlite3.Connection, platform: str, handle: str, niche: str = "") -> int:
    handle = handle.strip()
    if not handle:
        raise ValueError("Give the account's handle, e.g. @easton.clips")
    handle = handle if handle.startswith("@") else f"@{handle}"
    # Instagram stops recommending accounts that mostly repost: off until Easton turns it on.
    cur = conn.execute("INSERT INTO accounts (platform, handle, niche, enabled) VALUES (?,?,?,?)"
                       " ON CONFLICT(platform, handle) DO NOTHING",
                       (platform, handle, niche.strip(), int(platform != "instagram")))
    if not cur.rowcount:
        raise ValueError(f"{handle} on {platform} is already listed")
    return cur.lastrowid


# ---------- caption ----------

def caption_problems(caption: str, required_tags: str, disclosure: str = "#ad",
                     must_mention: str = "", mention_all: bool = False) -> list[str]:
    """What the caption is missing: the disclosure first, every required tag/mention, and one of
    the names the brief says it must mention (every one of them with mention_all)."""
    out = []
    if disclosure.strip() and not caption.lstrip().lower().startswith(disclosure.strip().lower()):
        out.append(f"{disclosure.strip()} must come first")
    have = {t.lower() for t in re.findall(r"[#@][\w.]+", caption)}
    out += [f"missing {t}" for t in dict.fromkeys(required_tags.replace(",", " ").split())
            if t.lower() not in have]
    if miss := missing_names(caption, must_mention, mention_all):
        out.append(f"must name {' and '.join(miss)}" if mention_all
                   else f"must name {' or '.join(name_list(must_mention)[:3])}")
    return out


def fix_caption(caption: str, required_tags: str, disclosure: str = "#ad",
                must_mention: str = "", mention_all: bool = False) -> str:
    """Put the disclosure first, name the first must-mention name after it, and add the missing
    tags at the end."""
    text = caption.strip()
    d = disclosure.strip()
    if d and text.lower().startswith(d.lower()):
        text = text[len(d):].strip()
    elif d:
        text = re.sub(rf"(^|\s){re.escape(d)}(?=\s|$)", " ", text, flags=re.I).strip()
    if miss := missing_names(text, must_mention, mention_all):
        text = f"{' x '.join(miss)}: {text}"
    text = f"{d} {text}".strip()
    have = {t.lower() for t in re.findall(r"[#@][\w.]+", text)}
    missing = [t for t in dict.fromkeys(required_tags.replace(",", " ").split())
               if t.lower() not in have]
    return f"{text}\n\n{' '.join(missing)}" if missing else text


# ---------- one moment, once per platform ----------

def _moment(conn: sqlite3.Connection, clip_id: int) -> tuple[str, int, int] | None:
    """Where a clip sits in its footage: (footage key, start ms, end ms). A live chunk is placed
    on its stream's timeline, so overlapping chunks of one stream compare."""
    r = conn.execute(
        """SELECT c.source_id, c.start_ms, c.end_ms, s.skip_ms, k.session_id, k.path
           FROM clips cl JOIN candidates c ON c.id=cl.candidate_id JOIN sources s
           ON s.id=c.source_id LEFT JOIN live_chunks k ON k.source_id=s.id
           WHERE cl.id=?""", (clip_id,)).fetchone()
    if not r:
        return None
    if r["session_id"] and (m := _CHUNK_AT.search(r["path"].replace("\\", "/"))):
        at = (int(m.group(1)) * 3600 + int(m.group(2)) * 60) * 1000 - (r["skip_ms"] or 0)
        return f"live:{r['session_id']}", at + r["start_ms"], at + r["end_ms"]
    return f"src:{r['source_id']}", r["start_ms"], r["end_ms"]


def already_posted(conn: sqlite3.Connection, clip_id: int, platform: str) -> int | None:
    """Another clip of the same moment (overlapping by half the shorter one) already posted on
    this platform: its id. Posting a moment twice on one platform reads as a repost."""
    me = _moment(conn, clip_id)
    if not me:
        return None
    for r in conn.execute("SELECT DISTINCT clip_id FROM posts WHERE platform=? AND clip_id!=?",
                          (platform, clip_id)):
        other = _moment(conn, r["clip_id"])
        if other and other[0] == me[0]:
            overlap = min(me[2], other[2]) - max(me[1], other[1])
            if overlap > 0.5 * min(me[2] - me[1], other[2] - other[1]):
                return r["clip_id"]
    return None


# ---------- the brand's review ----------

def set_review(conn: sqlite3.Connection, post_id: int, step: str, *, reason: str = "",
               approved_views: int | None = None, earned_usd: float | None = None) -> str:
    """Record what the marketplace did with a submission. Returns a message for Easton."""
    if step not in REVIEW_STEPS:
        raise ValueError(step)
    now = utcnow()
    if step == "rejected" and not reason.strip():
        raise ValueError("Write the brand's reason: it shows which clips they turn down")
    if step == "paid" and earned_usd is None:
        raise ValueError("Enter what the payout was")
    conn.execute(
        """UPDATE posts SET review_status=?, submitted_at=COALESCE(submitted_at, ?),
               reject_reason=CASE WHEN ?='rejected' THEN ? ELSE NULL END,
               approved_at=CASE WHEN ? IN ('approved','paid') THEN COALESCE(approved_at, ?)
                           ELSE NULL END,
               approved_views=COALESCE(?, approved_views),
               earned_usd=CASE WHEN ?='paid' THEN ? WHEN ?='rejected' THEN 0 ELSE NULL END,
               paid_at=CASE WHEN ?='paid' THEN COALESCE(paid_at, ?) ELSE NULL END
           WHERE id=?""",
        (step, now, step, reason.strip(), step, now, approved_views, step, earned_usd, step,
         step, now, post_id))
    return {"pending": "Waiting for the brand's review", "approved": "Marked approved",
            "rejected": "Marked rejected", "paid": f"Marked paid: ${earned_usd or 0:,.2f}"}[step]


# ---------- cadence ----------

def today_local() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def cadence(conn: sqlite3.Connection) -> list[dict]:
    """For each enabled account: posts today against today's target. One a day while it warms
    up; after warmup_days, up to posts_per_day_max, but only while its p90 views hold (the
    last 14 days' p90 at least the 14 days before)."""
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    today = today_local()
    start = datetime.combine(today, datetime.min.time(), tz).astimezone(ZoneInfo("UTC"))
    out = []
    for a in accounts(conn, enabled_only=True):
        day = (today - date.fromisoformat(a["started_at"][:10])).days + 1
        posted = conn.execute("SELECT COUNT(*) FROM posts WHERE account_id=? AND posted_at>=?",
                              (a["id"], start.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()[0]
        target, why = 1, f"warm-up day {day} of {s.warmup_days}"
        if day > s.warmup_days:
            recent, before = _p90(conn, a["id"], 0, 14), _p90(conn, a["id"], 14, 28)
            holds = recent is not None and (before is None or recent >= before)
            target = s.posts_per_day_max if holds else 1
            why = (f"p90 {recent:,} views is holding" if holds and recent is not None
                   else "p90 dropped: stay at 1 a day" if recent is not None
                   else "no views logged yet")
        out.append({**a, "day": day, "posted": posted, "target": target, "why": why})
    return out


def _p90(conn: sqlite3.Connection, account_id: int, from_days: int, to_days: int) -> int | None:
    now = datetime.now(ZoneInfo("UTC"))
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    views = [r[0] for r in conn.execute(
        "SELECT COALESCE(MAX(v.views), 0) FROM posts p LEFT JOIN view_snapshots v ON"
        " v.post_id=p.id WHERE p.account_id=? AND p.posted_at < ? AND p.posted_at >= ?"
        " GROUP BY p.id", (account_id, (now - timedelta(days=from_days)).strftime(fmt),
                           (now - timedelta(days=to_days)).strftime(fmt)))]
    return money.percentile(views, 0.9) if len(views) >= 5 else None
