"""Dashboard numbers. Views use the highest snapshot per post (view counts only go up)."""

import sqlite3

from clipforge.bounties import label
from clipforge.campaigns import effective_per_1k, estimate_payout

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
        d["earn"] = estimate_payout(b, d["views"]) if b else 0.0
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
