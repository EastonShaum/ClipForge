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
    made = library.make_folders(tmp_path, ["Man On – episode-4", "..", "a/b", "C:x", ""])
    assert made == ["Man On – episode-4"] and (tmp_path / "Man On – episode-4").is_dir()
    assert library.make_folders(tmp_path, ["Man On – episode-4"]) == []


def _report(client, files):
    return client.post("/api/worker/library", headers=H,
                       json={"host": "eastons-laptop", "files": files})


def _file(rel, mins=40, age=3600):
    return {"path": "C:\\Users\\E\\CLipping\\Campaigns\\" + rel.replace("/", "\\"), "rel": rel,
            "size": 2_000_000_000, "mtime": time.time() - age, "duration_s": mins * 60}


def test_confirm_then_queue_and_folder_matching(client, conn):
    client.post("/campaigns", data={"name": "Brandon Joe Williams – contract-killer",
                                    "rate_usd": "150", "rate_mode": "100000"})
    assert client.get("/api/worker/library/folders", headers=H).json() == {
        "folders": ["Brandon Joe Williams – contract-killer"]}
    # An older campaign without a set folder matches by name (ManOn ~ "Man On – episode-4").
    conn.execute("INSERT INTO campaigns (name, media_folder) VALUES ('Man On – episode-4', NULL)")
    _report(client, [_file("Brandon Joe Williams – contract-killer/a.mp4"),
                     _file("Brandon Joe Williams – contract-killer/b.mp4", age=5),
                     _file("ManOn/ep4.mp4", mins=39)])
    page = client.get("/campaigns/2").text
    assert "1 video(s) in the folder" in page and "Queue footage for clipping" in page

    confirm = client.get("/campaigns/1/queue").text
    assert "a.mp4" in confirm and "still downloading or copying" in confirm
    assert "1 videos, 0.7 h" in confirm  # only the ready file counts
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0  # nothing yet

    paths = [f["path"] for f in library.files_for(conn, conn.execute(
        "SELECT * FROM campaigns WHERE id=1").fetchone())]
    r = client.post("/campaigns/1/queue", data={"path": paths})
    assert "Queued 1 video(s)" in r.text  # the settling file was refused
    job = conn.execute("SELECT * FROM jobs WHERE kind='fetch'").fetchone()
    assert job["affinity_host"] == "eastons-laptop"
    assert "Queued 1" not in client.post("/campaigns/1/queue", data={"path": paths}).text


def test_delete_campaign_removes_everything_it_made(client, conn, settings):
    from clipforge.worker import run_local
    client.post("/campaigns", data={"name": "Gone", "rate_usd": "1"})
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
