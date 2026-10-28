"""Live recorder (D30). Runs on the laptop beside the remote worker, in its own thread: asks the
Pi which live sessions to record, records each with ffmpeg as 2-minute .ts pieces, and lists
finished pieces in .ffconcat chunks (ffmpeg reads one as a single video) for the Pi to clip.
Touches no DB; everything goes through the worker API.

A session folder under live_dir holds p00000.ts, p00001.ts…, the chunk files, and
chunks.json (piece durations and how many pieces are already in a chunk)."""

import atexit
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from clipforge.tools import ffmpeg, ffprobe

log = logging.getLogger(__name__)

PIECE_RE = re.compile(r"^p(\d{5})\.ts$")
QUIET_S = 30  # a piece untouched this long is finished (or its recorder is gone)
MIN_LAST_S = 60  # a stream's last scrap shorter than this isn't worth clipping
RETRY_S = 60  # wait between attempts to (re)start a recording
SYNC_S = 30


def pieces(d: Path) -> list[Path]:
    return sorted((p for p in d.glob("p*.ts") if PIECE_RE.match(p.name)), key=lambda p: p.name)


def busy(d: Path) -> bool:
    """Something is writing here (maybe a recorder left over from before a restart)."""
    ps = pieces(d)
    return bool(ps) and time.time() - ps[-1].stat().st_mtime < QUIET_S


def finished(d: Path, recording: bool) -> list[Path]:
    ps = pieces(d)
    return ps[:-1] if ps and (recording or busy(d)) else ps


def plan(durs: list[float], done: int, chunk_s: float, overlap_s: float,
         final: bool) -> tuple[int, int] | None:
    """The next chunk as (first, end) piece indexes, or None. Pieces [done, end) are new, about
    chunk_s long (all that's left when final); pieces [first, done) repeat at least overlap_s of
    the previous chunk."""
    pending = sum(durs[done:])
    if not pending or (pending < chunk_s and not (final and pending >= MIN_LAST_S)):
        return None
    end, acc = done, 0.0
    while end < len(durs) and acc < chunk_s:
        acc += durs[end]
        end += 1
    if final and sum(durs[end:]) < MIN_LAST_S:
        end = len(durs)  # fold a tiny tail into this chunk
    first, ov = done, 0.0
    while first > 0 and ov < overlap_s:
        first -= 1
        ov += durs[first]
    return first, end


