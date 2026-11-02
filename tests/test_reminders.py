"""Posting reminders: slot pings, ending-soon digest, nagging for unsubmitted post links."""

from datetime import UTC, datetime

import pytest

from clipforge import reminders
from clipforge.worker import run_local
from tests.test_e2e import FIXTURE

# 2026-10-27 17:05 in Denver (MDT, UTC-6).
SLOT_TIME = datetime(2026, 10, 27, 23, 5, tzinfo=UTC)
DIGEST_TIME = datetime(2026, 10, 27, 15, 1, tzinfo=UTC)


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(reminders.notify, "discord", out.append)
    return out


@pytest.fixture
def clips(client, conn):
    client.post("/campaigns", data={"name": "Man On", "rate_usd": "1",
                                    "allowed_platforms": "tiktok,youtube"})
    client.post("/sources", data={"campaign_id": "1", "urls": FIXTURE.resolve().as_uri()})
    run_local(["fetch", "transcribe", "select", "render"], once=True, idle_s=0)
    ids = [r[0] for r in conn.execute("SELECT id FROM clips ORDER BY id")]
    return ids


def test_slot_points_at_best_clip_and_remaining_platforms(client, conn, clips, sent):
    client.post(f"/clips/{clips[0]}/review", data={"decision": "approved"})
    client.post(f"/clips/{clips[0]}/post", data={"platform": "tiktok",
                                                 "url": "https://www.tiktok.com/@g/video/1"})
    reminders.tick(conn, SLOT_TIME)
    assert len(sent) == 1
    assert "Time to post" in sent[0] and "→ youtube" in sent[0] and f"#clip{clips[0]}" in sent[0]
    reminders.tick(conn, SLOT_TIME)  # same slot again: no repeat
    assert len(sent) == 1


def test_slot_with_nothing_approved_points_to_review(conn, clips, sent):
    reminders.tick(conn, SLOT_TIME)
    assert "nothing is approved" in sent[0] and "/review" in sent[0]


def test_missed_slot_is_not_sent_late(conn, clips, sent):
    reminders.tick(conn, datetime(2026, 10, 28, 3, 30, tzinfo=UTC))  # 21:30 local
    assert sent == []


def test_digest_warns_about_ending_campaign_and_nags_until_submitted(client, conn, clips, sent):
    client.post("/campaigns/1/bounties/1/settings", data={"rate_usd": "1", "end_date": "2026-10-28"})
    client.post(f"/clips/{clips[0]}/review", data={"decision": "approved"})
    client.post(f"/clips/{clips[0]}/post", data={"platform": "tiktok",
                                                 "url": "https://www.tiktok.com/@g/video/1"})
    conn.execute("UPDATE posts SET posted_at='2026-10-27T01:00:00Z'")
    reminders.tick(conn, DIGEST_TIME)
    assert "Man On** ends tomorrow" in sent[0]
    assert "not submitted" in sent[0] and "video/1" in sent[0]

    client.post("/posts/1/submitted")
    assert "waiting for review" in client.get("/posts").text
    assert reminders.unsubmitted(conn, DIGEST_TIME) == []
