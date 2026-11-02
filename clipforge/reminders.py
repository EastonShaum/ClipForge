"""Discord reminders: when to post, which clip, campaigns about to end, and post links that still
need submitting to the campaign. Each reminder fires once (keyed in settings_kv), and a missed
one is dropped rather than sent late, so a Pi reboot doesn't replay a day of pings."""

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from clipforge import notify
from clipforge.bounties import held_count, hold_reasons
from clipforge.config import Settings, get_settings

LATE_GRACE = timedelta(hours=2)  # a slot missed by more than this is skipped, not sent late
ENDING_SOON_DAYS = 3
SUBMIT_AFTER = timedelta(minutes=30)  # give a fresh post time to be submitted before nagging
HELD_REPEAT = timedelta(hours=3)  # clips waiting on requirements: first ping at once, then this
# "Man On · episode-4"; a migrated campaign's lone "main" bounty shows just the campaign.
LABEL = "CASE WHEN b.name IS NULL OR b.name='main' THEN cp.name ELSE cp.name || ' · ' || b.name END"


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
        f"""SELECT k.id, k.status, c.hook_text, c.score, {LABEL} campaign, b.end_date,
                   cp.id cid, b.id bid,
                   cp.allowed_platforms, GROUP_CONCAT(p.platform) done
            FROM clips k JOIN candidates c ON c.id=k.candidate_id
            JOIN sources s ON s.id=c.source_id JOIN campaigns cp ON cp.id=s.campaign_id
            LEFT JOIN bounties b ON b.id=s.bounty_id LEFT JOIN posts p ON p.clip_id=k.id
            WHERE k.status IN ('approved','posted') AND cp.status='active'
              AND COALESCE(b.status, 'active')='active'
            GROUP BY k.id
            ORDER BY b.end_date IS NULL, b.end_date, c.score DESC, k.id""").fetchall()
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
        f"""SELECT p.id, p.platform, p.url, c.hook_text, {LABEL} campaign, cp.id cid, b.id bid
            FROM posts p JOIN clips k ON k.id=p.clip_id JOIN candidates c ON c.id=k.candidate_id
            JOIN sources s ON s.id=c.source_id JOIN campaigns cp ON cp.id=s.campaign_id
            LEFT JOIN bounties b ON b.id=s.bounty_id
            WHERE p.submitted_at IS NULL AND p.posted_at <= ? ORDER BY p.id""",
        (cutoff,)).fetchall()


def ending_soon(conn: sqlite3.Connection, today: date) -> list[sqlite3.Row]:
    last = (today + timedelta(days=ENDING_SOON_DAYS)).isoformat()
    return conn.execute(
        f"SELECT {LABEL} name, b.end_date FROM bounties b JOIN campaigns cp ON cp.id=b.campaign_id"
        " WHERE b.status='active' AND cp.status='active' AND b.end_date IS NOT NULL"
        " AND b.end_date BETWEEN ? AND ? ORDER BY b.end_date",
        (today.isoformat(), last)).fetchall()


def _submit_lines(rows, base: str) -> list[str]:
    if not rows:
        return []
    lines = [f"📝 **{len(rows)} post link(s) not submitted to the campaign yet** "
             "(mark them done on each bounty's Posted tab):"]
    lines += [f"• {r['campaign']} · {r['platform']}: {r['url']} → "
              f"{base}/campaigns/{r['cid']}/bounties/{r['bid']}/posted#post{r['id']}"
              for r in rows[:8]]
    return lines


def slot_message(conn: sqlite3.Connection, s: Settings, now_utc: datetime) -> str | None:
    base = s.public_base_url
    queue = next_to_post(conn)
    if queue:
        c = queue[0]
        lines = [f"⏰ **Time to post:** \"{c['hook_text']}\" ({c['campaign']}) → "
                 f"{', '.join(c['todo'])}",
                 f"{base}/campaigns/{c['cid']}/bounties/{c['bid']}/post#clip{c['id']}"]
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


def held_messages(conn: sqlite3.Connection, s: Settings, now_utc: datetime) -> list[str]:
    """Bounties whose clips are picked but held until their requirements are in (D30)."""
    out = []
    rows = conn.execute(
        f"SELECT b.*, {LABEL} label FROM bounties b JOIN campaigns cp ON cp.id=b.campaign_id"
        " WHERE b.render_hold=1 AND b.status='active' AND cp.status='active'").fetchall()
    for b in rows:
        n, missing = held_count(conn, b["id"]), hold_reasons(conn, b)
        if not n or not missing:
            continue
        key = f"held-ping:{b['id']}"
        last = conn.execute("SELECT value FROM settings_kv WHERE key=?", (key,)).fetchone()
        if last and now_utc - datetime.fromisoformat(last["value"]) < HELD_REPEAT:
            continue
        conn.execute("INSERT INTO settings_kv (key, value) VALUES (?, ?) ON CONFLICT(key)"
                     " DO UPDATE SET value=excluded.value", (key, now_utc.isoformat()))
        out.append(f"⏸️ **{n} clip(s) for {b['label']} are picked and waiting to render.** "
                   f"Still needed: {', '.join(missing)}. Add them (or tap Render now): "
                   f"{s.public_base_url}/campaigns/{b['campaign_id']}/bounties/{b['id']}#rules")
    return out


