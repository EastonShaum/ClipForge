"""Live streams (D30): watch channels, record in pieces, clip overlapping chunks, hold renders
until the requirements are in. Offline: a fake live check and the test-pattern fixture."""

import os
import subprocess
import time

from clipforge import live, recorder
from clipforge.bounties import held_count, hold_reasons
from clipforge.reminders import held_messages
from clipforge.worker import run_local
from tests.test_e2e import FIXTURE

H = {"Authorization": "Bearer test-token"}


def _probe(live_now: dict):
    """Fake live check: channel -> info (live), None (offline), or missing."""
    def probe(ch):
        v = live_now.get(ch, "missing")
        if v == "missing":
            raise live.ChannelMissing(ch)
        return v
    return probe


def _pieces(d, n_s=10):
    """The 40 s fixture recorded as ~10 s .ts pieces, finished a while ago."""
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(FIXTURE), "-c", "copy", "-f",
                    "segment", "-segment_time", str(n_s), "-reset_timestamps", "1",
                    "-segment_format", "mpegts", str(d / "p%05d.ts")], check=True)
    old = time.time() - 600
    for p in d.glob("p*.ts"):
        os.utime(p, (old, old))


def test_channels_parse_and_guess():
    good, bad = live.parse_channels("https://kick.com/N3on, twitch.tv/n3on kick.com/n3on x.com/y")
    assert good == ["kick.com/n3on", "twitch.tv/n3on"] and bad == ["x.com/y"]
    assert live.channel_url("twitch.tv/n3on") == "https://www.twitch.tv/n3on"
    assert live.guesses("Kai Cenat") == ["kick.com/kaicenat", "twitch.tv/kaicenat"]


def test_chunk_plan_overlaps_one_piece_and_folds_the_tail():
    durs = [120.0] * 16
    assert recorder.plan(durs, 0, 1800, 120, final=False) == (0, 15)
    assert recorder.plan(durs, 15, 1800, 120, final=False) is None  # 2 min isn't a chunk yet
    assert recorder.plan(durs, 15, 1800, 120, final=True) == (14, 16)  # overlap + the rest
    assert recorder.plan([120.0] * 15 + [30.0], 0, 1800, 120, final=True) == (0, 16)
    assert recorder.plan([120.0, 20.0], 1, 1800, 120, final=True) is None  # 20 s scrap


def test_recorder_writes_playable_overlapping_chunks(tmp_path):
    d = tmp_path / "Live" / "N3on" / "kick-n3on 2026-10-28 2130"
    _pieces(d)
    # Still recording: the newest piece is unfinished, so only 30 s are chunkable.
    got = recorder.make_chunks(d, "N3on live 10-28", recording=True, final=False,
                               chunk_s=20, overlap_s=10)
    assert len(got["chunks"]) == 1 and got["chunks"][0]["overlap_ms"] == 0
    got = recorder.make_chunks(d, "N3on live 10-28", recording=False, final=True,
                               chunk_s=20, overlap_s=10)
    first, second = got["chunks"]
    assert second["overlap_ms"] > 0 and 39 < got["recorded_s"] < 41
    assert os.path.basename(first["path"]) == "N3on live 10-28 #01 0h00-0h00.ffconcat"
    for ch in got["chunks"]:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", ch["path"]], capture_output=True, text=True)
        assert float(out.stdout) > 15
    # Idempotent: nothing new to chunk, same list back.
    again = recorder.make_chunks(d, "N3on live 10-28", recording=False, final=True,
                                 chunk_s=20, overlap_s=10)
    assert again["chunks"] == got["chunks"]


