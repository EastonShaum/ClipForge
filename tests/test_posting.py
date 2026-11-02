"""Posting by hand (D38): accounts, the caption check, one moment per platform, the brand's
review of each submission, and the per-bounty on-screen #ad label."""

from clipforge import posting
from clipforge.config import get_settings
from clipforge.pipeline import build_context


def _clip(conn, cid, src_id=None, start=0, end=20_000, hook="h", caption="#ad hook"):
    bid = conn.execute("SELECT id FROM bounties WHERE campaign_id=?", (cid,)).fetchone()[0]
    if src_id is None:
        src_id = conn.execute("INSERT INTO sources (campaign_id, bounty_id, url, source_key)"
                              " VALUES (?,?,?,?)", (cid, bid, f"u{start}", f"k{start}-{end}"
                                                    )).lastrowid
    cand = conn.execute("INSERT INTO candidates (source_id, start_ms, end_ms, hook_text, score,"
                        " rank, kept) VALUES (?,?,?,?,50,1,1)",
                        (src_id, start, end, hook)).lastrowid
    k = conn.execute("INSERT INTO clips (candidate_id, path, status, caption) VALUES"
                     " (?, 'x.mp4', 'approved', ?)", (cand, caption)).lastrowid
    return k, src_id, bid


def test_caption_check_and_fix():
    assert posting.caption_problems("#ad great clip @pod #tag", "@pod #tag") == []
    probs = posting.caption_problems("great clip #ad", "@pod #tag")
    assert probs == ["#ad must come first", "missing @pod", "missing #tag"]
    fixed = posting.fix_caption("great clip #ad", "@pod #tag")
    assert fixed.startswith("#ad great clip") and fixed.endswith("@pod #tag")
    assert posting.caption_problems(fixed, "@pod #tag") == []


def test_accounts_and_the_post_tab(client, conn, campaign_id):
    r = client.post("/accounts", data={"platform": "instagram", "handle": "ez.clips"})
    assert "off for now" in r.text
    client.post("/accounts", data={"platform": "tiktok", "handle": "@ez.clips", "niche": "pods"})
    accts = posting.accounts(conn)
    assert {a["platform"]: a["enabled"] for a in accts} == {"tiktok": 1, "instagram": 0}
    k, src, bid = _clip(conn, campaign_id, caption="hook only")
    page = client.get(f"/campaigns/{campaign_id}/bounties/{bid}/post").text
    assert "Caption not ready" in page and "missing @testpod" in page
    assert "Copy caption" not in page and "TikTok @ez.clips" in page
    client.post(f"/clips/{k}/caption/fix")
    assert "Caption not ready" not in client.get(f"/campaigns/{campaign_id}/bounties/{bid}/post").text

    tiktok = next(a["id"] for a in accts if a["platform"] == "tiktok")
    r = client.post(f"/clips/{k}/post", data={"account_id": str(tiktok),
                                              "url": "https://tiktok.com/@ez/video/1"})
    assert "Logged tiktok post" in r.text
    assert conn.execute("SELECT account_id FROM posts").fetchone()[0] == tiktok
    assert posting.cadence(conn)[0]["posted"] == 1  # 1 of 1 on warm-up day 1

    # The same moment, cut again (overlapping 15 of its 20 s), can't go on TikTok twice.
    k2, _, _ = _clip(conn, campaign_id, src_id=src, start=5_000, end=25_000, hook="h2")
    r = client.post(f"/clips/{k2}/post", data={"account_id": str(tiktok),
                                               "url": "https://tiktok.com/@ez/video/2"})
    assert f"clip #{k} already put this moment on tiktok" in r.text
    assert "Logged youtube post" in client.post(
        f"/clips/{k2}/post", data={"platform": "youtube", "url": "https://youtu.be/2"}).text
    # A third clip reusing a posted hook is flagged.
    _clip(conn, campaign_id, start=90_000, end=99_000, hook="h", caption="#ad x @testpod")
    assert "already used this hook" in client.get(
        f"/campaigns/{campaign_id}/bounties/{bid}/post").text


def test_submission_review_steps(client, conn, campaign_id):
    conn.execute("UPDATE campaigns SET marketplace='content_rewards'")
    conn.execute("UPDATE bounties SET brief_url='https://contentrewards.com/c/1'")
    k, src, bid = _clip(conn, campaign_id)
    client.post(f"/clips/{k}/post", data={"platform": "tiktok", "url": "https://t.co/1"})
    pid = conn.execute("SELECT id FROM posts").fetchone()[0]
    client.post(f"/posts/{pid}/views", data={"views": "60000"})
    page = client.get(f"/campaigns/{campaign_id}/bounties/{bid}/posted").text
    assert "Open campaign page" in page and "Copy all" not in page  # one per video

    r = client.post(f"/posts/{pid}/submitted", data={})
    assert "waiting for the brand" in r.text
    assert "Rejected" in client.post(f"/posts/{pid}/review", data={"step": "rejected"}).text \
        and "Write the brand" in client.post(f"/posts/{pid}/review",
                                             data={"step": "rejected"}).text
    client.post(f"/posts/{pid}/review", data={"step": "approved", "approved_views": "60000"})
    p = conn.execute("SELECT * FROM posts").fetchone()
    assert p["review_status"] == "approved" and p["approved_views"] == 60_000 and p["approved_at"]
    page = client.get(f"/campaigns/{campaign_id}/bounties/{bid}/posted").text
    assert "Mark paid" in page and 'value="21.60"' in page  # $0.40/1K × 60K, less 10%
    client.post(f"/posts/{pid}/review", data={"step": "paid", "earned_usd": "20.00"})
    p = conn.execute("SELECT * FROM posts").fetchone()
    assert p["review_status"] == "paid" and p["earned_usd"] == 20 and p["paid_at"]
    assert "Paid $20.00" in client.get(f"/campaigns/{campaign_id}/bounties/{bid}/posted").text


def test_onscreen_label_follows_the_bounty(conn, campaign_id):
    k, src, bid = _clip(conn, campaign_id)
    cand = conn.execute("SELECT candidate_id FROM clips WHERE id=?", (k,)).fetchone()[0]
    job = {"kind": "render", "candidate_id": cand}
    assert build_context(conn, get_settings(), job)["onscreen"] is False
    conn.execute("UPDATE bounties SET onscreen_ad_label_required=1")
    assert build_context(conn, get_settings(), job)["onscreen"] is True
