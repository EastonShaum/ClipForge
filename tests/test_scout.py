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
    assert len(sent) == 1 and "1. Man On · episode-4: $" in sent[0]
    assert "/clip, $125/100K, 30% filled, paypal" in sent[0]
    assert conn.execute("SELECT COUNT(*) FROM api_costs WHERE stage='scout'").fetchone()[0] == 1

    page = client.get("/scout").text
    assert "Man On" in page and "Add as bounty" in page
    form = client.get("/campaigns?name=Man+On&rate_usd=125&rate_mode=100000&category=podcast")
    assert 'value="Man On"' in form.text \
        and '<input type="hidden" name="rate_mode" value="100000">' in form.text


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


def test_content_rewards_cards_rank_by_expected_dollars(conn, client):
    conn.execute("INSERT INTO scout_shots (id, sha, path) VALUES (1,'a','x')")
    cr = {"marketplace": "content_rewards", "status": "active", "campaign_type": "cpm",
          "rate_usd": 1.0, "rate_per_views": 1000, "category": "podcast",
          "budget_total_usd": 20000, "budget_remaining_usd": 15000}
    scout.store(conn, 1, [
        {**cr, "campaign": "Pod A", "min_payout_usd": 10, "max_payout_usd": 1000},
        {**cr, "campaign": "Pod B", "min_payout_usd": 1000},  # needs 1M views
        {**cr, "campaign": "Pod C", "min_payout_usd": 10, "max_payout_usd": 1000,
         "budget_remaining_usd": 200},
        {**cr, "campaign": "Pod D", "min_payout_usd": 10, "max_payout_usd": 15},
        {**cr, "campaign": "Kalshi", "category": "prediction market"},
        {**cr, "campaign": "Pool", "campaign_type": "prize_pool"}])
    r = scout.ranked(conn)
    names = [b["campaign"] for b in r["open"]]
    assert names[0] == "Pod A" and names.index("Pod C") > 0  # same terms, little budget
    assert names.index("Pod B") > names.index("Pod A")  # a 1M-view minimum rarely pays
    d = next(b for b in r["open"] if b["campaign"] == "Pod D")
    assert any("max payout cuts" in w for w in d["why"])
    c = next(b for b in r["open"] if b["campaign"] == "Pod C")
    assert "only $200 budget left" in c["why"]
    pool = next(b for b in r["open"] if b["campaign"] == "Pool")
    assert pool["score"] == 0 and "judge it by hand" in pool["why"][0]
    assert [b["campaign"] for b in r["other"]] == ["Kalshi"]
    assert r["odds"].startswith("defaults")
    page = client.get("/scout").text
    assert "per clip" in page and "$15,000.00" in page and "budget left" in page
    a = next(b for b in r["open"] if b["campaign"] == "Pod A")
    assert a["prefill"]["marketplace"] == "content_rewards" and a["prefill"]["cap_usd"] == 1000
    form = client.get("/campaigns?" + "&".join(f"{k}={v}" for k, v in a["prefill"].items()
                                               if k in ("name", "min_payout_usd", "cap_usd")))
    assert 'value="10.0"' in form.text and 'value="1000.0"' in form.text


def test_brief_screenshot_fills_content_rewards_terms(conn, campaign_id):
    import json

    from clipforge import briefs
    bid = conn.execute("SELECT id FROM bounties").fetchone()[0]
    conn.execute("UPDATE bounties SET rate_usd=0, min_views=0, cap_usd=NULL")
    conn.execute("INSERT INTO campaign_docs (campaign_id, bounty_id, path, sha, extracted)"
                 " VALUES (?,?,?,?,?)", (campaign_id, bid, "d.jpg", "s", json.dumps({
                     "campaign_type": "cpm", "rate_per_1k": 1.5, "min_payout_usd": 15,
                     "max_payout_usd": 200, "budget_remaining_usd": 900,
                     "audience_rules": "50%+ US", "footage_scope": "sponsor segment",
                     "ai_video_banned": "yes", "onscreen_ad_label": "no"})))
    changes = briefs.apply(conn, bid)
    b = conn.execute("SELECT * FROM bounties WHERE id=?", (bid,)).fetchone()
    assert b["rate_usd"] == 1.5 and b["rate_per_views"] == 1000 and b["min_views"] == 10_000
    assert b["cap_usd"] == 200 and b["budget_remaining_usd"] == 900 and b["budget_checked_at"]
    assert b["ai_video_banned"] == 1 and b["onscreen_ad_label_required"] == 0
    assert "audience: 50%+ US" in changes and "AI video banned" in changes