def test_live_bounty_records_clips_and_holds_until_rules(client, conn, settings, tmp_path,
                                                         monkeypatch):
    sent = []
    monkeypatch.setattr("clipforge.web.app.notify.discord", sent.append)
    r = client.post("/campaigns", data={"name": "N3on", "bounty_name": "lizzo",
                                        "category": "streamer", "rate_usd": "150",
                                        "rate_mode": "100000", "live_clip": "true",
                                        "live_channels": "https://kick.com/N3on"})
    assert "Move a stream into it from the Lives tab" in r.text and "Renders on hold" in r.text
    b = conn.execute("SELECT * FROM bounties").fetchone()
    assert b["live_clip"] == 1 and b["render_hold"] == 1
    assert hold_reasons(conn, b) == ["requirement screenshots"]

    # Goes live: one session with its own bounty (D34), and a ping.
    msgs = live.check(conn, _probe({"kick.com/n3on": {"id": "77", "title": "Lizzo stream"}}))
    assert len(msgs) == 1 and "N3on is live" in msgs[0] and "/lives" in msgs[0]
    assert "laptop worker is offline" in msgs[0]
    assert live.check(conn, _probe({"kick.com/n3on": {"id": "77", "title": "x"}})) == []
    own = conn.execute("SELECT * FROM bounties WHERE live_session_id IS NOT NULL").fetchone()
    assert own["name"].startswith("live ") and own["media_folder"] is None
    page = client.get("/lives").text
    assert "Lizzo stream" not in page and "“x”" in page and "N3on · lizzo" in page
    assert own["name"] not in client.get("/campaigns/1").text  # not under the campaign
    # The bounty came up: move the stream into it. Its chunks go there from now on.
    r = client.post("/lives/1/bounty", data={"bounty_id": str(b["id"])})
    assert "to N3on · lizzo" in r.text

    # The recorder (here: the Pi itself, so the local worker can run the chunk) claims it.
    plan = client.get("/api/worker/live", headers=H, params={"host": "pi"}).json()
    sess = plan["sessions"][0]
    assert sess["live"] and sess["url"] == "https://kick.com/n3on" and plan["chunk_s"] == 300
    assert plan["piece_s"] == 30 and plan["overlap_s"] == 60
    d = tmp_path / "Live" / sess["dir"]
    _pieces(d)
    got = recorder.make_chunks(d, sess["name"], recording=False, final=False, chunk_s=20,
                               overlap_s=10)
    r = client.post(f"/api/worker/live/{sess['id']}", headers=H,
                    json={"recording": True, "final": False, "recorded_s": 40,
                          "bytes": 1, "chunks": got["chunks"]})
    assert r.status_code == 200
    srcs = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
    assert len(srcs) == 2 and srcs[1]["skip_ms"] > 0 and srcs[0]["clips_wanted"] == 2
    assert srcs[0]["title"] == "x" and srcs[0]["bounty_id"] == b["id"]

    r = client.post(f"/live/{sess['id']}/guests", data={"guests": "Alabama Barker,  Yera"})
    assert "next chunks use these names" in r.text

    # Live chunks get the live prompt, its lengths and the names (D33).
    from clipforge.config import get_settings
    from clipforge.pipeline import build_context
    job = conn.execute("SELECT * FROM jobs WHERE kind='fetch' LIMIT 1").fetchone()
    ctx = build_context(conn, get_settings(), dict(job) | {"kind": "select"})
    assert "Streamer: N3on" in ctx["prompt"] and "Alabama Barker, Yera" in ctx["prompt"]
    assert (ctx["min_s"], ctx["max_s"]) == (6.0, 25.0)

    run_local(["fetch", "transcribe", "select", "render"], once=True, idle_s=0)
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 0  # held
    pv = conn.execute("SELECT DISTINCT pv.name FROM candidates c JOIN prompt_versions pv"
                      " ON pv.id=c.prompt_version_id").fetchall()
    assert pv and all(r[0].startswith("live-") for r in pv)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='render'").fetchone()[0] == 0
    # Moments wholly inside the second chunk's overlap were offered by the first chunk.
    skip = srcs[1]["skip_ms"]
    assert not conn.execute("SELECT COUNT(*) FROM candidates WHERE source_id=? AND kept=1"
                            " AND end_ms <= ?", (srcs[1]["id"], skip)).fetchone()[0]
    held = held_count(conn, b["id"])
    assert held > 0
    assert "waiting for rules" in client.get(f"/campaigns/1/bounties/{b['id']}").text

    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    first = held_messages(conn, settings, now)
    assert len(first) == 1 and f"{held} clip(s) for N3on · lizzo" in first[0]
    assert held_messages(conn, settings, now + timedelta(hours=1)) == []  # not every minute
    assert len(held_messages(conn, settings, now + timedelta(hours=4))) == 1

    r = client.post(f"/campaigns/1/bounties/{b['id']}/release")
    assert f"Rendering {held} clip(s) now" in r.text
    run_local(["render"], once=True, idle_s=0)
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == held
    assert all("#n3on" in r[0] for r in conn.execute("SELECT caption FROM clips"))
    assert conn.execute("SELECT status FROM sources WHERE id=?",
                        (srcs[0]["id"],)).fetchone()[0] == "rendered"

    # Stream ends: the session closes, the recorder finishes, one wrap-up ping.
    conn.execute("UPDATE live_sessions SET last_live_at='2020-01-01T00:00:00Z'")
    live.check(conn, _probe({"kick.com/n3on": None}))
    assert conn.execute("SELECT state FROM live_sessions").fetchone()[0] == "ended"
    plan = client.get("/api/worker/live", headers=H, params={"host": "pi"}).json()
    assert plan["sessions"][0]["live"] is False  # still listed: finish the last chunk
    client.post(f"/api/worker/live/{sess['id']}", headers=H,
                json={"recording": False, "final": True, "recorded_s": 40, "bytes": 1,
                      "chunks": got["chunks"]})
    assert any("stream ended: 0.0 h recorded, 2 chunk(s) sent for clipping" in m for m in sent)
    assert client.get("/api/worker/live", headers=H,
                      params={"host": "pi"}).json()["sessions"] == []


