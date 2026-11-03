"""YouTube channels as footage (D41): list and pick videos, watch for new uploads."""

import pytest

from clipforge import channels
from clipforge.ingest import add_source

ENTRIES = [  # newest first, as YouTube lists them
    {"id": "AAAAAAAAAA1", "title": "New long video", "duration": 1800, "view_count": 5_000,
     "timestamp": 4_070_908_800},
    {"id": "AAAAAAAAAA2", "title": "Teaser", "duration": 50, "view_count": 90_000},
    {"id": "AAAAAAAAAA3", "title": "Big old video", "duration": 3600, "view_count": 2_000_000},
    {"id": "AAAAAAAAAA4", "title": "Going live", "duration": None, "live_status": "is_upcoming"},
]


@pytest.fixture
def youtube(monkeypatch):
    """A fake channel: `listed` is what its uploads tab shows; it has no streams tab."""
    state = {"calls": [], "listed": list(ENTRIES)}

    def flat(url, limit):
        state["calls"].append(url)
        if url.endswith("/streams"):
            raise channels.NoSuchTab("This channel has no past live streams")
        return {"channel_id": "UC" + "x" * 22, "channel": "Man On",
                "entries": state["listed"][:limit]}

    monkeypatch.setattr(channels, "_flat", flat)
    return state


def test_channel_links():
    assert channels.channel_base("https://youtube.com/@ManOn/videos") == \
        "https://www.youtube.com/@ManOn"
    assert channels.channel_base("youtube.com/channel/UC" + "a" * 22)
    assert channels.channel_base("https://www.youtube.com/watch?v=AAAAAAAAAA1") is None
    assert channels.channel_base("https://www.youtube.com/shorts/AAAAAAAAAA1") is None


def test_listing_hides_live_and_dates_what_it_can(youtube):
    info = channels.listing("https://www.youtube.com/@ManOn")
    assert youtube["calls"][0].endswith("/@ManOn/videos")
    assert [v["id"] for v in info["videos"]] == ["AAAAAAAAAA1", "AAAAAAAAAA2", "AAAAAAAAAA3"]
    assert info["videos"][0]["published"] == "2099-01-01"
    assert info["videos"][1]["published"] == ""


def test_a_channel_link_is_never_queued_as_one_video(conn, campaign_id):
    with pytest.raises(ValueError, match="whole channel"):
        add_source(conn, campaign_id, "https://www.youtube.com/@ManOn")


def test_pick_videos_from_a_channel(client, conn, campaign_id, youtube):
    bid = conn.execute("SELECT id FROM bounties WHERE campaign_id=?",
                       (campaign_id,)).fetchone()[0]
    base = f"/campaigns/{campaign_id}/bounties/{bid}"
    # Pasting a channel into "Add links" opens its list instead of queuing it.
    r = client.post("/sources", data={"campaign_id": campaign_id, "bounty_id": bid,
                                      "urls": "https://www.youtube.com/@ManOn"})
    chid = conn.execute("SELECT id FROM bounty_channels WHERE bounty_id=?", (bid,)).fetchone()[0]
    assert r.url.path == f"{base}/channels/{chid}"
    assert "New long video" in r.text and "Teaser" not in r.text and "1 under 3 min" in r.text
    r = client.get(f"{base}/channels/{chid}?sort=views")
    assert r.text.index("Big old video") < r.text.index("New long video")

    r = client.post(f"{base}/channels/{chid}/queue",
                    data={"vid": ["AAAAAAAAAA3", "bad id"], "clips": 3})
    assert "Queued 1 video" in r.text
    row = conn.execute("SELECT url, bounty_id, clips_wanted FROM sources").fetchone()
    assert tuple(row) == ("https://www.youtube.com/watch?v=AAAAAAAAAA3", bid, 3)
    assert "already new" in client.get(f"{base}/channels/{chid}").text
    assert "pick videos" in client.get(base).text


def test_watching_queues_or_pings_new_long_uploads_once(client, conn, campaign_id, youtube):
    bid = conn.execute("SELECT id FROM bounties WHERE campaign_id=?",
                       (campaign_id,)).fetchone()[0]
    chid = channels.add(conn, bid, "https://www.youtube.com/@ManOn")
    assert channels.poll(conn) == []  # not watched
    r = client.post(f"/campaigns/{campaign_id}/bounties/{bid}/channels/{chid}/watch",
                    data={"on": "1"})
    assert "Watching" in r.text
    assert channels.poll(conn) == []  # what was already there isn't new

    youtube["listed"] = [
        {"id": "BBBBBBBBBB1", "title": "Fresh episode", "duration": 2400},
        {"id": "BBBBBBBBBB2", "title": "Clip dump", "duration": 90},  # too short
        {"id": "BBBBBBBBBB3", "title": "Premiere", "duration": None,
         "live_status": "is_upcoming"}, *ENTRIES]
    msgs = channels.poll(conn)
    assert len(msgs) == 1 and "Fresh episode (40 min)" in msgs[0]
    assert "Clip dump" not in msgs[0] and "Pick what to clip" in msgs[0]
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert channels.poll(conn) == []  # announced once

    youtube["listed"][2] = {"id": "BBBBBBBBBB3", "title": "Premiere", "duration": 1200}
    conn.execute("UPDATE bounties SET auto_queue=1 WHERE id=?", (bid,))
    msgs = channels.poll(conn)  # the premiere finished: now it counts, and auto-queue is on
    assert "Premiere (20 min)" in msgs[0] and "Auto-queued" in msgs[0]
    assert conn.execute("SELECT url FROM sources").fetchone()[0].endswith("BBBBBBBBBB3")


def test_first_poll_seeds_when_watch_could_not(conn, campaign_id, youtube, monkeypatch):
    bid = conn.execute("SELECT id FROM bounties WHERE campaign_id=?",
                       (campaign_id,)).fetchone()[0]
    chid = channels.add(conn, bid, "https://www.youtube.com/@ManOn")
    monkeypatch.setattr(channels, "_latest", lambda base: 1 / 0)
    channels.set_watch(conn, chid, True)  # YouTube unreachable: nothing marked yet
    monkeypatch.undo()
    monkeypatch.setattr(channels, "_flat", lambda url, n: {"entries": ENTRIES})
    assert channels.poll(conn) == [] and channels.poll(conn) == []  # backlog never announced
