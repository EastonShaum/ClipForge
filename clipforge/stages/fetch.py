"""Fetch stage: download source media with yt-dlp, guarded by disk limits."""

import shutil
from pathlib import Path

from clipforge.config import Settings


class DiskFull(RuntimeError):
    pass


def dir_size_gb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e9


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def check_disk(s: Settings, incoming_gb: float = 0.0) -> None:
    if free_gb(s.raw_dir) - incoming_gb < s.min_free_gb:
        raise DiskFull(f"only {free_gb(s.raw_dir):.1f} GB free (min {s.min_free_gb} GB)")
    if dir_size_gb(s.raw_dir) + incoming_gb > s.media_cap_gb:
        raise DiskFull(f"raw media cap {s.media_cap_gb} GB reached")


def fetch(s: Settings, source_id: int, url: str) -> dict:
    """Download one source. Idempotent: an existing finished file is reused."""
    dest = s.raw_dir / str(source_id)
    dest.mkdir(parents=True, exist_ok=True)
    done = [p for p in dest.glob("media.*") if not p.name.endswith((".part", ".ytdl"))]
    if done and (dest / "info.ok").exists():
        info = (dest / "info.ok").read_text().split("\n", 1)
        return {"media_path": str(done[0]), "duration_s": float(info[0] or 0),
                "title": info[1] if len(info) > 1 else ""}

    import yt_dlp  # heavy import, only when actually fetching

    # file:// lets you queue footage you downloaded by hand (e.g. from a campaign Drive folder).
    local = url.startswith("file://")
    probe_opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                  "enable_file_urls": local}
    with yt_dlp.YoutubeDL(probe_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    duration = float(info.get("duration") or 0)
    # Rough size estimate: ~1.5 GB/hour at 1080p.
    check_disk(s, incoming_gb=1.5 * duration / 3600)

    h = s.max_download_height
    opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(dest / "media.%(ext)s"),
        "format": "b" if local else
        f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/b",
        "merge_output_format": "mp4",
        "retries": 5,
        "fragment_retries": 5,
        "noplaylist": True,
        "enable_file_urls": local,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = [p for p in dest.glob("media.*") if not p.name.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError("yt-dlp finished but no media file found")
    title = info.get("title") or ""
    (dest / "info.ok").write_text(f"{duration}\n{title}")
    return {"media_path": str(files[0]), "duration_s": duration, "title": title}


def cleanup_raw(s: Settings, source_ids: list[int]) -> int:
    """Delete raw media directories for the given sources. Returns bytes freed."""
    freed = 0
    for sid in source_ids:
        d = s.raw_dir / str(sid)
        if d.exists():
            freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d)
    return freed
