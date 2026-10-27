"""Campaign media library: laptop scan + report, folder matching, confirm-then-queue, delete."""

import os
import shutil
import time

from clipforge import library
from tests.test_e2e import FIXTURE

H = {"Authorization": "Bearer test-token"}


def test_scan_skips_partial_and_marks_fresh_files(tmp_path):
    root = tmp_path / "Campaigns"
    (root / "ManOn").mkdir(parents=True)
    shutil.copy(FIXTURE, root / "ManOn" / "ep4.mp4")
    old = time.time() - 3600
    os.utime(root / "ManOn" / "ep4.mp4", (old, old))
    (root / "ManOn" / "ep5.mp4.crdownload").write_bytes(b"x")  # Chrome, still downloading
    shutil.copy(FIXTURE, root / "ManOn" / "fresh.mp4")  # just written: may still be copying
    files = library.scan(root)
    assert [f["rel"] for f in files] == ["ManOn/ep4.mp4", "ManOn/fresh.mp4"]
    assert files[0]["duration_s"] > 30 and files[1]["duration_s"] is None


def test_make_folders_creates_plain_names_only(tmp_path):
    made = library.make_folders(tmp_path, ["ManOn/episode-4", "..", "a/../b", "C:x", "/x", ""])
    assert made == ["ManOn/episode-4"] and (tmp_path / "ManOn" / "episode-4").is_dir()
    assert library.make_folders(tmp_path, ["ManOn/episode-4"]) == []


def _report(client, files):
    return client.post("/api/worker/library", headers=H,
                       json={"host": "eastons-laptop", "files": files})


def _file(rel, mins=40, age=3600):
    return {"path": "C:\\Users\\E\\CLipping\\Campaigns\\" + rel.replace("/", "\\"), "rel": rel,
            "size": 2_000_000_000, "mtime": time.time() - age, "duration_s": mins * 60}


def test_bounty_folders_confirm_then_queue(client, conn):
    client.post("/campaigns", data={"name": "Man On", "bounty_name": "episode-4",
                                    "rate_usd": "125", "rate_mode": "100000"})
    client.post("/campaigns/1/bounties", data={"name": "episode-5", "rate_usd": "125",
                                               "rate_mode": "100000"})
    assert client.get("/api/worker/library/folders", headers=H).json() == {
        "folders": ["Man On/episode-4", "Man On/episode-5"]}
    _report(client, [_file("Man On/episode-4/a.mp4"),
                     _file("Man On/episode-4/unzipped/b.mp4"),  # subfolders count
                     _file("Man On/episode-4/c.mp4", age=5),  # still copying
                     _file("Man On/episode-5/ep5.mp4", mins=39)])
    page = client.get("/campaigns/1/bounties/2").text
    assert "1 video(s) in the folder" in page and "Queue footage for clipping" in page

    confirm = client.get("/campaigns/1/bounties/1/queue").text
    assert "unzipped/b.mp4" in confirm and "still downloading or copying" in confirm
    assert "2 videos, 1.3 h" in confirm  # only the ready files count
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0  # nothing yet

    b1 = conn.execute("SELECT * FROM bounties WHERE id=1").fetchone()
    paths = [f["path"] for f in library.files_for(conn, b1)]
    r = client.post("/campaigns/1/bounties/1/queue", data={"path": paths})
    assert "Queued 2 video(s)" in r.text  # the settling file was refused
    assert {r[0] for r in conn.execute("SELECT bounty_id FROM sources")} == {1}
    assert conn.execute("SELECT affinity_host FROM jobs").fetchone()[0] == "eastons-laptop"
    assert "Nothing queued" in client.post("/campaigns/1/bounties/1/queue",
                                           data={"path": paths}).text


def test_moved_file_keeps_its_source(client, conn):
    client.post("/campaigns", data={"name": "Man On", "bounty_name": "episode-4",
                                    "rate_usd": "125"})
    _report(client, [_file("ManOn/ep4.mp4")])
    conn.execute("UPDATE bounties SET media_folder='ManOn' WHERE id=1")
    client.post("/campaigns/1/bounties/1/queue", data={"path": [_file("ManOn/ep4.mp4")["path"]]})
    conn.execute("UPDATE sources SET media_host='eastons-laptop', media_path=url")
    conn.execute("UPDATE bounties SET media_folder='ManOn/episode-4' WHERE id=1")
    _report(client, [_file("ManOn/episode-4/ep4.mp4")])  # you moved it into the bounty folder
    src = conn.execute("SELECT * FROM sources").fetchone()
    assert src["media_path"].endswith("ManOn\\episode-4\\ep4.mp4")
    files = library.files_for(conn, conn.execute("SELECT * FROM bounties").fetchone())
    assert [f["queued"] for f in files] == [True]  # not offered for clipping again


def test_delete_campaign_removes_everything_it_made(client, conn, settings):
    from clipforge.worker import run_local
    client.post("/campaigns", data={"name": "Gone", "rate_usd": "1"})  # bounty "main"
    client.post("/sources", data={"campaign_id": "1", "urls": FIXTURE.resolve().as_uri()})
    run_local(["fetch", "transcribe", "select", "render"], once=True, idle_s=0)
    clip = conn.execute("SELECT * FROM clips LIMIT 1").fetchone()
    client.post(f"/clips/{clip['id']}/review", data={"decision": "approved"})
    client.post(f"/clips/{clip['id']}/post", data={"platform": "tiktok",
                                                   "url": "https://tiktok.com/@g/video/9"})
    assert (settings.clips_dir / "1").is_dir()

    r = client.post("/campaigns/1/delete")
    assert "Deleted Gone: 1 videos, 2 clips, 1 posts" in r.text
    for t in ("campaigns", "sources", "clips", "posts", "candidates", "transcripts", "jobs"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    assert not (settings.clips_dir / "1").exists() and FIXTURE.is_file()
    assert conn.execute("SELECT COUNT(*) FROM api_costs").fetchone()[0] > 0  # spend is history


def test_theme_and_old_sources_page(client):
    page = client.get("/review").text
    assert "localStorage.getItem('theme') || 'dark'" in page and "toggleTheme()" in page
    assert ">Sources<" not in page
    assert client.get("/sources", follow_redirects=False).headers["location"] == "/campaigns"


def test_delete_one_bounty_keeps_the_campaign(client, conn):
    client.post("/campaigns", data={"name": "Man On", "bounty_name": "episode-4",
                                    "rate_usd": "125"})
    client.post("/campaigns/1/bounties", data={"name": "episode-5", "rate_usd": "125"})
    client.post("/sources", data={"campaign_id": "1", "bounty_id": "2",
                                  "urls": "https://youtu.be/dQw4w9WgXcQ"})
    r = client.post("/campaigns/1/bounties/2/delete")
    assert "Deleted bounty episode-5: 1 videos" in r.text
    assert [r[0] for r in conn.execute("SELECT name FROM bounties")] == ["episode-4"]
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