def _hm(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h{m % 60:02d}"


def _probe(p: Path) -> float:
    r = subprocess.run([ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of",
                        "csv=p=0", str(p)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0  # unreadable piece: left out of chunks


def make_chunks(d: Path, name: str, recording: bool, final: bool, chunk_s: float,
                overlap_s: float) -> dict:
    """Write any chunks that are due. Returns every chunk of the session so far, and how much
    is recorded."""
    state_file = d / "chunks.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {
        "durs": {}, "done": 0, "chunks": []}
    done_pieces = finished(d, recording)
    for p in done_pieces:
        if p.name not in state["durs"]:
            state["durs"][p.name] = _probe(p)
    names = [p.name for p in done_pieces]
    durs = [state["durs"][n] for n in names]
    while nxt := plan(durs, state["done"], chunk_s, overlap_s, final):
        first, end = nxt
        start_s, end_s = sum(durs[:state["done"]]), sum(durs[:end])
        path = d / f"{name} #{len(state['chunks']) + 1:02d} {_hm(start_s)}-{_hm(end_s)}.ffconcat"
        lines = ["ffconcat version 1.0"]
        for n, dur in zip(names[first:end], durs[first:end], strict=True):
            if dur > 0:
                lines += [f"file '{n}'", f"duration {dur:.6f}"]
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(path)  # atomic: the Pi may queue it as soon as it's reported
        overlap = sum(d_ for d_ in durs[first:state["done"]] if d_ > 0)
        state["chunks"].append({"path": str(path), "overlap_ms": int(overlap * 1000)})
        state["done"] = end
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(state_file)
    return {"chunks": state["chunks"], "recorded_s": sum(durs),
            "bytes": sum(p.stat().st_size for p in done_pieces)}


def start(url: str, d: Path, piece_s: int, max_height: int) -> subprocess.Popen:
    """Find the stream's HLS URL with yt-dlp, then record it with ffmpeg (no re-encode)."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "format": f"best[height<={max_height}]/best"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    ps = pieces(d)
    nxt = int(PIECE_RE.match(ps[-1].name).group(1)) + 1 if ps else 0
    headers = "".join(f"{k}: {v}\r\n" for k, v in (info.get("http_headers") or {}).items())
    cmd = [ffmpeg(), "-v", "error", *(["-headers", headers] if headers else []),
           "-i", info["url"], "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
           "-f", "segment", "-segment_time", str(piece_s), "-segment_start_number", str(nxt),
           "-reset_timestamps", "1", "-segment_format", "mpegts", str(d / "p%05d.ts")]
    with (d / "ffmpeg.log").open("ab") as err:
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=err)


def stop(proc: subprocess.Popen) -> None:
    """Ask ffmpeg to finish ("q") so the last piece is readable; force it after 15 s."""
    if proc.poll() is not None:
        return
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
        proc.wait(15)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
        proc.wait(5)


def safe_dir(root: Path, rel: str) -> Path | None:
    parts = (rel or "").split("/")
    if any(p in ("", ".", "..") or any(c in p for c in '\\:') for p in parts):
        return None
    return root.joinpath(*parts)


class Recorder:
    def __init__(self, root: Path):
        self.root = root
        self.procs: dict[int, subprocess.Popen] = {}
        self.retry_at: dict[int, float] = {}
        self.errors: dict[int, str] = {}

    def sync(self, plan_: dict) -> list[dict]:
        """Start, keep alive or stop recordings to match the Pi's list. Returns reports."""
        reports = []
        listed = {s["id"] for s in plan_["sessions"]}
        for sid in [k for k in self.procs if k not in listed]:
            stop(self.procs.pop(sid))
        for sess in plan_["sessions"]:
            d = safe_dir(self.root, sess["dir"])
            if d is None:
                continue
            sid = sess["id"]
            proc = self.procs.get(sid)
            if not sess["live"]:
                if proc:
                    stop(self.procs.pop(sid))
                    proc = None
            elif (proc is None or proc.poll() is not None) and time.time() >= \
                    self.retry_at.get(sid, 0):
                d.mkdir(parents=True, exist_ok=True)
                if not busy(d):
                    try:
                        self.procs[sid] = proc = start(sess["url"], d, plan_["piece_s"],
                                                       plan_["max_height"])
                        self.errors.pop(sid, None)
                        log.info("recording %s -> %s", sess["url"], d)
                    except Exception as e:  # noqa: BLE001 - offline between checks, etc.
                        self.errors[sid] = f"{type(e).__name__}: {e}"[:300]
                    self.retry_at[sid] = time.time() + RETRY_S
            recording = proc is not None and proc.poll() is None
            final = not sess["live"] and not busy(d)
            got = (make_chunks(d, sess["name"], recording, final, plan_["chunk_s"],
                               plan_["overlap_s"]) if d.is_dir()
                   else {"chunks": [], "recorded_s": 0, "bytes": 0})
            reports.append({"id": sid, "recording": recording, "final": final,
                            "error": self.errors.get(sid), **got})
        for rel in plan_.get("purge", []):
            d = safe_dir(self.root, rel)
            if d is not None and d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                log.info("deleted old recording %s", d)
        return reports

    def stop_all(self) -> None:
        for sid in list(self.procs):
            stop(self.procs.pop(sid))


def run(client, host: str, root: Path, stop_event: threading.Event) -> None:
    """The recorder thread: sync with the Pi every SYNC_S until stop_event is set."""
    rec = Recorder(root)
    atexit.register(rec.stop_all)  # a self-update restart finishes the current piece cleanly
    while not stop_event.is_set():
        try:
            for r in rec.sync(client.live(host)):
                client.live_report(r.pop("id"), r)
        except Exception:  # noqa: BLE001 - keep recording through Pi or network trouble
            log.warning("live recorder sync failed", exc_info=True)
        stop_event.wait(SYNC_S)
    rec.stop_all()
