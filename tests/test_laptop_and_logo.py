"""Local footage pinned to the machine that has it, campaign logos, and logo delivery to the
laptop worker."""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipforge import jobqueue
from clipforge.campaigns import add_logo
from clipforge.db import connect, init_db
from clipforge.ingest import add_source, source_key
from clipforge.stages.fetch import fetch, local_path
from clipforge.web.app import create_app
from clipforge.worker import run_local

FIXTURE = Path(__file__).parent / "fixtures" / "sample.mp4"
WIN = r"C:\Users\Easton\Clipping\Campaigns\ManOn\Man On_Ep4_Dennis_V2.mp4"
H = {"Authorization": "Bearer test-token"}


@pytest.mark.parametrize("url,path", [
    (WIN, WIN),
    ('"' + WIN + '"', WIN),  # Windows "Copy as path" adds quotes
    ("file:///C:/Users/E/ep%201.mp4", "C:/Users/E/ep 1.mp4"),
    ("file:///home/me/ep.mp4", "/home/me/ep.mp4"),
    ("/home/me/ep.mp4", "/home/me/ep.mp4"),
    ("https://youtu.be/dQw4w9WgXcQ", None),
])
def test_local_path(url, path):
    assert local_path(url) == path


def test_local_key_ignores_windows_case_and_slashes():
    assert source_key(WIN, "laptop") == source_key(WIN.lower().replace("\\", "/"), "laptop")
    assert source_key(WIN, "laptop") != source_key(WIN, "other-laptop")


def test_windows_path_needs_and_pins_to_the_laptop(conn, campaign_id):
    with pytest.raises(ValueError, match="no laptop worker"):
        add_source(conn, campaign_id, WIN)
    jobqueue.heartbeat(conn, "eastons-laptop-remote-42", ["fetch", "render"])
    sid, created = add_source(conn, campaign_id, WIN)
    assert created
    job = conn.execute("SELECT * FROM jobs WHERE source_id=?", (sid,)).fetchone()
    assert job["affinity_host"] == "eastons-laptop"
    # The Pi never claims it, even though it handles fetch jobs.
    assert jobqueue.claim(conn, "pi-light-1", "pi", ["fetch"]) is None
    assert jobqueue.claim(conn, "eastons-laptop-remote-42", "eastons-laptop", ["fetch"])


def test_local_file_is_used_in_place(settings):
    r = fetch(settings, 1, str(FIXTURE.resolve()))
    assert r["media_path"] == str(FIXTURE.resolve()) and 39 < r["duration_s"] < 41
    assert not any(settings.raw_dir.rglob("*.mp4"))  # nothing copied
    with pytest.raises(FileNotFoundError):
        fetch(settings, 2, "/nonexistent/ep.mp4")


def _red_png(path: Path) -> bytes:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=red:s=900x900",
                    "-frames:v", "1", str(path)], check=True)
    return path.read_bytes()


