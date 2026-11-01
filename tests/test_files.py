"""Lives on/off per stream, local times, and the Files page (D37): what ClipForge saved, when
it deletes itself, and deleting it now."""

from clipforge import live
from clipforge.web.app import local_time
from tests.test_live import _probe


def _stream(client, conn, title="DBD"):
    client.post("/lives/watch", data={"name": "BurntPeanut",
                                      "live_channels": "twitch.tv/theburntpeanut"})
    live.check(conn, _probe({"twitch.tv/theburntpeanut": {"id": "1", "title": title}}))
    return live.for_recorder(conn, "eastons-laptop")["sessions"][0]


def _chunk(conn, sid, name):
    live.report(conn, sid, {"chunks": [{"path": f"C:\\Live\\BP\\{name}.ffconcat",
                                        "overlap_ms": 0}], "recording": True})


def test_times_show_in_mountain_time():
    assert local_time("2026-10-31T23:03:00Z") == "Oct 31, 5:03 PM"  # MDT, UTC-6
    assert local_time("2026-11-02T23:03:00Z", "t") == "4:03 PM"  # MST from Nov 1, UTC-7
    assert local_time("2026-11-01T05:00:00Z", "day") == "Sat Oct 31"
    assert local_time(None) == ""


def test_a_live_stream_can_be_turned_off_and_on(client, conn):
    sess = _stream(client, conn)
    _chunk(conn, sess["id"], "a")
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0] == 1
    page = client.get("/lives").text
    assert 'name="on" value="1" checked' in page and "Streamers" in page

    r = client.post(f"/lives/{sess['id']}/recording", data={"on": "0"})
    assert "Stopped" in r.text and 'value="0" checked' in r.text
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "cancelled"
    assert live.for_recorder(conn, "eastons-laptop")["sessions"][0]["live"] is False
    _chunk(conn, sess["id"], "b")  # the recorder's last chunk: not clipped
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1

    client.post(f"/lives/{sess['id']}/recording", data={"on": "1"})
    assert live.for_recorder(conn, "eastons-laptop")["sessions"][0]["live"] is True
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2

    # Ended streams fold to one line: streamer, day, start-end.
    conn.execute("UPDATE live_sessions SET state='ended', started_at='2026-10-31T23:03:00Z',"
                 " ended_at='2026-11-01T06:58:00Z'")
    page = client.get("/lives").text
    assert '<details class="card stream"' in page and "Sat Oct 31 · 5:03 PM – 12:58 AM" in page
    assert "Watch a streamer" not in page
    assert "Watch a streamer" in client.get("/lives?tab=streamers").text


def test_old_recordings_are_deleted_even_with_empty_chunks(client, conn):
    sess = _stream(client, conn)
    _chunk(conn, sess["id"], "a")
    src = conn.execute("SELECT id FROM sources").fetchone()[0]
    # Clipped, nothing good in it: the chunk stays at 'selected' with no kept moments.
    conn.execute("UPDATE jobs SET state='done'")
    conn.execute("UPDATE sources SET status='selected' WHERE id=?", (src,))
    conn.execute("UPDATE live_sessions SET state='ended', ended_at='2020-01-01T00:00:00Z',"
                 " finalized_at='2020-01-01T00:10:00Z'")
    # A picked clip still waiting to render keeps it.
    cand = conn.execute("INSERT INTO candidates (source_id, start_ms, end_ms, hook_text, score,"
                        " rank, kept) VALUES (?, 0, 9000, 'h', 50, 1, 1)", (src,)).lastrowid
    page = client.get("/files").text
    assert "Kept while 1 picked clip(s) wait to render" in page
    assert live.for_recorder(conn, "eastons-laptop")["purge"] == []
    conn.execute("DELETE FROM candidates WHERE id=?", (cand,))
    assert "Due now" in client.get("/files").text
    assert live.for_recorder(conn, "eastons-laptop")["purge"] == [sess["dir"]]
    assert "1 older recording already deleted" in client.get("/files").text


def test_files_page_deletes_a_recording_and_clip_videos(client, conn, settings):
    sess = _stream(client, conn)
    _chunk(conn, sess["id"], "a")
    page = client.get("/files").text
    assert "C:\\Live\\BP" in page and "Recording now" in page
    r = client.post(f"/files/recordings/{sess['id']}/delete")
    assert "turn it off first" in r.text  # not while recording
    client.post(f"/lives/{sess['id']}/recording", data={"on": "0"})
    r = client.post(f"/files/recordings/{sess['id']}/delete")
    assert "next time the laptop checks in" in r.text
    plan = live.for_recorder(conn, "eastons-laptop")
    assert plan["purge"] == [sess["dir"]] and plan["sessions"] == []

    src = conn.execute("SELECT id, bounty_id FROM sources").fetchone()
    cand = conn.execute("INSERT INTO candidates (source_id, start_ms, end_ms, hook_text, score,"
                        " rank, kept) VALUES (?, 0, 9000, 'h', 50, 1, 1)", (src[0],)).lastrowid
    f = settings.clips_dir / f"{src[0]}" / "c1.mp4"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x" * 1000)
    conn.execute("INSERT INTO clips (candidate_id, path, size_bytes, status) VALUES"
                 " (?, ?, 1000, 'rejected')", (cand, f"{src[0]}/c1.mp4"))
    assert "Delete rejected videos" in client.get("/files").text
    r = client.post("/files/clips/delete", data={"bounty_id": src[1], "status": "rejected"})
    assert "Deleted 1 video(s)" in r.text and not f.exists()
    assert "Delete rejected videos" not in client.get("/files").text
