"""Dashboard numbers. Views use the highest snapshot per post (view counts only go up)."""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from clipforge import money
from clipforge.bounties import label
from clipforge.campaigns import effective_per_1k, estimate_payout
from clipforge.config import get_settings

POST_VIEWS = """
    SELECT p.id post_id, p.platform, p.clip_id, p.account_id, p.review_status, p.earned_usd,
           p.submitted_at, p.approved_at, p.paid_at, c.id cand_id, c.hook_type, c.prompt_version_id,
           s.campaign_id, s.bounty_id, COALESCE(MAX(v.views), 0) views
    FROM posts p JOIN clips k ON k.id=p.clip_id JOIN candidates c ON c.id=k.candidate_id
    JOIN sources s ON s.id=c.source_id LEFT JOIN view_snapshots v ON v.post_id=p.id
    GROUP BY p.id
"""


def _bounties(conn: sqlite3.Connection) -> dict[int, dict]:
    out = {}
    for r in conn.execute("SELECT b.*, c.name campaign, c.marketplace FROM bounties b"
                          " JOIN campaigns c ON c.id=b.campaign_id"):
        d = dict(r)
        d["per_1k"] = effective_per_1k(d["rate_usd"], d["rate_per_views"])
        d["label"] = label(d["campaign"], d["name"])
        out[d["id"]] = d
    return out


def post_rows(conn: sqlite3.Connection) -> list[dict]:
    bounties = _bounties(conn)
    rows = []
    for r in conn.execute(POST_VIEWS):
        d = dict(r)
        b = bounties.get(d["bounty_id"])
        # What landed once it's paid, nothing once rejected, the estimate until then.
        d["earn"] = (d["earned_usd"] if d["earned_usd"] is not None
                     else estimate_payout(b, d["views"]) if b else 0.0)
        rows.append(d)
    return rows


def _review_stats(conn, group_sql: str) -> dict:
    out = {}
    for r in conn.execute(
        f"""SELECT {group_sql} g,
                   SUM(k.status IN ('approved','posted')) approved,
                   SUM(k.status = 'rejected') rejected,
                   COUNT(k.id) clips
            FROM clips k JOIN candidates c ON c.id=k.candidate_id
            JOIN sources s ON s.id=c.source_id
            LEFT JOIN prompt_versions pv ON pv.id=c.prompt_version_id
            GROUP BY g"""
    ):
        decided = (r["approved"] or 0) + (r["rejected"] or 0)
        out[r["g"]] = {"clips": r["clips"], "approved": r["approved"] or 0,
                       "rejected": r["rejected"] or 0,
                       "approval_rate": (r["approved"] or 0) / decided if decided else None}
    return out


def _merge_views(stats: dict, posts: list[dict], key) -> list[dict]:
    for p in posts:
        g = stats.setdefault(key(p), {"clips": 0, "approved": 0, "rejected": 0,
                                      "approval_rate": None})
        g["posts"] = g.get("posts", 0) + 1
        g["views"] = g.get("views", 0) + p["views"]
        g["earn"] = g.get("earn", 0.0) + p["earn"]
    rows = []
    for name, g in stats.items():
        g.setdefault("posts", 0)
        g.setdefault("views", 0)
        g.setdefault("earn", 0.0)
        g["views_per_post"] = g["views"] / g["posts"] if g["posts"] else None
        rows.append({"name": name, **g})
    return sorted(rows, key=lambda r: (-(r["views"] or 0), str(r["name"])))


def dashboard(conn: sqlite3.Connection) -> dict:
    posts = post_rows(conn)
    pv_names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM prompt_versions")}
    bounties = _bounties(conn)

    # One row per bounty: each has its own rate and view threshold.
    by_campaign = _merge_views(_review_stats(conn, "s.bounty_id"), posts,
                               lambda p: p["bounty_id"])
    for r in by_campaign:
        c = bounties.get(r["name"])
        best = max((p["views"] for p in posts if p["bounty_id"] == r["name"]), default=0)
        r["label"] = c["label"] if c else r["name"]
        r["per_1k"] = c["per_1k"] if c else 0
        r["min_views"] = c["min_views"] if c else 0
        r["best_views"] = best
        r["threshold_progress"] = (min(1.0, best / c["min_views"]) if c and c["min_views"]
                                   else None)

    by_hook = _merge_views(_review_stats(conn, "c.hook_type"), posts, lambda p: p["hook_type"])
    by_prompt = _merge_views(_review_stats(conn, "pv.name"), posts,
                             lambda p: pv_names.get(p["prompt_version_id"]))
    by_platform = _merge_views({}, posts, lambda p: p["platform"])

    tot = conn.execute(
        "SELECT SUM(status IN ('approved','posted')) a, SUM(status='rejected') r,"
        " SUM(status='in_review') q, COUNT(*) n FROM clips").fetchone()
    decided = (tot["a"] or 0) + (tot["r"] or 0)
    spend = conn.execute(
        "SELECT stage, provider, SUM(usd) usd, SUM(usd_equiv) eq, COUNT(*) calls FROM api_costs"
        " WHERE created_at >= strftime('%Y-%m-01T00:00:00Z','now') GROUP BY stage, provider"
    ).fetchall()
    daily = conn.execute(
        "SELECT substr(created_at,1,10) day, SUM(usd) usd, SUM(usd_equiv) eq FROM api_costs"
        " GROUP BY day ORDER BY day DESC LIMIT 14").fetchall()
    return {
        "totals": {
            "clips": tot["n"] or 0, "in_review": tot["q"] or 0, "approved": tot["a"] or 0,
            "rejected": tot["r"] or 0,
            "approval_rate": (tot["a"] or 0) / decided if decided else None,
            "posts": len(posts), "views": sum(p["views"] for p in posts),
            "earn": sum(p["earn"] for p in posts),
        },
        "by_campaign": by_campaign,
        "by_hook": by_hook,
        "by_prompt": by_prompt,
        "by_platform": by_platform,
        "spend": [dict(r) for r in spend],
        "daily_spend": [dict(r) for r in daily],
    }


