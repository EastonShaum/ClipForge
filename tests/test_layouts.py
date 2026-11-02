"""Render layouts (D38): speaker crops, two speakers stacked, facecam over gameplay, and
"vary" taking turns, so an account's clips don't all share one template."""

import subprocess

import pytest

from clipforge.config import get_settings
from clipforge.pipeline import build_context, layout_for
from clipforge.stages import render
from tests.test_e2e import FIXTURE


@pytest.mark.parametrize("layout", ["speaker-left", "speaker-right", "split", "facecam:br",
                                    "facecam:60,5,35,30"])
def test_each_layout_renders_a_vertical_clip(layout, tmp_path, settings):
    out = tmp_path / "c.mp4"
    render.render(settings, media_path=str(FIXTURE), start_ms=1000, end_ms=3000, hook="",
                  out_path=out, work=tmp_path / "w", layout=layout)
    size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=width,height", "-of", "csv=p=0", str(out)],
                          capture_output=True, text=True).stdout.strip()
    assert size == "1080,1920"


def test_facecam_rectangle_must_fit_the_frame():
    assert render.facecam_rect("tr") == (0.72, 0.0, 0.28, 0.30)
    with pytest.raises(ValueError):
        render.facecam_rect("80,0,40,30")


def test_layout_comes_from_the_bounty_then_the_streamer(client, conn, campaign_id):
    src = {"bounty_layout": None, "facecam": None}
    assert layout_for(None, src, None, 1) is None  # render default
    assert layout_for(None, src, {"live_layout": None, "facecam": None}, 1) == "auto"
    live = {"live_layout": "vary", "facecam": "tr"}
    assert {layout_for(None, src, live, i) for i in range(3)} == {
        "blurfill", "crop", "facecam:tr"}
    assert layout_for(None, {"bounty_layout": "split", "facecam": None}, live, 1) == "split"

    bid = conn.execute("SELECT id FROM bounties").fetchone()[0]
    r = client.post(f"/campaigns/{campaign_id}/bounties/{bid}/settings",
                    data={"layout": "facecam", "facecam": "90,90,20,20", "rate_usd": "1"})
    assert "out of the frame" in r.text
    client.post(f"/campaigns/{campaign_id}/bounties/{bid}/settings",
                data={"layout": "facecam", "facecam": "bl", "rate_usd": "1"})
    sid = conn.execute("INSERT INTO sources (campaign_id, bounty_id, url, source_key) VALUES"
                       " (?,?,'u','k')", (campaign_id, bid)).lastrowid
    cand = conn.execute("INSERT INTO candidates (source_id, start_ms, end_ms, hook_text, score,"
                        " rank) VALUES (?, 0, 9000, 'h', 50, 1)", (sid,)).lastrowid
    ctx = build_context(conn, get_settings(), {"kind": "render", "candidate_id": cand})
    assert ctx["layout"] == "facecam:bl"
