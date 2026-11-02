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
    post = client.get("/campaigns/1/bounties/1/post").text
    assert "Ep4 hook" in post and 'data-hook="Ep4 hook"' in post
    assert client.get("/review").url.path == "/campaigns/1/bounties/2/review"


def test_edit_remove_and_add_post_links(client, conn):
    client.post("/campaigns", data={"name": "N3on", "bounty_name": "lizzo", "rate_usd": "150",
                                    "marketplace": "clipping_net",
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


def test_copy_post_links_one_per_line(client, conn):
    client.post("/campaigns", data={"name": "N3on", "bounty_name": "lizzo", "rate_usd": "150",
                                    "marketplace": "clipping_net",
                                    "rate_mode": "100000", "allowed_platforms": "tiktok,youtube"})
    here = "/campaigns/1/bounties/1/posted"
    a, b = _clip(conn, 1, 1, "A", "approved"), _clip(conn, 1, 1, "B", "approved")
    for k, url in [(a, "https://www.tiktok.com/@x/video/1"), (a, "https://youtube.com/shorts/a"),
                   (b, "https://www.tiktok.com/@x/video/2")]:
        client.post(f"/clips/{k}/post", data={"platform": "youtube" if "you" in url else "tiktok",
                                              "url": url, "next": here})
    page = client.get(here).text
    assert "Copy all 3 links" in page and "Copy 2 links" in page and "Copy 1 link<" in page
    assert ('data-links="https://www.tiktok.com/@x/video/1\nhttps://youtube.com/shorts/a\n'
            'https://www.tiktok.com/@x/video/2"') in page
    assert "not submitted" not in page  # all unsubmitted: one button is enough
    client.post("/posts/1/submitted", data={"next": here})
    assert "Copy 2 not submitted" in client.get(here).text


def test_terms_and_privacy_pages(client):
    assert "Terms of Service" in client.get("/terms").text
    assert "open_id" in client.get("/privacy").text


def test_home_lists_what_needs_you_and_review_shows_one_clip(client, conn):
    client.post("/campaigns", data={"name": "Man On", "bounty_name": "episode-4",
                                    "rate_usd": "125", "rate_mode": "100000",
                                    "min_views": "100000"})
    assert "caught up" in client.get("/").text
    for hook in ("First hook", "Second hook", "Third hook"):
        _clip(conn, 1, 1, hook, "in_review")
    _clip(conn, 1, 1, "Ready hook", "approved")
    home = client.get("/").text
    assert "Needs you" in home and "/campaigns/1/bounties/1/review" in home
    assert "Review Man On, episode-4" in home and "/campaigns/1/bounties/1/post" in home

    # One clip at a time: Skip moves along, a decision shows the next at the same place.
    page = client.get("/campaigns/1/bounties/1/review").text
    assert "1 of 3" in page and page.count("<video") == 1 and "?at=1" in page
    assert "2 of 3" in client.get("/campaigns/1/bounties/1/review?at=1").text
    r = client.post("/clips/1/review", data={"decision": "rejected", "reason": "boring",
                                             "next": "/campaigns/1/bounties/1/review"})
    assert "1 of 2" in r.text
    # The payout line: best clip's views against the bounty's minimum.
    assert "of 100,000 views on the best clip" in page
