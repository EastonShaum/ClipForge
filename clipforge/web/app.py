"""FastAPI app: mobile-first review UI, campaign/source forms, dashboard, health, worker API."""

import json
import logging
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from clipforge import analytics, costs, jobqueue, pipeline
from clipforge.campaigns import CampaignIn, add_campaign, clear_logo, list_campaigns, set_logo
from clipforge.captions import PLATFORM_REMINDER
from clipforge.config import get_settings
from clipforge.db import connect, init_db, tx, utcnow
from clipforge.feedback import REJECT_REASONS
from clipforge.ingest import add_source, remote_hosts
from clipforge.select_prompts import sync_prompts
from clipforge.stages.fetch import dir_size_gb, free_gb
from clipforge.update import code_zip, repo_version
from clipforge.worker import WORKER_PROTOCOL

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["money"] = lambda v: f"${v or 0:,.2f}"
templates.env.filters["num"] = lambda v: f"{v or 0:,}"
templates.env.filters["pct"] = lambda v: "—" if v is None else f"{v * 100:.0f}%"
templates.env.filters["secs"] = lambda ms: f"{(ms or 0) / 1000:.0f}s"
templates.env.filters["mmss"] = lambda ms: f"{(ms or 0) // 60000}:{(ms or 0) // 1000 % 60:02d}"

PLATFORMS = ["tiktok", "youtube", "instagram", "x"]


def create_app() -> FastAPI:
    s = get_settings()
    init_db()
    conn = connect()
    sync_prompts(conn)
    conn.close()
    app = FastAPI(title="ClipForge", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/media/clips", StaticFiles(directory=s.clips_dir), name="clips")
    app.mount("/media/assets", StaticFiles(directory=s.assets_dir), name="assets")
    _routes(app)
    return app


def db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


DB = Annotated[sqlite3.Connection, Depends(db)]


def page(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {"active": name.split(".")[0], **ctx})


def back(url: str, msg: str = "") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}msg={quote(msg)}" if msg else url, status_code=303)


