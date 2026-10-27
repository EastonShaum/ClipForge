"""Campaign scouting: store bounties read from marketplace screenshots and rank them.

The score is a plain formula so you can see why one bounty beats another:
  pay rate (40) + budget left (35) + content fit (15) + payout method (10),
minus 15 if the fill speed says it will close within 2 days. Pot bounties score low on pay
(a new account rarely reaches their minimum views). Gambling is blocked, as for campaigns."""

import re
import sqlite3
from datetime import datetime

from clipforge.campaigns import CampaignIn, blocked_reason

GOOD_FIT = {"podcast", "interview", "gaming", "streamer"}
TOP_RATE_PER_100K = 200.0  # $/100K that earns the full pay score
CLOSING_DAYS = 2.0
MIN_TRACK_HOURS = 6  # fill speed needs screenshots at least this far apart


def key(campaign: str, bounty: str) -> str:
    norm = lambda t: re.sub(r"[^a-z0-9]+", "", t.lower())  # noqa: E731
    return f"{norm(campaign)}|{norm(bounty)}"


def store(conn: sqlite3.Connection, shot_id: int, bounties: list[dict]) -> int:
    """Replace this screenshot's rows (a retry must not duplicate them)."""
    seen_at = conn.execute("SELECT created_at FROM scout_shots WHERE id=?",
                           (shot_id,)).fetchone()["created_at"]
    conn.execute("DELETE FROM scout_bounties WHERE shot_id=?", (shot_id,))
    for b in bounties:
        conn.execute(
            """INSERT INTO scout_bounties (shot_id, key, campaign, bounty, status, kind, rate_usd,
                   rate_per_views, pot_usd, min_views, filled_pct, clips, views, payout,
                   category, notes, seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (shot_id, key(b["campaign"], b.get("bounty", "")), b["campaign"],
             b.get("bounty", ""), b.get("status", "unknown"), b.get("kind", "rate"),
             b.get("rate_usd"), b.get("rate_per_views"), b.get("pot_usd"), b.get("min_views"),
             b.get("filled_pct"), b.get("clips"), b.get("views"), b.get("payout", "unknown"),
             b.get("category", "other"), b.get("notes", ""), seen_at))
    return len(bounties)


def _days(a: str, b: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds() / 86400


def fill_speed(history: list[sqlite3.Row]) -> float | None:
    """Percent filled per day, from the oldest and newest readings far enough apart."""
    pts = [h for h in history if h["filled_pct"] is not None]
    if len(pts) < 2:
        return None
    first, last = pts[0], pts[-1]
    days = _days(first["seen_at"], last["seen_at"])
    if days * 24 < MIN_TRACK_HOURS:
        return None
    return max(0.0, (last["filled_pct"] - first["filled_pct"]) / days)


def score(b: dict) -> tuple[float, list[str]]:
    why = []
    if b["kind"] == "pot":
        pay = 10.0
        why.append("pot bounty: needs high view minimums")
    else:
        pay = 40 * min(1.0, (b["per_100k"] or 0) / TOP_RATE_PER_100K)
    filled = b["filled_pct"]
    room = 17.5 if filled is None else 35 * (1 - filled / 100)
    fit = 15.0 if b["category"] in GOOD_FIT else 5.0
    payout = {"paypal": 10.0, "crypto": 3.0}.get(b["payout"], 5.0)
    if b["payout"] == "crypto":
        why.append("crypto payout (wallet + fees)")
    total = pay + room + fit + payout
    if b["days_left"] is not None and b["days_left"] < CLOSING_DAYS:
        total -= 15
        why.append("closing within 2 days")
    if filled is not None and filled >= 90:
        why.append("almost full")
    return round(max(0.0, total), 1), why


def ranked(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Latest reading of every bounty, scored. Returns {'open': [...], 'other': [...]}."""
    rows = conn.execute("SELECT * FROM scout_bounties ORDER BY seen_at, id").fetchall()
    history: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        history.setdefault(r["key"], []).append(r)
    open_, other = [], []
    for hist in history.values():
        b = dict(hist[-1])
        b["readings"] = len(hist)
        b["per_100k"] = (b["rate_usd"] * 100000 / b["rate_per_views"]
                         if b["rate_usd"] is not None and b["rate_per_views"] else None)
        b["avg_views"] = b["views"] // b["clips"] if b["views"] and b["clips"] else None
        speed = fill_speed(hist)
        b["speed"] = speed
        b["days_left"] = ((100 - b["filled_pct"]) / speed
                          if speed and b["filled_pct"] is not None else None)
        b["score"], b["why"] = score(b)
        b["blocked"] = blocked_reason(CampaignIn(name=b["campaign"], category=b["category"],
                                                 notes=f"{b['bounty']} {b['notes']}"))
        if b["blocked"] or b["status"] in ("ended", "disabled"):
            other.append(b)
        else:
            open_.append(b)
    open_.sort(key=lambda b: -b["score"])
    return {"open": open_, "other": other}


def summary(conn: sqlite3.Connection, base_url: str, n: int = 3) -> str:
    r = ranked(conn)
    if not r["open"]:
        return f"🔎 Scout: no open bounties found. {base_url}/scout"
    lines = [f"🔎 **Scout: top {min(n, len(r['open']))} of {len(r['open'])} open bounties**"]
    for i, b in enumerate(r["open"][:n], 1):
        rate = (f"${b['per_100k']:.0f}/100K" if b["per_100k"] is not None
                else f"${b['pot_usd'] or 0:,.0f} pot")
        filled = f"{b['filled_pct']:.0f}% filled" if b["filled_pct"] is not None else "fill ?"
        lines.append(f"{i}. {b['campaign']} · {b['bounty'] or '—'}: {rate}, {filled}, "
                     f"{b['payout']} (score {b['score']:.0f})")
    lines.append(f"{base_url}/scout")
    return "\n".join(lines)
