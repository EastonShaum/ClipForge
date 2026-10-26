"""End to end: campaign → source (local fixture) → fetch → transcribe → select → render →
review → approve → log post → views → dashboard. Offline: fake transcriber and selector."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipforge.web.app import create_app
from clipforge.worker import run_local

FIXTURE = Path(__file__).parent / "fixtures" / "sample.mp4"


@pytest.fixture
def client(conn):
    return TestClient(create_app())


def test_full_loop(client, conn, settings):
    r = client.post("/campaigns", data={"name": "E2E Pod", "category": "podcast",
                                        "rate_usd": "40", "rate_mode": "100000",
                                        "min_views": "1000", "required_tags": "@e2e"})
    assert r.status_code == 200 and "Campaign #1 added" in r.text

    r = client.post("/sources", data={"campaign_id": "1", "urls": FIXTURE.resolve().as_uri()})
    assert "Queued 1 source" in r.text

    run_local(["fetch", "transcribe", "select", "render"], once=True, idle_s=0)

    src = conn.execute("SELECT * FROM sources").fetchone()
    assert src["status"] == "rendered", src["error"]
    clips = conn.execute("SELECT * FROM clips").fetchall()
    assert len(clips) == settings.candidates_keep
    for c in clips:
        assert (settings.clips_dir / c["path"]).stat().st_size > 10_000
        assert c["caption"].startswith("#ad ")

    page = client.get("/review").text
    assert "/media/clips/" in page and "Approve" in page
    assert client.get(f"/media/clips/{clips[0]['path']}").status_code == 200

    client.post(f"/clips/{clips[0]['id']}/review", data={"decision": "approved"})
    client.post(f"/clips/{clips[1]['id']}/review", data={"decision": "rejected",
                                                         "reason": "weak_hook"})
    assert "Copy caption" in client.get("/approved").text

    r = client.post(f"/clips/{clips[0]['id']}/post",
                    data={"platform": "tiktok", "url": "https://www.tiktok.com/@me/video/1"})
    assert "Logged tiktok post" in r.text
    client.post("/posts/1/views", data={"views": "25000"})

    dash = client.get("/dashboard").text
    assert "$10.00" in dash  # 25K views × $0.40/1K
    assert "50%" in dash  # 1 approved, 1 rejected

    from clipforge.feedback import build_feedback
    fb = build_feedback(conn)
    assert "LIKED" in fb and "REJECTED" in fb and "25,000 views" in fb

    health = client.get("/health").json()
    assert health["ok"] and health["jobs"]["render"]["done"] == settings.candidates_keep


def test_rerun_is_idempotent(client, conn):
    client.post("/campaigns", data={"name": "Idem", "rate_usd": "1"})
    client.post("/sources", data={"campaign_id": "1", "urls": FIXTURE.resolve().as_uri()})
    run_local(["fetch", "transcribe", "select", "render"], once=True, idle_s=0)
    n = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    # Re-queue everything as if every job crashed after finishing; nothing should duplicate.
    conn.execute("UPDATE jobs SET state='queued', run_after='2000-01-01T00:00:00Z'")
    run_local(["fetch", "transcribe", "select", "render"], once=True, idle_s=0)
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == n
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1


def test_budget_pause_defers_paid_jobs(client, conn, monkeypatch, settings):
    from clipforge import config, costs

    monkeypatch.setenv("CF_TRANSCRIBE_PROVIDER", "groq")
    config.get_settings.cache_clear()
    client.post("/campaigns", data={"name": "Budget", "rate_usd": "1"})
    client.post("/sources", data={"campaign_id": "1", "urls": FIXTURE.resolve().as_uri()})
    costs.record(conn, provider="groq", model="x", stage="transcribe", units="", usd=100)
    run_local(["fetch", "transcribe"], once=True, idle_s=0)
    job = conn.execute("SELECT * FROM jobs WHERE kind='transcribe'").fetchone()
    assert job["state"] == "queued" and "budget" in job["last_error"]


def test_worker_api_requires_token_and_round_trips(client, conn):
    assert client.post("/api/worker/claim", json={"worker": "l", "host": "laptop",
                                                  "kinds": ["fetch"]}).status_code == 401
    client.post("/campaigns", data={"name": "Remote", "rate_usd": "1"})
    client.post("/sources", data={"campaign_id": "1", "urls": FIXTURE.resolve().as_uri()})
    h = {"Authorization": "Bearer test-token"}
    r = client.post("/api/worker/claim", headers=h,
                    json={"worker": "laptop-remote-1", "host": "laptop", "kinds": ["fetch"]})
    assert r.status_code == 200
    job = r.json()["job"]
    r = client.post(f"/api/worker/jobs/{job['id']}/result", headers=h,
                    data={"result": '{"media_path": "C:/clips/raw/1/media.mp4", '
                                    '"duration_s": 40, "title": "Remote title"}'})
    assert r.status_code == 200
    src = conn.execute("SELECT * FROM sources").fetchone()
    assert src["media_host"] == "laptop" and src["title"] == "Remote title"
    t = conn.execute("SELECT * FROM jobs WHERE kind='transcribe'").fetchone()
    assert t["affinity_host"] == "laptop"  # media lives on the laptop now
