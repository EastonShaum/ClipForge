"""Review, post and views live inside each bounty; posts can be edited, removed and added to."""


def _clip(conn, bounty_id: int, campaign_id: int, hook: str, status: str) -> int:
    sid = conn.execute("INSERT INTO sources (campaign_id, bounty_id, url, source_key)"
                       " VALUES (?,?,?,?)", (campaign_id, bounty_id, f"https://x/{hook}",
                                             f"k:{hook}")).lastrowid
    cand = conn.execute("INSERT INTO candidates (source_id, start_ms, end_ms, hook_text, score,"
                        " rank, kept) VALUES (?,0,20000,?,70,1,1)", (sid, hook)).lastrowid
    return conn.execute("INSERT INTO clips (candidate_id, path, status) VALUES (?,?,?)",
                        (cand, f"{sid}/c{cand}.mp4", status)).lastrowid


def test_tabs_show_only_this_bountys_clips(client, conn):
    client.post("/campaigns", data={"name": "Man On", "bounty_name": "episode-4",
                                    "rate_usd": "125", "rate_mode": "100000"})
    client.post("/campaigns/1/bounties", data={"name": "episode-5", "rate_usd": "125",
                                               "rate_mode": "100000"})
    _clip(conn, 1, 1, "Ep4 hook", "in_review")
    _clip(conn, 2, 1, "Ep5 hook", "in_review")
    page = client.get("/campaigns/1/bounties/1/review").text
    assert "Ep4 hook" in page and "Ep5 hook" not in page
    assert 'Review <span class="count">1</span>' in page
    listing = client.get("/campaigns").text
    assert "/campaigns/1/bounties/2/review" in listing and "1 to review" in listing
    # Deciding returns to the same bounty's tab.
    r = client.post("/clips/1/review", data={"decision": "approved",
                                             "next": "/campaigns/1/bounties/1/review"})
    assert r.url.path == "/campaigns/1/bounties/1/review" and "Approved" in r.text
    assert "Ep4 hook" in client.get("/campaigns/1/bounties/1/post").text
    assert client.get("/review").url.path == "/campaigns/1/bounties/2/review"


def test_edit_remove_and_add_post_links(client, conn):
    client.post("/campaigns", data={"name": "N3on", "bounty_name": "lizzo", "rate_usd": "150",
                                    "rate_mode": "100000", "min_views": "50000",
                                    "allowed_platforms": "tiktok,youtube"})
    k = _clip(conn, 1, 1, "Space for $10M", "approved")
    here = "/campaigns/1/bounties/1/posted"
    client.post(f"/clips/{k}/post", data={"platform": "tiktok", "next": here,
                                          "url": "https://www.tiktok.com/@ggcutroom/video/1"})
    # Posted on TikTok only: still on the Post tab for YouTube, and on Posted.
    assert "post to: youtube" in client.get("/campaigns/1/bounties/1/post").text
    page = client.get(here).text
    assert "not posted yet: youtube" in page and "Edit link" in page

    r = client.post("/posts/1/edit", data={"platform": "tiktok", "next": here,
                                           "url": "https://www.tiktok.com/@ggcutroom/video/2"})
    assert "Post updated" in r.text
    assert conn.execute("SELECT url FROM posts").fetchone()[0].endswith("/video/2")
    client.post(f"/clips/{k}/post", data={"platform": "youtube", "next": here,
                                          "url": "https://www.youtube.com/shorts/abc"})
    assert "Nothing left to post" in client.get("/campaigns/1/bounties/1/post").text
    r = client.post("/posts/2/edit", data={"platform": "youtube", "next": here,
                                           "url": "https://www.tiktok.com/@ggcutroom/video/2"})
    assert "already logged" in r.text  # can't point two posts at one URL

    client.post("/posts/1/views", data={"views": "60000", "next": here})
    page = client.get(here).text
    assert "60,000 views" in page and "qualifies" in page

    client.post("/posts/1/delete", data={"next": here})
    client.post("/posts/2/delete", data={"next": here})
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM view_snapshots").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM clips").fetchone()[0] == "approved"  # back to Post
