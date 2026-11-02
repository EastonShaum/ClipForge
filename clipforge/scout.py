"""Campaign scouting: store bounties read from marketplace screenshots and rank them.

Ranked by expected dollars per clip (D38): each bounty's payout rule (minimum, rate, max payout,
bonus, fee) weighted by the odds of a clip's views: default odds until the accounts have their
own (money.odds). Marked down for a small budget left, an end date under 3 weeks, a fast fill
or crypto payouts; the reasons are listed. Gambling and prediction markets are blocked, as for
campaigns."""

import re
import sqlite3
from datetime import UTC, datetime

from clipforge import money
from clipforge.campaigns import CampaignIn, blocked_reason
from clipforge.config import get_settings

CLOSING_DAYS = 2.0
MIN_TRACK_HOURS = 6  # fill speed needs screenshots at least this far apart


def key(campaign: str, bounty: str) -> str:
    norm = lambda t: re.sub(r"[^a-z0-9]+", "", t.lower())  # noqa: E731
    return f"{norm(campaign)}|{norm(bounty)}"


FIELDS = ("status", "kind", "rate_usd", "rate_per_views", "pot_usd", "min_views", "filled_pct",
          "clips", "views", "payout", "category", "notes", "marketplace", "campaign_type",
          "min_payout_usd", "max_payout_usd", "bonus_usd", "budget_total_usd",
          "budget_remaining_usd", "end_date", "platforms", "footage_scope")
DEFAULTS = {"status": "unknown", "kind": "rate", "payout": "unknown", "category": "other",
            "notes": "", "marketplace": "", "campaign_type": "", "platforms": "",
            "footage_scope": ""}


def store(conn: sqlite3.Connection, shot_id: int, bounties: list[dict]) -> int:
    """Replace this screenshot's rows (a retry must not duplicate them)."""
    seen_at = conn.execute("SELECT created_at FROM scout_shots WHERE id=?",
                           (shot_id,)).fetchone()["created_at"]
    conn.execute("DELETE FROM scout_bounties WHERE shot_id=?", (shot_id,))
    cols = ("shot_id", "key", "campaign", "bounty", *FIELDS, "seen_at")
    for b in bounties:
        vals = [shot_id, key(b["campaign"], b.get("bounty", "")), b["campaign"],
                b.get("bounty", ""), *[b.get(k, DEFAULTS.get(k)) for k in FIELDS], seen_at]
        conn.execute(f"INSERT INTO scout_bounties ({', '.join(cols)})"
                     f" VALUES ({', '.join('?' * len(cols))})", vals)
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


def terms(b: dict) -> dict:
    """A Scout reading as bounty terms for money.expected."""
    kind = b["campaign_type"] or ("prize_pool" if b["kind"] == "pot" else "cpm")
    t = {"marketplace": b["marketplace"] or "other", "campaign_type": kind,
         "rate_usd": b["rate_usd"] or 0.0, "rate_per_views": b["rate_per_views"] or 1000,
         "min_views": b["min_views"] or 0, "min_payout_usd": b["min_payout_usd"],
         "cap_usd": b["max_payout_usd"], "flat_fee_bonus_usd": b["bonus_usd"],
         "budget_total_usd": b["budget_total_usd"]}
    t["min_views"] = money.min_views_for(t)
    return t


def prefill(b: dict) -> dict:
    """Query parameters for the Add campaign form, from this reading."""
    mk = {"whop": "content_rewards"}.get(b["marketplace"], b["marketplace"])
    out = {"name": b["campaign"], "bounty_name": b["bounty"] or "main",
           "category": b["category"], "marketplace": mk if mk in money.MARKETPLACES else "",
           "campaign_type": b["campaign_type"] if b["campaign_type"] in money.CAMPAIGN_TYPES
           else ("prize_pool" if b["kind"] == "pot" else "cpm"),
           "rate_usd": b["rate_usd"], "rate_mode": b["rate_per_views"],
           "min_payout_usd": b["min_payout_usd"], "min_views": b["min_views"],
           "cap_usd": b["max_payout_usd"], "flat_fee_bonus_usd": b["bonus_usd"],
           "budget_total_usd": b["budget_total_usd"],
           "budget_remaining_usd": b["budget_remaining_usd"], "end_date": b["end_date"],
           "platforms": b["platforms"], "footage_scope": b["footage_scope"],
           "notes": f"from Scout, payout {b['payout']}"}
    return {k: v for k, v in out.items() if v not in (None, "")}


