import pytest

from clipforge import config
from clipforge.db import connect, init_db


@pytest.fixture(autouse=True)
def settings(tmp_path, monkeypatch):
    """Every test gets its own data dir, DB, and offline providers."""
    monkeypatch.setenv("CF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CF_TRANSCRIBE_PROVIDER", "fake")
    monkeypatch.setenv("CF_SELECT_PROVIDER", "fake")
    monkeypatch.setenv("CF_DISCORD_WEBHOOK_URL", "")
    monkeypatch.setenv("CF_TIKTOK_CLIENT_KEY", "")  # never the real app's keys from .env
    monkeypatch.setenv("CF_TIKTOK_CLIENT_SECRET", "")
    monkeypatch.setenv("CF_WORKER_TOKEN", "test-token")
    monkeypatch.setenv("CF_MIN_CLIP_S", "8")
    monkeypatch.setenv("CF_MAX_CLIP_S", "15")
    monkeypatch.setenv("CF_CANDIDATES_KEEP", "2")
    monkeypatch.setenv("CF_RENDER_PRESET", "ultrafast")
    monkeypatch.setenv("CF_HOOK_CARD", "true")  # keep the card's render path under test
    monkeypatch.setenv("CF_REQUIRE_WORKER_VERSION", "false")  # test_update.py turns it on
    config.get_settings.cache_clear()
    s = config.get_settings()
    yield s
    config.get_settings.cache_clear()


@pytest.fixture
def conn(settings):
    init_db()
    c = connect()
    from clipforge.select_prompts import sync_prompts

    sync_prompts(c)
    yield c
    c.close()


@pytest.fixture
def campaign_id(conn):
    """A campaign with one bounty ("main"), like most tests need."""
    from clipforge.bounties import BountyIn, add_bounty
    from clipforge.campaigns import CampaignIn, add_campaign

    cid = add_campaign(conn, CampaignIn(name="Test Pod", category="podcast",
                                        marketplace="clipping_net",
                                        required_tags="@testpod #testcampaign"))
    add_bounty(conn, cid, BountyIn(name="main", rate_usd=40, rate_per_views=100_000,
                                   min_views=10_000, cap_usd=100))
    return cid


@pytest.fixture
def client(conn):
    from fastapi.testclient import TestClient

    from clipforge.web.app import create_app
    return TestClient(create_app())