def test_each_stream_has_its_own_clips_until_a_bounty_comes_up(client, conn):
    r = client.post("/lives/watch", data={"name": "Kai Cenat"})  # no channels: guess them
    assert "Watching Kai Cenat" in r.text
    assert "Kai Cenat" not in client.get("/campaigns").text  # a streamer, not a campaign
    probe = _probe({"kick.com/kaicenat": None, "twitch.tv/kaicenat": {"id": "9", "title": "t"}})
    msgs = live.check(conn, probe)
    assert "Watching **Kai Cenat** on kick.com/kaicenat, twitch.tv/kaicenat" in msgs[0]
    assert "Clipping every 5 min" in msgs[1]
    sess = live.for_recorder(conn, "eastons-laptop")["sessions"][0]
    live.report(conn, sess["id"], {"chunks": [{"path": "C:/Live/a.ffconcat", "overlap_ms": 0}],
                                   "recording": True})
    first = conn.execute("SELECT * FROM sources").fetchone()
    own = conn.execute("SELECT id FROM bounties WHERE live_session_id=?",
                       (sess["id"],)).fetchone()["id"]
    assert first["url"] == "C:/Live/a.ffconcat" and first["bounty_id"] == own
    job = conn.execute("SELECT affinity_host FROM jobs WHERE kind='fetch'").fetchone()
    assert job["affinity_host"] == "eastons-laptop"  # the laptop has the recording

    # The next day's stream is a different stream: its own bounty, not yesterday's.
    conn.execute("UPDATE live_sessions SET last_live_at='2020-01-01T00:00:00Z'")
    live.check(conn, _probe({"kick.com/kaicenat": None, "twitch.tv/kaicenat": None}))
    probe2 = _probe({"kick.com/kaicenat": None, "twitch.tv/kaicenat": {"id": "10", "title": "u"}})
    live.check(conn, probe2)
    s2 = conn.execute("SELECT * FROM live_sessions ORDER BY id DESC").fetchone()
    assert s2["id"] != sess["id"] and s2["bounty_id"] not in (None, own)

    # A bounty opens mid-stream, added with "Add & clip live": today's stream moves into it.
    client.post("/campaigns", data={"name": "Kai Cenat", "bounty_name": "stream-clips",
                                    "rate_usd": "100", "rate_mode": "100000",
                                    "live_clip": "true"})
    paid = conn.execute("SELECT id FROM bounties WHERE name='stream-clips'").fetchone()["id"]
    live.for_recorder(conn, "eastons-laptop")
    live.report(conn, s2["id"], {"chunks": [{"path": "C:/Live/b.ffconcat", "overlap_ms": 0}],
                                 "recording": True})
    assert conn.execute("SELECT bounty_id FROM sources WHERE url='C:/Live/b.ffconcat'"
                        ).fetchone()[0] == paid
    assert conn.execute("SELECT bounty_id FROM sources WHERE id=?",
                        (first["id"],)).fetchone()[0] == own  # yesterday's stays put
    # And back to its own tabs.
    assert "own tabs" in client.post(
        f"/lives/{s2['id']}/bounty", data={"bounty_id": ""}).text
    assert conn.execute("SELECT bounty_id FROM sources WHERE url='C:/Live/b.ffconcat'"
                        ).fetchone()[0] == s2["bounty_id"]


def test_a_gambling_stream_is_recorded_but_not_clipped(client, conn):
    client.post("/lives/watch", data={"name": "N3on", "live_channels": "kick.com/n3on"})
    msgs = live.check(conn, _probe({"kick.com/n3on": {"id": "1",
                                                      "title": "JACK.COM 18+ GIVEAWAY IF WIN"}}))
    assert "Not clipping" in msgs[0] and "jack.com" in msgs[0]
    sess = live.for_recorder(conn, "eastons-laptop")["sessions"][0]
    # Retitled mid-stream: still blocked.
    live.check(conn, _probe({"kick.com/n3on": {"id": "1", "title": "just chatting"}}))
    live.report(conn, sess["id"], {"chunks": [{"path": "C:/Live/a.ffconcat", "overlap_ms": 0}],
                                   "recording": True})
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert "looks like gambling" in client.get("/lives").text