def _days_between(a: str, b: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds() / 86400


def money_stats(conn: sqlite3.Connection) -> dict:
    """D38: what an hour of Easton's time earns (paid dollars ÷ logged minutes) per bounty and
    per week against his freelance rate; each account's p50/p90 views and the bounties whose
    minimum is above an account's p90; days from submission to payment per marketplace."""
    s = get_settings()
    bounties = _bounties(conn)
    posts = post_rows(conn)
    paid = [p for p in posts if p["review_status"] == "paid"]
    minutes: dict[int, int] = {r[0]: r[1] for r in conn.execute(
        "SELECT bounty_id, SUM(minutes) FROM work_log GROUP BY bounty_id")}
    per_bounty = []
    for bid in set(minutes) | {p["bounty_id"] for p in paid}:
        b = bounties.get(bid)
        got = sum(p["earned_usd"] or 0 for p in paid if p["bounty_id"] == bid)
        est = sum(p["earn"] for p in posts if p["bounty_id"] == bid)
        m = minutes.get(bid, 0)
        per_bounty.append({"label": b["label"] if b else bid, "minutes": m, "paid": got,
                           "est": est, "per_hour": got * 60 / m if m else None,
                           "est_per_hour": est * 60 / m if m else None})
    per_bounty.sort(key=lambda r: -(r["minutes"]))

    # Weeks start Monday, local time. Paid dollars count in the week they landed.
    tz = ZoneInfo(s.timezone)

    def week(iso: str) -> str:
        d = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).astimezone(tz)
        return (d.date() - timedelta(days=d.weekday())).isoformat()

    weeks: dict[str, dict] = {}
    for r in conn.execute("SELECT day, minutes FROM work_log"):
        wk = (date.fromisoformat(r["day"]) - timedelta(days=date.fromisoformat(r["day"])
                                                       .weekday())).isoformat()
        weeks.setdefault(wk, {"week": wk, "minutes": 0, "paid": 0.0})["minutes"] += r["minutes"]
    for p in paid:
        wk = week(p["paid_at"])
        weeks.setdefault(wk, {"week": wk, "minutes": 0, "paid": 0.0})["paid"] += \
            p["earned_usd"] or 0
    per_week = sorted(weeks.values(), key=lambda w: w["week"], reverse=True)[:8]
    for w in per_week:
        w["per_hour"] = w["paid"] * 60 / w["minutes"] if w["minutes"] else None

    accts = []
    for a in conn.execute("SELECT * FROM accounts ORDER BY platform, handle"):
        views = money.account_views(conn, a["id"])
        p90 = money.percentile(views, 0.9)
        above = [b["label"] for b in bounties.values() if b["status"] == "active"
                 and not b["live_session_id"] and p90 is not None and len(views) >= 10
                 and money.min_views_for(b) > p90]
        accts.append({**dict(a), "posts": len(views), "p50": money.percentile(views, 0.5),
                      "p90": p90, "above_p90": above})

    pay_days: dict[str, list[float]] = {}
    for p in paid:
        if p["submitted_at"] and p["paid_at"]:
            b = bounties.get(p["bounty_id"])
            mk = (b or {}).get("marketplace") or "other"
            pay_days.setdefault(mk, []).append(_days_between(p["submitted_at"], p["paid_at"]))
    return {"target": s.target_hourly_usd, "per_bounty": per_bounty, "per_week": per_week,
            "accounts": accts,
            "pay_days": [{"marketplace": money.MARKETPLACES.get(k, k), "n": len(v),
                          "avg": sum(v) / len(v), "max": max(v)} for k, v in pay_days.items()],
            "paid_total": sum(p["earned_usd"] or 0 for p in paid),
            "minutes_total": sum(minutes.values())}
