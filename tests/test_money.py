"""Content Rewards money terms (D38): marketplace, minimum payout in dollars, max payout, flat
bonus, the marketplace fee, view odds and expected dollars per clip."""

import pytest

from clipforge import money
from clipforge.bounties import get_bounty


def cr(**kw):
    return {"marketplace": "content_rewards", "campaign_type": "cpm", "rate_usd": 1.0,
            "rate_per_views": 1000, "min_views": 0, **kw}


def test_payout_follows_the_campaign_terms():
    b = cr(min_payout_usd=10, cap_usd=500, flat_fee_bonus_usd=5)
    assert money.min_views_for(b) == 10_000 and money.cap_views(b) == 500_000
    b["min_views"] = money.min_views_for(b)
    assert money.payout(b, 9_999) == 0
    assert money.payout(b, 50_000, net=False) == pytest.approx(55.0)  # $50 + $5 bonus
    assert money.payout(b, 50_000) == pytest.approx(49.5)  # 10% Content Rewards fee
    assert money.payout(b, 5_000_000, net=False) == pytest.approx(505.0)  # capped at $500
    post = cr(campaign_type="per_post", rate_usd=25, budget_total_usd=8000)
    assert money.payout(post, 1) == 25  # $5,000+ per-post budgets pay no fee
    assert money.payout(cr(campaign_type="retainer", rate_usd=500), 10**6) == 0
    assert money.payout({**b, "marketplace": "clipping_net"}, 50_000) == pytest.approx(55.0)


def test_expected_value_uses_the_odds_and_the_cap():
    odds = money.default_odds()
    assert money.chance(odds, 500) == pytest.approx(0.5)
    assert money.chance(odds, 100_000) == pytest.approx(0.03)
    assert money.chance(odds, 10_000) > money.chance(odds, 20_000) > money.chance(odds, 100_000)
    b = cr(min_payout_usd=10, cap_usd=50)
    b["min_views"] = money.min_views_for(b)
    capped, free = money.expected(b, odds), money.expected(b, odds, capped=False)
    assert 0 < capped < free
    high_min = cr(min_views=1_000_000, cap_usd=50)
    assert money.expected(high_min, odds) < capped  # a high minimum rarely pays
    assert money.expected(cr(campaign_type="prize_pool"), odds) is None
    # An account's own posts replace the defaults: half of them reach 2,000, a tenth 20,000.
    mine = money.odds_from_views([300] * 50 + [2_000] * 40 + [20_000] * 10)
    assert money.chance(mine, 2_000) == pytest.approx(0.5)
    assert money.chance(mine, 20_000) == pytest.approx(0.1)
    assert money.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == 10


def test_a_content_rewards_campaign_from_the_web_form(client, conn):
    r = client.post("/campaigns", data={
        "name": "Startup Pod", "category": "startup", "bounty_name": "launch",
        "campaign_type": "cpm", "rate_usd": "1.50", "rate_mode": "1000",
        "min_payout_usd": "15", "cap_usd": "300", "budget_total_usd": "10000",
        "budget_remaining_usd": "1500", "brief_url": "https://contentrewards.com/c/abc",
        "audience_rules": "50%+ US", "onscreen_ad_label_required": "true",
        "pf_tiktok": "1", "pf_x": "1"})
    assert "Campaign #1 added" in r.text
    c = conn.execute("SELECT * FROM campaigns").fetchone()
    assert c["marketplace"] == "content_rewards" and c["allowed_platforms"] == "tiktok,x"
    b = get_bounty(conn, 1)
    assert b["min_views"] == 10_000 and b["budget_checked_at"]  # $15 at $1.50/1K
    assert b["onscreen_ad_label_required"] == 1 and b["audience_rules"] == "50%+ US"
    page = client.get("/campaigns/1/bounties/1").text
    assert "$1,500.00" in page and "budget left" in page and "Content Rewards" in page
    assert "Audience: 50%+ US" in page
    assert "Content Rewards" in client.get("/campaigns?m=content_rewards").text
    assert "Startup Pod" not in client.get("/campaigns?m=clipping_net").text


def test_political_needs_the_sponsor_terms_and_prediction_markets_are_blocked(client, conn):
    r = client.post("/campaigns", data={"name": "Vote Clips", "category": "political",
                                        "rate_usd": "1"})
    assert "names the sponsor" in r.text
    assert not conn.execute("SELECT 1 FROM campaigns").fetchone()
    r = client.post("/campaigns", data={"name": "Vote Clips", "category": "political",
                                        "rate_usd": "1", "force": "on"})
    assert "Campaign #1 added" in r.text
    r = client.post("/campaigns", data={"name": "Kalshi Picks", "rate_usd": "1"})
    assert "blocked keyword: kalshi" in r.text


def test_dollars_per_hour_percentiles_and_payment_delay(client, conn, campaign_id):
    from clipforge.analytics import money_stats
    from tests.test_posting import _clip
    client.post("/accounts", data={"platform": "tiktok", "handle": "@ez"})
    acct = conn.execute("SELECT id FROM accounts").fetchone()[0]
    bid = conn.execute("SELECT id FROM bounties").fetchone()[0]
    r = client.post(f"/campaigns/{campaign_id}/bounties/{bid}/time",
                    data={"minutes": "90", "note": "posting"})
    assert "Logged 90 min" in r.text
    for i, views in enumerate([100, 200, 300, 400, 500, 600, 700, 800, 900, 50_000]):
        k, _, _ = _clip(conn, campaign_id, start=i * 60_000, end=i * 60_000 + 20_000)
        client.post(f"/clips/{k}/post", data={"account_id": str(acct),
                                               "url": f"https://t.co/{i}"})
        pid = conn.execute("SELECT MAX(id) FROM posts").fetchone()[0]
        client.post(f"/posts/{pid}/views", data={"views": str(views)})
    pid = conn.execute("SELECT MAX(id) FROM posts").fetchone()[0]
    conn.execute("UPDATE posts SET submitted_at='2026-11-01T00:00:00Z', review_status='paid',"
                 " earned_usd=60, paid_at='2026-11-04T12:00:00Z' WHERE id=?", (pid,))
    m = money_stats(conn)
    assert m["paid_total"] == 60 and m["minutes_total"] == 90
    assert m["per_bounty"][0]["per_hour"] == pytest.approx(40.0)  # $60 in 1.5 h, under $50
    a = m["accounts"][0]
    assert a["posts"] == 10 and a["p50"] == 600 and a["p90"] == 50_000
    assert a["above_p90"] == []  # min 10K is under this account's p90
    conn.execute("UPDATE bounties SET min_views=100000")
    assert money_stats(conn)["accounts"][0]["above_p90"] == ["Test Pod"]
    assert m["pay_days"][0]["avg"] == pytest.approx(3.5)
    page = client.get("/dashboard").text
    assert "Paid per hour" in page and "$40.00" in page and "3.5 days from submitting" in page
    assert "Minimum views above this account" in page