def score(b: dict, odds: list, p90: int | None = None) -> tuple[float, list[str]]:
    """Expected net dollars per clip (money.expected), marked down for what makes it unlikely
    to pay: little budget left, an end date under end_soon_days, a fast fill, crypto payouts.
    Priority categories and sponsor-segment bounties get a nudge up. The score is dollars, so
    "4.10" means about $4.10 per clip posted."""
    s = get_settings()
    why = []
    t = terms(b)
    ev = money.expected(t, odds)
    if ev is None:
        why.append(f"{money.CAMPAIGN_TYPES[t['campaign_type']].lower()}: pay doesn't follow one"
                   " clip's views, judge it by hand")
        return 0.0, why
    full = money.expected(t, odds, capped=False) or 0.0
    if full > 0 and ev < 0.75 * full:
        why.append(f"max payout cuts {100 * (1 - ev / full):.0f}% of a breakout's value")
    if p90 is not None and t["min_views"] > p90:
        why.append(f"minimum {t['min_views']:,} views is above your p90 ({p90:,})")
    mult = 1.0
    left = b["budget_remaining_usd"]
    if left is not None and left < 500:
        mult *= 0.5
        why.append(f"only ${left:,.0f} budget left")
    elif b["filled_pct"] is not None and b["filled_pct"] >= 90:
        mult *= 0.5
        why.append("almost full")
    if b.get("end_days") is not None and b["end_days"] < s.end_soon_days:
        mult *= 0.7
        why.append(f"ends in {max(0, b['end_days']):.0f} days")
    if b["days_left"] is not None and b["days_left"] < CLOSING_DAYS:
        mult *= 0.3
        why.append("closing within 2 days")
    if b["payout"] == "crypto":
        mult *= 0.9
        why.append("crypto payout (wallet + fees)")
    if any(c in b["category"] for c in s.priority_categories):
        mult *= 1.2
    if re.search(r"segment|sponsor", b["footage_scope"] or "", re.I):
        mult *= 1.1
        why.append("sponsor segment: short, focused footage")
    return round(ev * mult, 2), why


def ranked(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Latest reading of every bounty, scored. Returns {'open': [...], 'other': [...],
    'odds': where the view odds came from}."""
    rows = conn.execute("SELECT * FROM scout_bounties ORDER BY seen_at, id").fetchall()
    history: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        history.setdefault(r["key"], []).append(r)
    odds, odds_from = money.odds(conn)
    views = money.account_views(conn)
    p90 = money.percentile(views, 0.9) if len(views) >= get_settings().min_posts_for_odds \
        else None
    today = datetime.now(UTC).date()
    open_, other = [], []
    for hist in history.values():
        b = dict(hist[-1])
        b["readings"] = len(hist)
        b["per_100k"] = (b["rate_usd"] * 100000 / b["rate_per_views"]
                         if b["rate_usd"] is not None and b["rate_per_views"] else None)
        b["per_1k"] = b["per_100k"] / 100 if b["per_100k"] is not None else None
        b["avg_views"] = b["views"] // b["clips"] if b["views"] and b["clips"] else None
        speed = fill_speed(hist)
        b["speed"] = speed
        b["days_left"] = ((100 - b["filled_pct"]) / speed
                          if speed and b["filled_pct"] is not None else None)
        b["end_days"] = ((datetime.strptime(b["end_date"], "%Y-%m-%d").date() - today).days
                         if b["end_date"] else None)
        b["terms"] = terms(b)
        b["prefill"] = prefill(b)
        b["score"], b["why"] = score(b, odds, p90)
        b["ev"] = b["score"]
        b["blocked"] = blocked_reason(CampaignIn(name=b["campaign"], category=b["category"],
                                                 notes=f"{b['bounty']} {b['notes']}"))
        if b["blocked"] or b["status"] in ("ended", "disabled") or \
                (b["end_days"] is not None and b["end_days"] < 0):
            other.append(b)
        else:
            open_.append(b)
    open_.sort(key=lambda b: -b["score"])
    return {"open": open_, "other": other, "odds": odds_from}


def summary(conn: sqlite3.Connection, base_url: str, n: int = 3) -> str:
    r = ranked(conn)
    if not r["open"]:
        return f"🔎 Scout: no open bounties found. {base_url}/scout"
    lines = [f"🔎 **Scout: top {min(n, len(r['open']))} of {len(r['open'])} open bounties**"
             " (expected $ per clip)"]
    for i, b in enumerate(r["open"][:n], 1):
        rate = (f"${b['per_100k']:.0f}/100K" if b["per_100k"] is not None
                else f"${b['pot_usd'] or 0:,.0f} pot")
        left = (f"${b['budget_remaining_usd']:,.0f} left" if b["budget_remaining_usd"] is not None
                else f"{b['filled_pct']:.0f}% filled" if b["filled_pct"] is not None
                else "budget ?")
        lines.append(f"{i}. {b['campaign']} · {b['bounty'] or '—'}: ${b['ev']:.2f}/clip, {rate},"
                     f" {left}, {b['payout']}")
    lines.append(f"{base_url}/scout")
    return "\n".join(lines)
