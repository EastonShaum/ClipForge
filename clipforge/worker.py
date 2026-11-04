"""Worker loops.

Local worker (on the Pi): claims jobs straight from SQLite.
Remote worker (laptop): pulls jobs from the Pi's HTTP API over Tailscale and pushes results
back. The Pi never connects to the laptop, so the laptop can come and go freely.
"""

import json
import logging
import os
import signal
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

from clipforge import jobqueue, library, pipeline, recorder, tools
from clipforge.config import get_settings
from clipforge.db import session
from clipforge.update import UPDATE_EXIT_CODE, local_version

log = logging.getLogger(__name__)
ALL_KINDS = ["fetch", "transcribe", "select", "render"]
# Bump when a remote worker must understand something new in a job context. The Pi refuses
# older workers, so an outdated laptop can't quietly render clips without a required logo.
# 2: campaign logos (ctx.logo_rel) and local-file sources.
# 3: several logos per campaign (ctx.logo_rels), drawn as a row under the captions.
# 4: render context has no words; hook card needs Pillow (D31).
# 5: select carries min_s/max_s, render carries layout (D33).
# 6: layout "auto": camera or shared screen, per clip (D34)
# 7: render carries "onscreen" (per-bounty #ad label) and new layouts (D38)
# 8: render carries "watermark" (logos as a corner watermark, D47)
WORKER_PROTOCOL = 9  # 9: render carries "words" for burned-in subtitles (D48)
LIBRARY_SCAN_S = 60
_stop = False


def _handle_stop(*_):
    global _stop
    _stop = True


def worker_name(host: str, label: str) -> str:
    return f"{host}-{label}-{os.getpid()}"


def run_local(kinds: list[str], label: str = "main", once: bool = False, idle_s: float = 5.0):
    s = get_settings()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    name = worker_name(s.host_name, label)
    log.info("local worker %s starting for %s", name, kinds)
    while not _stop:
        with session() as conn:
            jobqueue.heartbeat(conn, name, kinds)
            remote = jobqueue.remote_worker_alive(conn, s.host_name, s.laptop_grace_s)
            pinned_only = [k for k in s.prefer_remote_for if k in kinds] if remote else []
            job = jobqueue.claim(conn, name, s.host_name, kinds, pinned_only=pinned_only)
            if job:
                log.info("claimed job %s %s", job["id"], job["kind"])
                state = pipeline.run_local_job(conn, job, s)
                log.info("job %s -> %s", job["id"], state)
        if once and not job:
            return
        if not job:
            time.sleep(idle_s)


class Outdated(Exception):
    """The Pi runs different code than this worker; exit so the start script can update."""


class RemoteClient:
    def __init__(self, base_url: str, token: str):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=120,
                                 headers={"Authorization": f"Bearer {token}"})

    def claim(self, name: str, host: str, kinds: list[str]) -> dict | None:
        r = self.http.post("/api/worker/claim", json={"worker": name, "host": host,
                                                      "kinds": kinds,
                                                      "protocol": WORKER_PROTOCOL,
                                                      "version": local_version()})
        if r.status_code == 204:
            return None
        if r.status_code == 426:
            raise Outdated(r.text)
        r.raise_for_status()
        return r.json()

    def result(self, job_id: int, result: dict, file: Path | None = None) -> None:
        if file:
            with file.open("rb") as fh:
                r = self.http.post(f"/api/worker/jobs/{job_id}/result",
                                   data={"result": json.dumps(result)},
                                   files={"file": (file.name, fh, "video/mp4")}, timeout=600)
        else:
            r = self.http.post(f"/api/worker/jobs/{job_id}/result",
                               data={"result": json.dumps(result)})
        r.raise_for_status()

    def fetch_asset(self, rel: str, dest: Path) -> None:
        """Download a campaign asset (logo) once. Names carry a content hash, so a cached copy
        is never stale."""
        if dest.is_file():
            return
        r = self.http.get(f"/api/worker/assets/{rel}")
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(dest)

    def library(self, host: str, files: list[dict]) -> None:
        self.http.post("/api/worker/library",
                       json={"host": host, "files": files}).raise_for_status()

    def campaign_folders(self) -> list[str]:
        r = self.http.get("/api/worker/library/folders")
        r.raise_for_status()
        return r.json()["folders"]

    def live(self, host: str) -> dict:
        r = self.http.get("/api/worker/live", params={"host": host})
        r.raise_for_status()
        return r.json()

    def live_report(self, session_id: int, report: dict) -> None:
        self.http.post(f"/api/worker/live/{session_id}", json=report).raise_for_status()

    def defer(self, job_id: int, seconds: float, reason: str) -> None:
        self.http.post(f"/api/worker/jobs/{job_id}/defer",
                       json={"seconds": seconds, "reason": reason}).raise_for_status()

    def fail(self, job_id: int, error: str, cost: dict | None = None) -> None:
        self.http.post(f"/api/worker/jobs/{job_id}/fail",
                       json={"error": error, "cost": cost}).raise_for_status()


