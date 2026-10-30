"""Live layout (D34): is this stretch of a stream a camera or a shared screen?

A center crop suits IRL camera footage (people fill the frame) but shows only the middle of a
shared screen (a Discord call, a browser, a game). Screens are made of flat panels: long runs of
pixels with the same brightness along a row. Camera footage almost never has them, even in dark
scenes, where compression flattens only short stretches. Measured on N3on's streams: screen
frames 0.22-0.46 of pixels in long flat runs, IRL frames 0.00-0.06.
"""

import subprocess

from clipforge.tools import ffmpeg

W, H = 320, 180
RUN = 48  # px at 320 wide: 15% of the row
SCREEN_AT = 0.12
SAMPLES = 5


def _frame(path: str, t: float) -> bytes | None:
    r = subprocess.run([ffmpeg(), "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                        "-vf", f"scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=60)
    return r.stdout if len(r.stdout) == W * H else None


def flat_share(px: bytes, w: int = W, h: int = H) -> float:
    """Share of pixels in long flat runs along rows (luma within 1 of the run's first pixel),
    leaving out black bars and blown-out white."""
    n = 0
    for y in range(0, h * w, w):
        x = 0
        while x < w:
            v, e = px[y + x], x
            while e + 1 < w and abs(px[y + e + 1] - v) <= 1:
                e += 1
            if e - x + 1 >= RUN and 16 <= v < 240:
                n += e - x + 1
            x = e + 1
    return n / (w * h)


def is_screen(path: str, start_ms: int, end_ms: int) -> bool:
    """Most sampled frames of the clip look like a shared screen."""
    dur = (end_ms - start_ms) / 1000
    shares = [flat_share(f) for i in range(SAMPLES)
              if (f := _frame(path, start_ms / 1000 + dur * (i + 0.5) / SAMPLES))]
    return bool(shares) and sorted(shares)[len(shares) // 2] >= SCREEN_AT


def choose(layout: str, path: str, start_ms: int, end_ms: int) -> str:
    """'auto' becomes blurfill (whole width) for a screen, crop for a camera."""
    if layout != "auto":
        return layout
    try:
        return "blurfill" if is_screen(path, start_ms, end_ms) else "crop"
    except (OSError, subprocess.SubprocessError):
        return "blurfill"  # can't tell: show the whole frame
