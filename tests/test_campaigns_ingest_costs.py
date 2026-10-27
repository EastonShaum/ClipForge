import pytest

from clipforge import costs
from clipforge.bounties import default_bounty, get_bounty
from clipforge.campaigns import (
    CampaignIn,
    add_campaign,
    blocked_reason,
    effective_per_1k,
    estimate_payout,
)
from clipforge.captions import build_caption
from clipforge.ingest import add_source, source_key


def test_effective_rate():
    assert effective_per_1k(40, 100_000) == pytest.approx(0.40)
    assert effective_per_1k(1.5, 1000) == pytest.approx(1.5)


def test_blocklist_uses_word_boundaries():
    assert blocked_reason(CampaignIn(name="Stake Slots Promo"))
    assert blocked_reason(CampaignIn(name="Big Win", category="Casino"))
    assert blocked_reason(CampaignIn(name="My biggest mistake podcast")) is None


def test_blocked_campaign_rejected_unless_forced(conn):
    with pytest.raises(ValueError):
        add_campaign(conn, CampaignIn(name="Stake streamer clips"))
    assert add_campaign(conn, CampaignIn(name="Stake streamer clips"), force=True)


def test_payout_threshold_and_cap(conn, campaign_id):
    c = get_bounty(conn, default_bounty(conn, campaign_id))  # $0.40/1K, min 10K, cap $100
    assert estimate_payout(c, 9_999) == 0
    assert estimate_payout(c, 50_000) == pytest.approx(20.0)
    assert estimate_payout(c, 10_000_000) == 100


@pytest.mark.parametrize("url,key", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30", "youtube:dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "youtube:dQw4w9WgXcQ"),
    ("https://m.youtube.com/shorts/dQw4w9WgXcQ", "youtube:dQw4w9WgXcQ"),
    ("https://youtube.com/live/dQw4w9WgXcQ", "youtube:dQw4w9WgXcQ"),
    ("https://drive.google.com/file/d/1AbC_dEf/view?usp=sharing", "gdrive:1AbC_dEf"),
])
def test_source_key(url, key):
    assert source_key(url) == key


def test_add_source_dedupes_and_queues_fetch(conn, campaign_id):
    sid, created = add_source(conn, campaign_id, "https://youtu.be/dQw4w9WgXcQ")
    sid2, created2 = add_source(conn, campaign_id, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert created and not created2 and sid == sid2
    assert conn.execute("SELECT kind FROM jobs").fetchall()[0]["kind"] == "fetch"


def test_budget_cap(conn, settings):
    costs.record(conn, provider="groq", model="x", stage="transcribe", units="", usd=14.5)
    costs.check_budget(conn, 0.4)
    with pytest.raises(costs.BudgetExceeded):
        costs.check_budget(conn, 0.6)
    assert costs.budget_status(conn)["state"] == "warn"


def test_llm_cost_math():
    assert costs.llm_cost("claude-haiku-5-5", 22_000, 4_000) == pytest.approx(0.0042)


def test_caption_has_disclosure_first_and_tags():
    cap = build_caption(title="He said WHAT?", hook="x", category="podcast",
                        required_tags="@pod, #camp", caption_notes="", disclosure="#ad")
    assert cap.startswith("#ad He said WHAT?")
    assert "@pod #camp #podcast #podcastclips" in cap
