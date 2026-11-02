"""The home screen (D35): what needs Easton now, newest work first. Each item links to the bounty
tab where it gets done."""

import sqlite3

from clipforge.analytics import post_rows
from clipforge.bounties import label
from clipforge.config import get_settings
from clipforge.db import utc_in

STALE_VIEWS_S = 24 * 3600  # a post whose views weren't logged in a day


def _by_bounty(conn: sqlite3.Connection, status: str) -> list[dict]:
    """Bounties with clips at this stage, most first, with the best score among them."""
    rows = conn.execute(
        """SELECT s.bounty_id bid, b.campaign_id cid, b.name, b.live_session_id,
                  cp.name campaign, COUNT(*) n, MAX(c.score) best
           FROM clips k JOIN candidates c ON c.id=k.candidate_id JOIN sources s ON s.id=c.source_id
           JOIN bounties b ON b.id=s.bounty_id JOIN campaigns cp ON cp.id=b.campaign_id
           WHERE k.status=? GROUP BY s.bounty_id ORDER BY n DESC, best DESC""",
        (status,)).fetchall()
    return [{**dict(r), "label": live_label(r)} for r in rows]


def live_label(r) -> str:
    """"N3on live 10-28" for a stream's own bounty, "Man On, episode-4" otherwise."""
    if r["live_session_id"]:
        return f"{r['campaign']} {r['name']}"
    return label(r["campaign"], r["name"]).replace(" · ", ", ")


def _posts_where(conn: sqlite3.Connection, where: str, args: tuple = ()) -> list[dict]:
    rows = conn.execute(
        f"""SELECT s.bounty_id bid, b.campaign_id cid, b.name, b.live_session_id,
                   cp.name campaign, COUNT(*) n
            FROM posts p JOIN clips k ON k.id=p.clip_id JOIN candidates c ON c.id=k.candidate_id
            JOIN sources s ON s.id=c.source_id JOIN bounties b ON b.id=s.bounty_id
            JOIN campaigns cp ON cp.id=b.campaign_id WHERE {where}
            GROUP BY s.bounty_id ORDER BY n DESC""", args).fetchall()
    return [{**dict(r), "label": live_label(r)} for r in rows]


def needs_you(conn: sqlite3.Connection) -> dict:
    stale = utc_in(-STALE_VIEWS_S)
    return {
        "review": _by_bounty(conn, "in_review"),
        "post": _by_bounty(conn, "approved"),
        "views": _posts_where(
            conn, "p.posted_at < ? AND NOT EXISTS (SELECT 1 FROM view_snapshots v WHERE"
            " v.post_id=p.id AND v.taken_at >= ?)", (stale, stale)),
        # Links go to the campaign site only for a paid bounty.
        "submit": _posts_where(conn, "p.submitted_at IS NULL AND b.live_session_id IS NULL"),
    }


def progress(conn: sqlite3.Connection, bounty: dict) -> dict:
    """A bounty's views against its payout line: the best clip's views, all views, posts and
    the estimated pay (analytics.post_rows). Plus its budget: low (under budget_low_ratio of
    the total) and whether posts waiting for the brand's approval could use up what's left."""
    posts = [p for p in post_rows(conn) if p["bounty_id"] == bounty["id"]]
    best = max((p["views"] for p in posts), default=0)
    line = bounty.get("min_views") or 0
    left, total = bounty.get("budget_remaining_usd"), bounty.get("budget_total_usd")
    pending = sum(p["earn"] for p in posts if p["review_status"] in (None, "pending"))
    warn = ""
    if left is not None and pending > left:
        warn = (f"Posts waiting for approval could earn {pending:,.2f} dollars, more than the"
                " budget left: submit soon, or pick another bounty.")
    return {"posts": len(posts), "views": sum(p["views"] for p in posts), "best": best,
            "line": line, "pct": min(100.0, best * 100 / line) if line else None,
            "earn": sum(p["earn"] for p in posts), "pending": pending,
            "budget_low": bool(left is not None and total
                               and left < total * get_settings().budget_low_ratio),
            "budget_warn": warn}