def test_logo_rule_holds_until_a_logo_is_added(client, conn, monkeypatch):
    client.post("/campaigns", data={"name": "Man On", "bounty_name": "ep5", "rate_usd": "125",
                                    "rate_mode": "100000", "live_clip": "true"})
    conn.execute("INSERT INTO campaign_docs (campaign_id, bounty_id, sha, path, extracted,"
                 " applied_at) VALUES (1, 1, 'a', 'x.jpg', ?, '2026-10-28T00:00:00Z')",
                 ('{"logo_rule": "Man On logo must be clearly visible."}',))
    b = conn.execute("SELECT * FROM bounties").fetchone()
    assert hold_reasons(conn, b) == ["campaign logo"]
    conn.execute("UPDATE campaign_docs SET extracted=?",
                 ('{"logo_rule": "No logos or watermarks."}',))
    assert hold_reasons(conn, b) == []


def test_bad_channel_is_refused(client):
    r = client.post("/campaigns", data={"name": "X", "rate_usd": "1", "rate_mode": "1000",
                                        "live_channels": "youtube.com/@x"})
    assert "Not a Kick or Twitch channel: youtube.com/@x" in r.text


def test_hashtags_name_the_streamer_and_the_guests_in_the_hook():
    guests = "Alabama Barker, Adrien Broner (AB), Yera"
    assert live.hashtags("N3on", guests, "N3on realized AB was FINISHED") == ["#n3on",
                                                                             "#adrienbroner"]
    assert live.hashtags("N3on", guests, "Alabama Barker brought FLOWERS") == [
        "#n3on", "#alabamabarker"]
    assert live.hashtags("N3on", guests, "a tab of absinthe") == ["#n3on"]  # "ab" inside a word


def test_auto_layout_keeps_a_shared_screen_whole_and_crops_a_camera(tmp_path):
    import random

    from clipforge.stages import layout
    rnd = random.Random(1)
    camera = bytes(rnd.randrange(40, 200) for _ in range(layout.W * layout.H))
    screen = bytes([54] * (layout.W * layout.H // 2)) + camera[: layout.W * layout.H // 2]
    assert layout.flat_share(camera) < 0.01 and layout.flat_share(screen) > 0.45
    black = bytes(layout.W * layout.H)  # letterbox bars don't count
    assert layout.flat_share(black) == 0
    assert layout.choose("crop", "nope.mp4", 0, 1000) == "crop"
    for name, src in (("screen", "color=c=0x36393f:s=640x360:d=2,drawbox=x=0:y=0:w=120:h=360"
                                  ":color=0x202225:t=fill"),
                      ("camera", "nullsrc=s=640x360:d=2,geq=lum='random(1)*255':cb=128:cr=128")):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src,
                        str(tmp_path / f"{name}.mp4")], check=True)
    assert layout.choose("auto", str(tmp_path / "screen.mp4"), 0, 2000) == "blurfill"
    assert layout.choose("auto", str(tmp_path / "camera.mp4"), 0, 2000) == "crop"


def test_a_gaming_streamer_can_be_set_to_full_width(client, conn):
    # A 3D game has no flat panels, so auto reads it as a camera: Easton sets the streamer.
    from clipforge.config import get_settings
    from clipforge.pipeline import build_context
    client.post("/lives/watch", data={"name": "BurntPeanut",
                                      "live_channels": "twitch.tv/theburntpeanut"})
    live.check(conn, _probe({"twitch.tv/theburntpeanut": {"id": "1", "title": "DBD"}}))
    sess = live.for_recorder(conn, "eastons-laptop")["sessions"][0]
    live.report(conn, sess["id"], {"chunks": [{"path": "C:/Live/a.ffconcat", "overlap_ms": 0}],
                                   "recording": True})
    src = conn.execute("SELECT id FROM sources").fetchone()["id"]
    cand = conn.execute("INSERT INTO candidates (source_id, start_ms, end_ms, hook_text, score,"
                        " rank) VALUES (?, 0, 9000, 'h', 50, 1)", (src,)).lastrowid
    job = {"kind": "render", "candidate_id": cand}
    assert build_context(conn, get_settings(), job)["layout"] == "auto"
    cid = conn.execute("SELECT campaign_id FROM live_sessions").fetchone()[0]
    client.post(f"/campaigns/{cid}/live", data={"live_channels": "twitch.tv/theburntpeanut",
                                                "watch_live": "true",
                                                "live_layout": "blurfill"})
    assert build_context(conn, get_settings(), job)["layout"] == "blurfill"
    assert 'value="blurfill" selected' in client.get("/lives?tab=streamers").text