SUBMIT_WITHIN = timedelta(hours=20)  # ping before a post is a day old and still unsubmitted


def money_messages(conn: sqlite3.Connection, s: Settings, now_utc: datetime) -> list[str]:
    """D38, each once: a bounty's budget under budget_low_ratio while its posts wait for
    approval; a post near its minimum views (worth replying to comments or posting elsewhere);
    posts on a paid bounty not submitted 20 h after posting."""
    out = []
    base = s.public_base_url
    for b in conn.execute(
            f"""SELECT b.*, {LABEL} label, COUNT(p.id) waiting FROM bounties b
                JOIN campaigns cp ON cp.id=b.campaign_id
                JOIN sources src ON src.bounty_id=b.id JOIN candidates c ON c.source_id=src.id
                JOIN clips k ON k.candidate_id=c.id JOIN posts p ON p.clip_id=k.id
                WHERE b.status='active' AND b.budget_remaining_usd IS NOT NULL
                  AND b.budget_total_usd > 0
                  AND b.budget_remaining_usd < b.budget_total_usd * ?
                  AND COALESCE(p.review_status, 'pending')='pending'
                GROUP BY b.id""", (s.budget_low_ratio,)):
        if _once(conn, f"budget-low:{b['id']}:{b['budget_checked_at']}"):
            out.append(f"💸 **{b['label']}: ${b['budget_remaining_usd']:,.0f} budget left** of"
                       f" ${b['budget_total_usd']:,.0f}, with {b['waiting']} post(s) not approved"
                       " yet. Submit the rest now, and check the budget again: "
                       f"{base}/campaigns/{b['campaign_id']}/bounties/{b['id']}/posted")
    for p in conn.execute(
            f"""SELECT p.id, p.platform, p.url, {LABEL} label, b.min_views, cp.id cid, b.id bid,
                       (SELECT MAX(views) FROM view_snapshots v WHERE v.post_id=p.id) views
                FROM posts p JOIN clips k ON k.id=p.clip_id JOIN candidates c
                ON c.id=k.candidate_id JOIN sources src ON src.id=c.source_id
                JOIN bounties b ON b.id=src.bounty_id JOIN campaigns cp ON cp.id=b.campaign_id
                WHERE b.min_views > 0 AND COALESCE(p.review_status, '') != 'rejected'"""):
        if p["views"] and p["min_views"] * s.near_min_ratio <= p["views"] < p["min_views"] \
                and _once(conn, f"near-min:{p['id']}"):
            out.append(f"📈 **A {p['platform']} post for {p['label']} is at {p['views']:,} of"
                       f" {p['min_views']:,} views**, close to paying. Reply to its comments, or"
                       f" post the clip on another platform: {p['url']}")
    cutoff = (now_utc - SUBMIT_WITHIN).strftime("%Y-%m-%dT%H:%M:%SZ")
    late = [p for p in conn.execute(
        f"""SELECT p.id, p.platform, p.url, {LABEL} label, cp.id cid, b.id bid FROM posts p
            JOIN clips k ON k.id=p.clip_id JOIN candidates c ON c.id=k.candidate_id
            JOIN sources src ON src.id=c.source_id JOIN bounties b ON b.id=src.bounty_id
            JOIN campaigns cp ON cp.id=b.campaign_id
            WHERE p.submitted_at IS NULL AND p.posted_at < ? AND b.live_session_id IS NULL
              AND b.status='active'""", (cutoff,)) if _once(conn, f"submit-24h:{p['id']}")]
    if late:
        lines = [f"⏰ **{len(late)} post(s) still not submitted, posted 20+ h ago.** Submit each"
                 " before it's a day old:"]
        lines += [f"• {p['label']} ({p['platform']}): {base}/campaigns/{p['cid']}/bounties/"
                  f"{p['bid']}/posted#post{p['id']}" for p in late]
        out.append("\n".join(lines))
    return out


def tick(conn: sqlite3.Connection, now_utc: datetime | None = None) -> list[str]:
    """Send whatever reminders are due. Returns what was sent (for tests and logs)."""
    s = get_settings()
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    local = now_utc.astimezone(ZoneInfo(s.timezone))
    sent = []
    for msg in held_messages(conn, s, now_utc) + money_messages(conn, s, now_utc):
        notify.discord(msg)
        sent.append(msg)
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
