"""Scouting: upload marketplace screenshots, read bounties (fake reader), rank them."""

import subprocess

from clipforge import scout
from clipforge.worker import run_local


def _png(tmp_path, color="red"):
    p = tmp_path / f"{color}.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color={color}:s=1170x2532", "-frames:v", "1", str(p)], check=True)
    return p.read_bytes()


def _upload(client, *images):
    return client.post("/scout/upload",
                       files=[("shots", (f"s{i}.png", b, "image/png"))
                              for i, b in enumerate(images)])


def test_upload_read_rank_and_prefill(client, conn, tmp_path, settings, monkeypatch):
    sent = []
    monkeypatch.setattr("clipforge.pipeline.notify.discord", sent.append)
    r = _upload(client, _png(tmp_path), b"not an image")
    assert "Reading 1 screenshot" in r.text and "1 skipped" in r.text
    assert "Nothing new" in _upload(client, _png(tmp_path)).text  # same image again

    shot = conn.execute("SELECT path FROM scout_shots").fetchone()["path"]
    assert (settings.assets_dir / shot).stat().st_size > 0
    run_local(["scout"], once=True, idle_s=0)

    ranked = scout.ranked(conn)
    assert [b["campaign"] for b in ranked["open"]] == ["Man On", "Preme"]
    assert "crypto payout (wallet + fees)" in ranked["open"][1]["why"]
    assert len(sent) == 1 and "1. Man On · episode-4: $125/100K, 30% filled" in sent[0]
    assert conn.execute("SELECT COUNT(*) FROM api_costs WHERE stage='scout'").fetchone()[0] == 1

    page = client.get("/scout").text
    assert "Man On" in page and "Add as bounty" in page
    form = client.get("/campaigns?name=Man+On&rate_usd=125&rate_mode=100000&category=podcast")
    assert 'value="Man On"' in form.text and '<option value="100000" selected' in form.text


def test_fill_speed_and_closing_penalty(conn):
    conn.execute("INSERT INTO scout_shots (id, sha, path, created_at) VALUES"
                 " (1,'a','x','2026-10-25T12:00:00Z'), (2,'b','y','2026-10-27T12:00:00Z')")
    base = {"campaign": "GFuel", "bounty": "gfuel-2", "status": "active", "rate_usd": 125,
            "rate_per_views": 100000, "payout": "paypal", "category": "gaming"}
    scout.store(conn, 1, [{**base, "filled_pct": 60}])
    scout.store(conn, 2, [{**base, "filled_pct": 90}])
    b = scout.ranked(conn)["open"][0]
    assert b["speed"] == 15 and round(b["days_left"], 1) == 0.7
    assert "closing within 2 days" in b["why"] and b["readings"] == 2


def test_gambling_and_ended_bounties_are_set_aside(conn):
    conn.execute("INSERT INTO scout_shots (id, sha, path) VALUES (1,'a','x')")
    scout.store(conn, 1, [
        {"campaign": "Stake", "bounty": "slots", "status": "active", "rate_usd": 300,
         "rate_per_views": 100000},
        {"campaign": "Tubi", "bounty": "old", "status": "ended", "rate_usd": 250,
         "rate_per_views": 100000}])
    r = scout.ranked(conn)
    assert r["open"] == [] and {b["campaign"] for b in r["other"]} == {"Stake", "Tubi"}