def test_a_new_bounty_reads_its_brief_first_then_asks_for_the_rest(client, conn, tmp_path,
                                                                   settings):
    png = _png(tmp_path)
    r = client.post("/campaigns", data={"name": "Pod", "bounty_name": "ep1"},
                    files=[("docs", ("brief.png", png, "image/png"))])
    assert r.url.path == "/campaigns/1/bounties/1/setup" and "Reading the brief" in r.text
    assert 'name="rate_usd"' not in r.text  # no manual fields while it reads
    run_local(["brief"], once=True, idle_s=0)
    page = client.get("/campaigns/1/bounties/1/setup").text
    assert "From the brief" in page and "tags: #manon @manon" in page
    assert "people: Man On" in page and "end date: 2026-11-15" in page
    # Only what the brief didn't give is asked for.
    assert 'name="rate_usd"' in page and 'name="people"' not in page
    assert 'name="end_date"' not in page
    r = client.post("/campaigns/1/bounties/1/setup",
                    data={"rate_usd": "1.5", "rate_mode": "1000", "campaign_type": "cpm",
                          "min_payout_usd": "", "audience_rules": ""})
    b = conn.execute("SELECT * FROM bounties WHERE id=1").fetchone()
    assert b["rate_usd"] == 1.5 and b["people"] == "Man On" and b["end_date"] == "2026-11-15"
    assert b["audience_rules"] == "" and "is set up" in r.text

    # A pasted brief is read too, stored as a text file next to the screenshots.
    r = client.post("/campaigns/1/bounties", data={"name": "ep2", "brief_text": "Clip ep2."})
    assert r.url.path == "/campaigns/1/bounties/2/setup"
    doc = conn.execute("SELECT path FROM campaign_docs WHERE bounty_id=2").fetchone()
    assert (settings.assets_dir / doc["path"]).read_text() == "Clip ep2."
    # Editing the brief in settings replaces that text and reads it again.
    client.post("/campaigns/1/bounties/2/settings", data={"brief_text": "Clip ep2, new."})
    assert conn.execute("SELECT COUNT(*) FROM campaign_docs WHERE bounty_id=2").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='brief'").fetchone()[0] == 2
    # No brief at all: every term is asked for.
    r = client.post("/campaigns/1/bounties", data={"name": "ep3"})
    assert "fill in the terms by hand" in r.text and 'name="end_date"' in r.text
    assert 'name="docs"' in client.get("/campaigns").text


def test_a_brief_sets_length_all_names_and_watermark(conn, campaign_id):
    import json

    from clipforge import briefs
    bid = conn.execute("SELECT id FROM bounties").fetchone()[0]
    conn.execute("INSERT INTO campaign_docs (campaign_id, bounty_id, path, sha, extracted)"
                 " VALUES (?,?,?,?,?)", (campaign_id, bid, "d.txt", "s", json.dumps({
                     "min_seconds": 20, "max_seconds": 90, "must_mention": ["John Morgan",
                                                                            "Raw Law Media"],
                     "mention_all": "yes", "logo_rule": "The Raw Law Media logo with"
                     " @rawlawmedia as a visible watermark, at about 50-60% opacity."})))
    changes = briefs.apply(conn, bid)
    b = conn.execute("SELECT * FROM bounties WHERE id=?", (bid,)).fetchone()
    assert (b["min_clip_s"], b["max_clip_s"], b["mention_all"]) == (20, 90, 1)
    c = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    assert (c["logo_style"], c["logo_handle"], c["logo_opacity"]) == ("watermark",
                                                                     "@rawlawmedia", 0.6)
    assert "logos as a watermark with @rawlawmedia" in changes
