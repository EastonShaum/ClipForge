"""Requirement screenshots: upload to a campaign, read (fake reader), apply to the campaign."""

import subprocess

from clipforge.worker import run_local


def _png(tmp_path):
    p = tmp_path / "brief.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=white:s=1200x1600",
                    "-frames:v", "1", str(p)], check=True)
    return p.read_bytes()


def test_brief_upload_read_and_apply(client, conn, tmp_path):
    client.post("/campaigns", data={"name": "Man On", "bounty_name": "episode-4",
                                    "rate_usd": "125", "rate_mode": "100000",
                                    "required_tags": "#manon", "allowed_platforms": "tiktok"})
    url = "/campaigns/1/bounties/1"
    r = client.post(f"{url}/docs", files=[("docs", ("b.png", _png(tmp_path), "image/png"))])
    assert "Reading 1 requirement screenshot" in r.text and "reading…" in r.text
    assert "Nothing new" in client.post(
        f"{url}/docs", files=[("docs", ("b.png", _png(tmp_path), "image/png"))]).text

    run_local(["brief"], once=True, idle_s=0)
    page = client.get(url).text
    assert "Playmaker logos must be clearly visible" in page and "No profanity in the hook" in page

    r = client.post(f"{url}/docs/apply")
    # #manon is already a campaign tag, so only @manon is new; it goes on the bounty.
    assert "Updated tags: @manon" in r.text and "end date: 2026-11-15" in r.text
    c = conn.execute("SELECT * FROM campaigns WHERE id=1").fetchone()
    b = conn.execute("SELECT * FROM bounties WHERE id=1").fetchone()
    assert c["required_tags"] == "#manon" and b["required_tags"] == "@manon"
    assert c["allowed_platforms"] == "tiktok,youtube" and b["end_date"] == "2026-11-15"
    assert "drive.google.com/x" in b["source_links"] and "English only" in b["notes"]
    assert c["disclosure"] == "#ad"  # never touched by a brief
    assert "Nothing new to apply" in client.post(f"{url}/docs/apply").text  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM api_costs WHERE stage='brief'").fetchone()[0] == 1

    client.post(f"{url}/docs/1/delete")
    assert conn.execute("SELECT COUNT(*) FROM campaign_docs").fetchone()[0] == 0
