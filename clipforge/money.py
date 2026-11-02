"""What a clip pays (D38): payout rules per bounty, marketplace fees, view odds and the expected
dollars per clip that Scout ranks by.

A bounty dict here needs: campaign_type, rate_usd, rate_per_views, min_views, cap_usd,
flat_fee_bonus_usd, and for the fee: marketplace, budget_total_usd. min_payout_usd, when set,
decides min_views (min_views_for)."""

import bisect
import math
import sqlite3

from clipforge.config import get_settings

MARKETPLACES = {"content_rewards": "Content Rewards", "clipping_net": "Clipping.net",
                "other": "Other"}
CAMPAIGN_TYPES = {"cpm": "Per 1K views", "per_post": "Per post", "retainer": "Monthly retainer",
                  "prize_pool": "Prize pool"}
PAYER_TYPES = {"streamer": "The streamer", "sponsor": "A sponsor", "network": "A network",
               "none": "Nobody (don't record)"}


def per_1k(b: dict) -> float:
    rpv = b.get("rate_per_views") or 0
    return (b.get("rate_usd") or 0) * 1000 / rpv if rpv else 0.0


def min_views_for(b: dict) -> int:
    """Content Rewards states a minimum payout in dollars: the views that earn it."""
    rate = per_1k(b)
    if b.get("min_payout_usd") and rate and (b.get("campaign_type") or "cpm") == "cpm":
        return math.ceil(b["min_payout_usd"] * 1000 / rate)
    return int(b.get("min_views") or 0)


def cap_views(b: dict) -> int | None:
    """Views past which a clip earns nothing more (its max payout)."""
    rate = per_1k(b)
    return int(b["cap_usd"] * 1000 / rate) if b.get("cap_usd") and rate else None


def fee_rate(b: dict) -> float:
    """The marketplace's cut of a payout. Only Content Rewards publishes one."""
    if b.get("marketplace") != "content_rewards":
        return 0.0
    s = get_settings()
    if (b.get("campaign_type") or "cpm") in ("per_post", "retainer") \
            and (b.get("budget_total_usd") or 0) >= s.cr_fee_free_budget_usd:
        return 0.0
    return s.cr_fee


def payout(b: dict, views: int, *, net: bool = True) -> float:
    """One clip's pay: 0 below the minimum; rate × views / 1000 up to the max payout per clip;
    plus the flat-fee bonus. Per-post pays its rate once the clip qualifies. Retainers and
    prize pools don't pay per clip (0). After the marketplace fee unless net=False."""
    kind = b.get("campaign_type") or "cpm"
    if kind in ("retainer", "prize_pool") or views < min_views_for(b):
        return 0.0
    if kind == "per_post":
        pay = b.get("rate_usd") or 0.0
    else:
        pay = views / 1000 * per_1k(b)
        if b.get("cap_usd"):
            pay = min(pay, b["cap_usd"])
    pay += b.get("flat_fee_bonus_usd") or 0.0
    return pay * (1 - fee_rate(b)) if net else pay


# ---------- view odds ----------

def default_odds() -> list[tuple[int, float]]:
    return sorted(get_settings().view_odds.items())


def odds_from_views(views: list[int]) -> list[tuple[int, float]]:
    """An account's own odds from its posts' views: the share at or above each percentile."""
    v = sorted(views)
    n = len(v)
    xs = {max(1, v[min(n - 1, int(q * n))]) for q in (0.5, 0.75, 0.9, 0.97, 0.99)}
    return [(x, (n - bisect.bisect_left(v, x)) / n) for x in sorted(xs)]


def chance(odds: list[tuple[int, float]], views: float) -> float:
    """P(a post reaches at least `views`), straight lines between the points on log-log axes,
    1.0 at 1 view, and the last stretch's slope beyond the last point."""
    pts = [(1, 1.0)] + [(x, p) for x, p in odds if x > 1 and p > 0]
    if views <= 1:
        return 1.0
    xs = [x for x, _ in pts]
    i = bisect.bisect_right(xs, views) - 1
    (x0, p0), (x1, p1) = (pts[i], pts[i + 1]) if i + 1 < len(pts) else (pts[-2], pts[-1])
    if x0 == x1:
        return p0
    slope = (math.log(p1) - math.log(p0)) / (math.log(x1) - math.log(x0))
    return min(1.0, math.exp(math.log(p0) + slope * (math.log(views) - math.log(x0))))


_GRID = [10 ** (k / 20) for k in range(0, 8 * 20 + 1)]  # 1 view .. 100M, 20 steps a decade


def _grid() -> list[float]:
    top = get_settings().max_views
    return [v for v in _GRID if v < top] + [top]


def expected(b: dict, odds: list[tuple[int, float]], *, capped: bool = True) -> float | None:
    """Expected net dollars per clip: payout(views) weighted by the odds of those views.
    None for retainers and prize pools (pay doesn't follow one clip's views)."""
    kind = b.get("campaign_type") or "cpm"
    if kind in ("retainer", "prize_pool"):
        return None
    if kind == "per_post":
        return payout(b, max(min_views_for(b), 1)) * chance(odds, max(min_views_for(b), 1))
    bb = b if capped else {**b, "cap_usd": None}
    total = 0.0
    grid = _grid()
    for lo, hi in zip(grid, grid[1:], strict=False):
        total += payout(bb, int(lo)) * (chance(odds, lo) - chance(odds, hi))
    total += payout(bb, int(grid[-1])) * chance(odds, grid[-1])  # everything at the top
    return total


def account_views(conn: sqlite3.Connection, account_id: int | None = None) -> list[int]:
    """Each post's best view count, for one account or all."""
    return [r[0] for r in conn.execute(
        "SELECT COALESCE(MAX(v.views), 0) FROM posts p LEFT JOIN view_snapshots v"
        " ON v.post_id=p.id WHERE ? IS NULL OR p.account_id=? GROUP BY p.id",
        (account_id, account_id))]


def odds(conn: sqlite3.Connection, account_id: int | None = None) -> tuple[list, str]:
    """The odds to rank with and where they came from: the account's (or all accounts') own
    posts once there are enough, the defaults until then."""
    views = account_views(conn, account_id)
    if len(views) >= get_settings().min_posts_for_odds:
        return odds_from_views(views), f"your last {len(views)} posts"
    return default_odds(), f"defaults (until {get_settings().min_posts_for_odds} posts)"


def percentile(views: list[int], q: float) -> int | None:
    if not views:
        return None
    v = sorted(views)
    return v[min(len(v) - 1, int(q * len(v)))]
