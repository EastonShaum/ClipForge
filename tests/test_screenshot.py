"""Tall screenshots are stored sharp and read in overlapping slices, so small text (a tag like
#coreboys) isn't shrunk into a misread."""

import base64
import subprocess

from clipforge.stages import screenshot


def _img(tmp_path, w, h):
    p = tmp_path / f"{w}x{h}.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=s={w}x{h}", "-frames:v", "1", str(p)], check=True)
    return p.read_bytes()


def test_slicing_plan():
    assert screenshot.plan(1568, 882) == (1568, [(0, 882)])  # landscape: one image
    assert screenshot.plan(900, 1200) == (900, [(0, 1200)])  # already within the limits
    width, parts = screenshot.plan(1170, 2532)  # a phone screenshot
    assert width == 1170 and len(parts) == 3
    assert all(width * h <= screenshot.MAX_PIXELS for _, h in parts)
    assert parts[-1][0] + parts[-1][1] == 2532  # reaches the bottom
    assert all(parts[i + 1][0] < parts[i][0] + parts[i][1] for i in range(len(parts) - 1))
    width, parts = screenshot.plan(1568, 40_000)  # absurdly tall: narrowed to fit
    assert len(parts) <= screenshot.MAX_TILES and width < 1568


def test_tall_page_is_stored_sharp_and_read_in_slices(tmp_path):
    _, path = screenshot.prepare_image(_img(tmp_path, 1920, 5130), tmp_path / "briefs")
    assert screenshot.size(path) == (1568, 4190)  # not shrunk to 1568 tall any more
    content = screenshot.content(path, "Read it.")
    imgs = [b for b in content if b["type"] == "image"]
    n = len(screenshot.plan(1568, 4190)[1])
    assert n == 7 and len(imgs) == n and "7 slices from top to bottom" in content[-1]["text"]
    assert content[-1]["text"].endswith("Read it.")
    first = tmp_path / "t0.jpg"
    first.write_bytes(base64.b64decode(imgs[0]["source"]["data"]))
    w, h = screenshot.size(first)
    assert w == 1568 and w * h <= screenshot.MAX_PIXELS
    _, wide = screenshot.prepare_image(_img(tmp_path, 1920, 1080), tmp_path / "scout")
    assert len([b for b in screenshot.content(wide, "x") if b["type"] == "image"]) == 1
