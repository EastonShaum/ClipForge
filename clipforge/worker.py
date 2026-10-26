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
import tempfile
import time
from pathlib import Path

import httpx

from clipforge import jobqueue, pipeline
from clipforge.config import get_settings
from clipforge.db import session

log = logging.getLogger(__name__)
ALL_KINDS = ["fetch", "transcribe", "select", "render"]
# Bump when a remote worker must understand something new in a job context. The Pi refuses
# older workers, so an outdated laptop can't quietly render clips without a required logo.
# 2: campaign logos (ctx.logo_rel) and local-file sources.
WORKER_PROTOCOL = 2
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


class RemoteClient:
    def __init__(self, base_url: str, token: str):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=120,
                                 headers={"Authorization": f"Bearer {token}"})

    def claim(self, name: str, host: str, kinds: list[str]) -> dict | None:
        r = self.http.post("/api/worker/claim", json={"worker": name, "host": host,
                                                      "kinds": kinds,
                                                      "protocol": WORKER_PROTOCOL})
        if r.status_code == 204:
            return None
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

    def defer(self, job_id: int, seconds: float, reason: str) -> None:
        self.http.post(f"/api/worker/jobs/{job_id}/defer",
                       json={"seconds": seconds, "reason": reason}).raise_for_status()

    def fail(self, job_id: int, error: str) -> None:
        self.http.post(f"/api/worker/jobs/{job_id}/fail",
                       json={"error": error}).raise_for_status()


def run_remote(base_url: str, kinds: list[str], idle_s: float = 10.0):
    s = get_settings()
    signal.signal(signal.SIGINT, _handle_stop)
    host = s.host_name if s.host_name != "pi" else socket.gethostname().lower()
    name = worker_name(host, "remote")
    client = RemoteClient(base_url, s.worker_token)
    log.info("remote worker %s -> %s for %s", name, base_url, kinds)
    while not _stop:
        try:
            claimed = client.claim(name, host, kinds)
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
            if ctx.get("logo_rel"):
                client.fetch_asset(ctx["logo_rel"], s.assets_dir / ctx["logo_rel"])
            with tempfile.TemporaryDirectory() as tmp:
                result = pipeline.run_handler(s, job["kind"], ctx, out_dir=Path(tmp))
                upload = Path(result.pop("path")) if job["kind"] == "render" else None
                client.result(job["id"], result, upload)
            log.info("job %s done", job["id"])
        except pipeline.Deferred as e:
            client.defer(job["id"], e.seconds, str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job["id"])
            client.fail(job["id"], f"{type(e).__name__}: {e}")