def _pixel(video: Path, t: float, x: int, y: int) -> tuple[int, int, int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(video),
                          "-frames:v", "1", "-vf", f"format=rgb24,crop=1:1:{x}:{y}", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return raw[0], raw[1], raw[2]


def test_logo_is_shrunk_and_burned_into_every_clip(conn, campaign_id, settings, tmp_path):
    from clipforge.stages.render import HOOK_X, LOGO_STRIP_H, placement

    rel = add_logo(conn, campaign_id, _red_png(tmp_path / "logo.png"))
    stored = settings.assets_dir / rel
    assert stored.is_file() and rel.startswith(f"logos/c{campaign_id}-")
    with pytest.raises(ValueError, match="not a readable image"):
        add_logo(conn, campaign_id, b"not an image")

    add_source(conn, campaign_id, str(FIXTURE.resolve()))
    run_local(["fetch", "transcribe", "select", "render"], once=True, idle_s=0)
    clip = conn.execute("SELECT path FROM clips LIMIT 1").fetchone()
    assert clip, conn.execute("SELECT last_error FROM jobs WHERE last_error IS NOT NULL").fetchall()
    video = settings.clips_dir / clip["path"]
    logo_bottom, hook_top = placement("blurfill", 640, 360)  # the fixture's size
    cx, cy = HOOK_X, logo_bottom - LOGO_STRIP_H // 2
    for t in (0.5, 6.0):  # present for the whole clip, not just the first frame
        r, g, b = _pixel(video, t, cx, cy)
        assert r > 200 and g < 60 and b < 60, (t, r, g, b)
    for t in (0.5, 6.0):  # the white hook card sits just under the picture for the whole clip
        r, g, b = _pixel(video, t, HOOK_X, hook_top + 8)
        assert min(r, g, b) > 220, (t, r, g, b)


def test_missing_logo_fails_the_render_instead_of_skipping(conn, campaign_id, settings,
                                                           tmp_path):
    rel = add_logo(conn, campaign_id, _red_png(tmp_path / "logo.png"))
    (settings.assets_dir / rel).unlink()
    add_source(conn, campaign_id, str(FIXTURE.resolve()))
    run_local(["fetch", "transcribe", "select", "render"], once=True, idle_s=0)
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 0
    err = conn.execute("SELECT last_error FROM jobs WHERE kind='render'").fetchone()[0]
    assert "logo" in err


def test_asset_api_requires_token_and_stays_in_assets(conn, campaign_id, tmp_path):
    rel = add_logo(conn, campaign_id, _red_png(tmp_path / "logo.png"))
    client = TestClient(create_app())
    assert client.get(f"/api/worker/assets/{rel}").status_code == 401
    r = client.get(f"/api/worker/assets/{rel}", headers=H)
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert client.get("/api/worker/assets/../clipforge.db", headers=H).status_code == 404
    assert client.get("/api/worker/assets/%2e%2e/clipforge.db", headers=H).status_code == 404


def test_outdated_remote_worker_is_refused(conn):
    client = TestClient(create_app())
    r = client.post("/api/worker/claim", headers=H, json={
        "worker": "old-remote-1", "host": "old", "kinds": ["render"]})
    assert r.status_code == 426 and "outdated" in r.text
    assert not conn.execute("SELECT 1 FROM workers WHERE name='old-remote-1'").fetchone()


def test_render_context_carries_logo_for_remote_worker(conn, campaign_id, tmp_path):
    rel = add_logo(conn, campaign_id, _red_png(tmp_path / "logo.png"))
    jobqueue.heartbeat(conn, "eastons-laptop-remote-42", ["fetch", "transcribe", "render"])
    client = TestClient(create_app())
    client.post("/sources", data={"campaign_id": str(campaign_id), "urls": WIN})
    r = client.post("/api/worker/claim", headers=H, json={
        "worker": "eastons-laptop-remote-42", "host": "eastons-laptop", "kinds": ["fetch"],
        "protocol": 4})
    assert r.status_code == 200 and r.json()["ctx"]["url"] == WIN
    # Skip ahead to a render job for this source and check its context.
    from clipforge.pipeline import build_context
    sid = r.json()["job"]["source_id"]
    conn.execute("UPDATE sources SET media_host='eastons-laptop', media_path=? WHERE id=?",
                 (WIN, sid))
    cid = conn.execute("INSERT INTO candidates (source_id, start_ms, end_ms, hook_text, hook_type,"
                       " score, rank, kept) VALUES (?,0,9000,'h','other',1,1,1)",
                       (sid,)).lastrowid
    ctx = build_context(conn, None, {"kind": "render", "candidate_id": cid})
    assert ctx["logo_rels"] == [rel] and ctx["media_path"] == WIN


def test_init_db_migrates_an_old_db_to_bounties(tmp_path):
    db = tmp_path / "old.db"
    c = connect(db)
    # The campaigns/sources tables as first shipped, before logos and bounties.
    c.executescript("""
        CREATE TABLE campaigns (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            rate_usd REAL NOT NULL DEFAULT 0, rate_per_views INTEGER NOT NULL DEFAULT 1000,
            min_views INTEGER NOT NULL DEFAULT 0, cap_usd REAL, end_date TEXT,
            status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE sources (id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL,
            url TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE);
        INSERT INTO campaigns (name, rate_usd, rate_per_views, end_date)
            VALUES ('Man On – episode-4', 125, 100000, '2026-11-30');
        INSERT INTO sources (campaign_id, url, source_key) VALUES (1, 'C:\\x.mp4', 'file:1');
    """)
    c.close()
    init_db(db)
    init_db(db)  # idempotent: one bounty, not two
    c = connect(db)
    assert "logo_path" in {r["name"] for r in c.execute("PRAGMA table_info(campaigns)")}
    b = c.execute("SELECT * FROM bounties").fetchall()
    assert len(b) == 1 and b[0]["name"] == "main" and b[0]["rate_usd"] == 125
    assert b[0]["end_date"] == "2026-11-30"
    assert c.execute("SELECT bounty_id FROM sources").fetchone()[0] == b[0]["id"]


def test_filter_path_escapes_windows_drive_for_both_ffmpeg_levels():
    from clipforge.stages.render import _filter_path
    assert _filter_path(Path(r"C:\Users\E\cap.ass")) == r"C\\:/Users/E/cap.ass"


def test_render_works_when_paths_contain_filter_syntax(settings, tmp_path):
    # A Linux stand-in for a Windows path: ':' and ',' in the caption and font dirs.
    from clipforge.stages.render import render
    work = tmp_path / "C:odd,dir"
    out = render(settings, media_path=str(FIXTURE), start_ms=1000, end_ms=3000, hook="Hook 🎃",
                 out_path=tmp_path / "out.mp4", work=work, disclosure="Paid partnership #ad")
    assert Path(out["path"]).stat().st_size > 0


def test_two_logos_render_side_by_side_and_can_be_removed(conn, campaign_id, settings, tmp_path):
    from clipforge.campaigns import logo_list, remove_logo
    from clipforge.stages.render import HOOK_X, LOGO_STRIP_H, placement

    red = add_logo(conn, campaign_id, _red_png(tmp_path / "red.png"))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=900x900",
                    "-frames:v", "1", str(tmp_path / "blue.png")], check=True)
    blue = add_logo(conn, campaign_id, (tmp_path / "blue.png").read_bytes())
    add_logo(conn, campaign_id, (tmp_path / "blue.png").read_bytes())  # same file: no duplicate
    row = conn.execute("SELECT logo_path FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[0]
    assert logo_list(row) == [red, blue]

    add_source(conn, campaign_id, str(FIXTURE.resolve()))
    run_local(["fetch", "transcribe", "select", "render"], once=True, idle_s=0)
    video = settings.clips_dir / conn.execute("SELECT path FROM clips LIMIT 1").fetchone()["path"]
    cy = placement("blurfill", 640, 360)[0] - LOGO_STRIP_H // 2
    r, g, b = _pixel(video, 1.0, HOOK_X - 80, cy)  # left logo: red
    assert r > 200 and b < 60, (r, g, b)
    r, g, b = _pixel(video, 1.0, HOOK_X + 80, cy)  # right logo: blue
    assert b > 200 and r < 60, (r, g, b)

    remove_logo(conn, campaign_id, red)
    row = conn.execute("SELECT logo_path FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[0]
    assert logo_list(row) == [blue]
    remove_logo(conn, campaign_id)
    assert conn.execute("SELECT logo_path FROM campaigns WHERE id=?",
                        (campaign_id,)).fetchone()[0] is None
