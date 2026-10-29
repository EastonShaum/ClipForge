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
    assert "live streams get clipped" in r.text and "Renders on hold" in r.text
    b = conn.execute("SELECT * FROM bounties").fetchone()
    assert b["live_clip"] == 1 and b["render_hold"] == 1
    assert hold_reasons(conn, b) == ["requirement screenshots"]

    # Goes live: one session, a ping that says what's missing.
    msgs = live.check(conn, _probe({"kick.com/n3on": {"id": "77", "title": "Lizzo stream"}}))
    assert len(msgs) == 1 and "N3on is live" in msgs[0] and "requirement screenshots" in msgs[0]
    assert "laptop worker is offline" in msgs[0]
    assert live.check(conn, _probe({"kick.com/n3on": {"id": "77", "title": "x"}})) == []

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


def test_watch_only_records_then_a_new_live_bounty_clips_it(client, conn):
    client.post("/campaigns", data={"name": "Kai Cenat", "bounty_name": "main",
                                    "category": "streamer", "rate_usd": "100",
                                    "rate_mode": "100000"})
    client.post("/campaigns/1/live", data={"watch_live": "true"})  # no channels: guess them
    probe = _probe({"kick.com/kaicenat": None, "twitch.tv/kaicenat": {"id": "9", "title": "t"}})
    msgs = live.check(conn, probe)
    assert "Watching **Kai Cenat** on kick.com/kaicenat, twitch.tv/kaicenat" in msgs[0]
    assert "only recorded" in msgs[1]
    sess = live.for_recorder(conn, "eastons-laptop")["sessions"][0]
    live.report(conn, sess["id"], {"chunks": [{"path": "C:/Live/a.ffconcat", "overlap_ms": 0}],
                                   "recording": True})
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0  # nobody pays yet

    # A bounty opens and is added with "Add & clip live": the recording so far gets clipped.
    client.post("/campaigns", data={"name": "Kai Cenat", "bounty_name": "stream-clips",
                                    "rate_usd": "100", "rate_mode": "100000",
                                    "live_clip": "true"})
    live.check(conn, probe)
    src = conn.execute("SELECT * FROM sources").fetchone()
    assert src["url"] == "C:/Live/a.ffconcat" and src["bounty_id"] == 2
    job = conn.execute("SELECT affinity_host FROM jobs WHERE kind='fetch'").fetchone()
    assert job["affinity_host"] == "eastons-laptop"  # the laptop has the recording


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