def _clip_rows(conn, status: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT k.*, c.hook_text, c.hook_type, c.title, c.rationale, c.score, c.start_ms,
                  c.end_ms, c.source_id, s.title src_title, s.url src_url, cp.name campaign,
                  cp.id campaign_id, cp.allowed_platforms, pv.name prompt
           FROM clips k JOIN candidates c ON c.id=k.candidate_id JOIN sources s ON s.id=c.source_id
           JOIN campaigns cp ON cp.id=s.campaign_id
           LEFT JOIN prompt_versions pv ON pv.id=c.prompt_version_id
           WHERE k.status=? ORDER BY c.score DESC, k.id LIMIT ?""", (status, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["posts"] = [dict(p) for p in conn.execute(
            "SELECT * FROM posts WHERE clip_id=? ORDER BY id", (r["id"],))]
        out.append(d)
    return out


def _routes(app: FastAPI) -> None:  # noqa: C901 - flat list of routes reads best
    s = get_settings()
    # The commit this process loaded. Remote workers must run exactly this (see update.py).
    code_version = repo_version() if s.require_worker_version else None

    @app.get("/health")
    def health(conn: DB):
        conn.execute("SELECT 1").fetchone()
        b = costs.budget_status(conn)
        workers = [dict(r) for r in conn.execute("SELECT * FROM workers ORDER BY last_seen DESC")]
        return {
            "ok": True, "time": utcnow(), "jobs": jobqueue.counts(conn),
            "disk_free_gb": round(free_gb(s.data_dir), 2),
            "raw_media_gb": round(dir_size_gb(s.raw_dir), 2), "budget": b, "workers": workers,
            "remote_worker_online": jobqueue.remote_worker_alive(conn, s.host_name,
                                                                 s.laptop_grace_s),
        }

    # ---------- review ----------
    @app.get("/")
    def home():
        return RedirectResponse("/review")

    @app.get("/review")
    def review(request: Request, conn: DB, msg: str = ""):
        return page(request, "review.html", clips=_clip_rows(conn, "in_review"),
                    reasons=REJECT_REASONS, msg=msg)

    @app.post("/clips/{clip_id}/review")
    def decide(clip_id: int, conn: DB, decision: str = Form(...),
               reason: list[str] = Form([]), note: str = Form("")):
        if decision not in ("approved", "rejected"):
            raise HTTPException(400, "bad decision")
        # Several presets may be ticked; stored comma-joined, unknown keys dropped.
        reasons = ",".join(r for r in reason if r in REJECT_REASONS)
        note = note.strip()
        if decision == "rejected" and not (reasons or note):
            return back("/review", "Pick a reason or write a note so the AI can learn from it")
        with tx(conn):
            cur = conn.execute("UPDATE clips SET status=? WHERE id=? AND status='in_review'",
                               (decision, clip_id))
            if cur.rowcount:
                conn.execute("INSERT INTO reviews (clip_id, decision, reason, note)"
                             " VALUES (?,?,?,?)",
                             (clip_id, decision, reasons or None, note or None))
        return back("/review", "Approved" if decision == "approved" else "Rejected")

    @app.get("/approved")
    def approved(request: Request, conn: DB, msg: str = ""):
        return page(request, "approved.html", clips=_clip_rows(conn, "approved", 100),
                    posted=_clip_rows(conn, "posted", 30), platforms=PLATFORMS,
                    reminders=PLATFORM_REMINDER, msg=msg)

    @app.post("/clips/{clip_id}/caption")
    def edit_caption(clip_id: int, conn: DB, caption: str = Form(...)):
        conn.execute("UPDATE clips SET caption=? WHERE id=?", (caption, clip_id))
        return back("/approved", "Caption saved")

    @app.post("/clips/{clip_id}/post")
    def log_post(clip_id: int, conn: DB, platform: str = Form(...), url: str = Form(...)):
        if platform not in PLATFORMS or not url.startswith("http"):
            return back("/approved", "Pick a platform and paste the full post URL")
        try:
            with tx(conn):
                conn.execute("INSERT INTO posts (clip_id, platform, url) VALUES (?,?,?)",
                             (clip_id, platform, url.strip()))
                conn.execute("UPDATE clips SET status='posted' WHERE id=?", (clip_id,))
        except sqlite3.IntegrityError:
            return back("/approved", "That URL is already logged")
        return back("/approved", f"Logged {platform} post")

    @app.post("/posts/{post_id}/submitted")
    def mark_submitted(post_id: int, conn: DB, undo: str = Form("")):
        conn.execute("UPDATE posts SET submitted_at=? WHERE id=?",
                     (None if undo else utcnow(), post_id))
        return back("/approved", "Marked not submitted" if undo else "Marked submitted")

    @app.post("/clips/{clip_id}/unreview")
    def unreview(clip_id: int, conn: DB):
        conn.execute("UPDATE clips SET status='in_review' WHERE id=? AND status IN"
                     " ('approved','rejected')", (clip_id,))
        return back("/review", "Moved back to review")

    # ---------- posts & views ----------
    @app.get("/posts")
    def posts(request: Request, conn: DB, msg: str = ""):
        rows = conn.execute(
            """SELECT p.*, c.hook_text, cp.name campaign, cp.min_views,
                      (SELECT MAX(views) FROM view_snapshots v WHERE v.post_id=p.id) views,
                      (SELECT MAX(taken_at) FROM view_snapshots v WHERE v.post_id=p.id) checked
               FROM posts p JOIN clips k ON k.id=p.clip_id JOIN candidates c ON c.id=k.candidate_id
               JOIN sources s ON s.id=c.source_id JOIN campaigns cp ON cp.id=s.campaign_id
               ORDER BY p.posted_at DESC LIMIT 200""").fetchall()
        return page(request, "posts.html", posts=rows, msg=msg)

    @app.post("/posts/{post_id}/views")
    def add_views(post_id: int, conn: DB, views: int = Form(...), likes: str = Form("")):
        conn.execute("INSERT INTO view_snapshots (post_id, views, likes) VALUES (?,?,?)",
                     (post_id, views, int(likes) if likes.strip().isdigit() else None))
        return back("/posts", "Views saved")

    # ---------- campaigns & sources ----------
    @app.get("/campaigns")
    def campaigns(request: Request, conn: DB, msg: str = ""):
        return page(request, "campaigns.html", campaigns=list_campaigns(conn), msg=msg)

    @app.post("/campaigns")
    async def create_campaign(request: Request, conn: DB):
        form = dict(await request.form())
        data = {k: v for k, v in form.items() if v not in ("", None)}
        if "rate_mode" in data:  # UI lets you enter "$40 per 100K" style rates
            data["rate_per_views"] = int(data.pop("rate_mode"))
        force = data.pop("force", None) == "on"
        try:
            cid = add_campaign(conn, CampaignIn(**data), force=force)
        except (ValueError, TypeError) as e:
            return back("/campaigns", str(e).split("\n")[0][:200])
        return back("/campaigns", f"Campaign #{cid} added")

    @app.post("/campaigns/{cid}/status")
    def campaign_status(cid: int, conn: DB, status: str = Form(...)):
        conn.execute("UPDATE campaigns SET status=? WHERE id=?", (status, cid))
        return back("/campaigns", "Updated")

    @app.post("/campaigns/{cid}/end-date")
    def campaign_end_date(cid: int, conn: DB, end_date: str = Form("")):
        conn.execute("UPDATE campaigns SET end_date=? WHERE id=?", (end_date or None, cid))
        return back("/campaigns", "End date saved" if end_date else "End date cleared")

    @app.post("/campaigns/{cid}/logo")
    async def campaign_logo(cid: int, conn: DB, logo: UploadFile = File(...)):
        try:
            set_logo(conn, cid, await logo.read())
        except ValueError as e:
            return back("/campaigns", str(e))
        return back("/campaigns", "Logo saved. New renders will include it.")

    @app.post("/campaigns/{cid}/logo/remove")
    def campaign_logo_remove(cid: int, conn: DB):
        clear_logo(conn, cid)
        return back("/campaigns", "Logo removed")

    @app.get("/sources")
    def sources(request: Request, conn: DB, msg: str = ""):
        rows = conn.execute(
            """SELECT s.*, c.name campaign,
                      (SELECT last_error FROM jobs j WHERE j.source_id=s.id AND j.last_error
                       IS NOT NULL ORDER BY j.updated_at DESC LIMIT 1) last_error,
                      (SELECT COUNT(*) FROM candidates WHERE source_id=s.id AND kept=1) kept
               FROM sources s JOIN campaigns c ON c.id=s.campaign_id
               ORDER BY s.id DESC LIMIT 100""").fetchall()
        return page(request, "sources.html", sources=rows,
                    campaigns=list_campaigns(conn, "active"),
                    hosts=[s.host_name, *remote_hosts(conn)], msg=msg)

    @app.post("/sources")
    def create_source(conn: DB, campaign_id: int = Form(...), urls: str = Form(...),
                      host: str = Form("")):
        added = dup = 0
        # One per line, not per whitespace: Windows paths often contain spaces.
        for url in filter(None, (u.strip() for u in urls.splitlines())):
            try:
                with tx(conn):
                    _, created = add_source(conn, campaign_id, url, host or None)
            except ValueError as e:
                return back("/sources", str(e))
            added += created
            dup += not created
        return back("/sources", f"Queued {added} source(s)" + (f", {dup} already known" if dup
                                                                else ""))

    @app.post("/sources/{sid}/retry")
    def retry_source(sid: int, conn: DB):
        conn.execute("UPDATE jobs SET state='queued', attempts=0, run_after=? WHERE source_id=?"
                     " AND state IN ('dead','failed','queued')", (utcnow(), sid))
        conn.execute("UPDATE sources SET status=CASE WHEN status='failed' THEN 'new' ELSE status"
                     " END, error=NULL WHERE id=?", (sid,))
        return back("/sources", "Retrying")

    # ---------- dashboard ----------
    @app.get("/dashboard")
    def dashboard(request: Request, conn: DB):
        return page(request, "dashboard.html", d=analytics.dashboard(conn),
                    budget=costs.budget_status(conn), jobs=jobqueue.counts(conn),
                    health=health(conn))

    # ---------- worker API (laptop) ----------
    def require_worker(request: Request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not s.worker_token or token != s.worker_token:
            raise HTTPException(401, "bad worker token")

    Worker = Depends(require_worker)

    class ClaimIn(BaseModel):
        worker: str
        host: str
        kinds: list[str]
        protocol: int = 1
        version: str = ""

    @app.post("/api/worker/claim", dependencies=[Worker])
    def api_claim(body: ClaimIn, conn: DB):
        if body.protocol < WORKER_PROTOCOL or (code_version and body.version != code_version):
            # No heartbeat either: an outdated laptop must not count as online.
            raise HTTPException(426, "laptop worker is outdated: restart it with "
                                     "start-worker.bat, which updates it from the Pi")
        jobqueue.heartbeat(conn, body.worker, body.kinds)
        kinds = [k for k in body.kinds if k != "select"]  # selection always runs on the Pi
        for _ in range(5):
            job = jobqueue.claim(conn, body.worker, body.host, kinds)
            if not job:
                return Response(status_code=204)
            try:
                ctx = pipeline.build_context(conn, s, job)
            except pipeline.Deferred as e:
                jobqueue.defer(conn, job["id"], e.seconds, str(e))
                continue
            return {"job": dict(job), "ctx": ctx}
        return Response(status_code=204)

    def _job(conn, job_id: int):
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["state"] != "running":
            raise HTTPException(409, "job is not running (lease expired?)")
        return job

    @app.post("/api/worker/jobs/{job_id}/result", dependencies=[Worker])
    def api_result(job_id: int, conn: DB, result: str = Form(...),
                   file: UploadFile | None = File(None)):
        job = _job(conn, job_id)
        data = json.loads(result)
        host = job["locked_by"].rsplit("-", 2)[0]
        if job["kind"] == "render":
            if not file:
                raise HTTPException(400, "render result needs the clip file")
            cand = conn.execute("SELECT source_id FROM candidates WHERE id=?",
                                (job["candidate_id"],)).fetchone()
            rel = f"{cand['source_id']}/c{job['candidate_id']}.mp4"
            dest = s.clips_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".upload")
            with tmp.open("wb") as fh:
                shutil.copyfileobj(file.file, fh)
            tmp.replace(dest)
            data["rel_path"] = rel
            data["size_bytes"] = dest.stat().st_size
        pipeline.apply_result(conn, s, job, data, host=host)
        return {"ok": True}

    @app.get("/api/worker/code/version", dependencies=[Worker])
    def api_code_version():
        if not code_version:
            raise HTTPException(404, "the Pi isn't running from a git checkout")
        return {"version": code_version}

    @app.get("/api/worker/code", dependencies=[Worker])
    def api_code():
        if not code_version:
            raise HTTPException(404, "the Pi isn't running from a git checkout")
        return Response(code_zip(code_version), media_type="application/zip",
                        headers={"X-ClipForge-Version": code_version})

    @app.get("/api/worker/assets/{rel:path}", dependencies=[Worker])
    def api_asset(rel: str):
        root = s.assets_dir.resolve()
        p = (root / rel).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise HTTPException(404, "no such asset")
        return FileResponse(p)

    class DeferIn(BaseModel):
        seconds: float
        reason: str

    @app.post("/api/worker/jobs/{job_id}/defer", dependencies=[Worker])
    def api_defer(job_id: int, body: DeferIn, conn: DB):
        jobqueue.defer(conn, _job(conn, job_id)["id"], body.seconds, body.reason)
        return {"ok": True}

    class FailIn(BaseModel):
        error: str

    @app.post("/api/worker/jobs/{job_id}/fail", dependencies=[Worker])
    def api_fail(job_id: int, body: FailIn, conn: DB):
        job = _job(conn, job_id)
        if jobqueue.fail(conn, job_id, body.error) == "dead":
            pipeline.mark_failed_source(conn, job, body.error)
        return {"ok": True}

    @app.exception_handler(sqlite3.OperationalError)
    def db_busy(request: Request, exc: sqlite3.OperationalError):
        log.error("database error on %s: %s", request.url.path, exc)
        return JSONResponse({"error": "database busy, try again"}, status_code=503)
