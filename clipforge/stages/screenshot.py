"""Screenshots for the readers (Scout and requirement briefs): stored sharp, read in slices.

The API reads an image at full detail only up to about 1.15 megapixels and 1568 px on the long
edge; anything bigger is shrunk first. A tall page (a phone screenshot, a full-page capture of a
brief) shrunk to fit that limit has unreadable text, and a tag like #coreboys came back as
#corebhoys. So a portrait screenshot is stored at up to 1568 px wide, whatever its height, and
read as overlapping slices top to bottom, each one within the limit. Landscape and square ones
are one image, as before."""

import base64
import hashlib
import subprocess
import tempfile
from pathlib import Path

from clipforge.tools import ffmpeg, ffprobe

MAX_EDGE = 1568  # px: the API's full-detail long edge
MAX_PIXELS = 1_150_000  # the API's full-detail area
MAX_TILES = 12  # a taller page is narrowed until it fits in this many slices
OVERLAP = 120  # px each slice repeats from the one above, so no line is only cut in half
MIN_TILE_H = 400


class NotAnImage(ValueError):
    pass


def prepare_image(data: bytes, dest_dir: Path) -> tuple[str, Path]:
    """Store as a JPEG named by content hash: landscape fitted to MAX_EDGE, portrait at up to
    MAX_EDGE wide at any height (read in slices). Returns (sha, path)."""
    sha = hashlib.sha256(data).hexdigest()[:16]
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{sha}-hd.jpg"  # "-hd": screenshots stored before slicing were shrunk
    if out.exists():
        return sha, out
    with tempfile.TemporaryDirectory(dir=dest_dir) as tmp:
        src = Path(tmp) / "in"
        src.write_bytes(data)
        part = Path(tmp) / "out.jpg"
        fit = f"scale='min({MAX_EDGE},iw)':-2"  # landscape: the long edge; portrait: the width
        proc = subprocess.run(
            [ffmpeg(), "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-vf", fit,
             "-q:v", "2", str(part)], capture_output=True, text=True)
        if proc.returncode or not part.exists():
            raise NotAnImage("not a readable image (use PNG or JPEG screenshots)")
        part.replace(out)  # atomic
    return sha, out


def size(path: Path) -> tuple[int, int]:
    r = subprocess.run([ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=60)
    w, h = r.stdout.strip().split(",")[:2]
    return int(w), int(h)


def plan(w: int, h: int) -> tuple[int, list[tuple[int, int]]]:
    """(width to read at, [(top, height)...]) for an image w×h. One slice when it already fits;
    otherwise slices within MAX_PIXELS, overlapping by OVERLAP, at most MAX_TILES of them."""
    if w * h <= MAX_PIXELS * 1.2 and max(w, h) <= MAX_EDGE or w >= h:
        return w, [(0, h)]
    width = w
    while True:
        scale = width / w
        hh = round(h * scale)
        tile = max(MIN_TILE_H, min(MAX_EDGE, MAX_PIXELS // width))
        step = tile - OVERLAP
        n = max(1, -(-(hh - OVERLAP) // step))
        if n <= MAX_TILES or width <= 600:
            break
        width = int(width * 0.85)
    tops = [min(i * step, max(0, hh - tile)) for i in range(n)]
    return width, [(t, min(tile, hh - t)) for t in dict.fromkeys(tops)]


def tiles(path: Path) -> list[bytes]:
    """The image as JPEG slices, top to bottom (one slice if it fits)."""
    w, h = size(path)
    width, parts = plan(w, h)
    if len(parts) == 1:
        return [path.read_bytes()]
    out = []
    for top, th in parts:
        r = subprocess.run(
            [ffmpeg(), "-v", "error", "-i", str(path), "-frames:v", "1", "-vf",
             f"scale={width}:-2,crop={width}:{th}:0:{top}", "-q:v", "2", "-f", "image2pipe",
             "-vcodec", "mjpeg", "-"], capture_output=True, timeout=60)
        if r.returncode or not r.stdout:
            raise RuntimeError(f"couldn't slice {path.name}: {r.stderr.decode()[-300:]}")
        out.append(r.stdout)
    return out


def content(path: Path, prompt: str) -> list[dict]:
    """The message content for a reader: the slices, then the prompt (told about the slices)."""
    parts = tiles(path)
    blocks = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                           "data": base64.standard_b64encode(b).decode()}}
              for b in parts]
    if len(parts) > 1:
        prompt = (f"The screenshot is a tall page, sent as {len(parts)} slices from top to "
                  "bottom. Each slice repeats the last few lines of the one before it. Read them "
                  "as one page and list each item once.\n\n" + prompt)
    return [*blocks, {"type": "text", "text": prompt}]