def run_remote(base_url: str, kinds: list[str], idle_s: float = 10.0):
    s = get_settings()
    signal.signal(signal.SIGINT, _handle_stop)
    host = s.host_name if s.host_name != "pi" else socket.gethostname().lower()
    name = worker_name(host, "remote")
    try:  # fail fast with a clear message instead of failing every job
        log.info("ffmpeg: %s, ffprobe: %s", tools.ffmpeg(), tools.ffprobe())
    except tools.ToolMissing as e:
        log.error("%s", e)
        sys.exit(2)
    client = RemoteClient(base_url, s.worker_token)
    log.info("remote worker %s -> %s for %s", name, base_url, kinds)
    log.info("media library: %s", s.media_library_dir)
    # Live streams record in their own thread (and HTTP client), so a long job can't stall them.
    rec_stop = threading.Event()
    threading.Thread(target=recorder.run, daemon=True, name="recorder",
                     args=(RemoteClient(base_url, s.worker_token), host, s.live_dir,
                           rec_stop)).start()
    log.info("live recordings: %s", s.live_dir)
    last_scan, last_report = 0.0, None
    while not _stop:
        if time.monotonic() - last_scan >= LIBRARY_SCAN_S:
            last_scan = time.monotonic()
            try:
                library.make_folders(s.media_library_dir, client.campaign_folders())
                files = library.scan(s.media_library_dir)
                report = [(f["rel"], f["size"], f["mtime"], f["duration_s"]) for f in files]
                if report != last_report:  # only send when something changed
                    client.library(host, files)
                    last_report = report
                    log.info("reported %d library files", len(files))
            except (OSError, httpx.HTTPError) as e:
                log.warning("library scan/report failed: %s", e)
        try:
            claimed = client.claim(name, host, kinds)
        except Outdated:
            log.warning("the Pi has newer code; exiting so the start script can update")
            sys.exit(UPDATE_EXIT_CODE)
        except httpx.HTTPError as e:
            log.warning("Pi unreachable (%s); retrying in 30 s", e)
            time.sleep(30)
            continue
        if not claimed:
            time.sleep(idle_s)
            continue
        job, ctx = claimed["job"], claimed["ctx"]
        log.info("claimed job %s %s", job["id"], job["kind"])
        try:
            for rel in ctx.get("logo_rels", []):
                client.fetch_asset(rel, s.assets_dir / rel)
            with tempfile.TemporaryDirectory() as tmp:
                result = pipeline.run_handler(s, job["kind"], ctx, out_dir=Path(tmp))
                upload = Path(result.pop("path")) if job["kind"] == "render" else None
                client.result(job["id"], result, upload)
            log.info("job %s done", job["id"])
        except pipeline.Deferred as e:
            client.defer(job["id"], e.seconds, str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job["id"])
            client.fail(job["id"], f"{type(e).__name__}: {e}", getattr(e, "meta", None))
