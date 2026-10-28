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

from clipforge import analytics, briefs, costs, jobqueue, library, notify, pipeline, scout
from clipforge.bounties import BountyIn, add_bounty, get_bounty, list_bounties
from clipforge.campaigns import (
    CampaignIn,
    add_campaign,
    add_logo,
    delete_bounty,
    delete_campaign,
    get_campaign,
    list_campaigns,
    logo_list,
    remove_logo,
)
from clipforge.captions import PLATFORM_REMINDER
from clipforge.config import get_settings
from clipforge.db import connect, init_db, tx, utcnow
from clipforge.feedback import REJECT_REASONS
from clipforge.ingest import add_source, remote_hosts
from clipforge.select_prompts import sync_prompts
from clipforge.stages.fetch import dir_size_gb, free_gb
from clipforge.stages.scout import NotAnImage, prepare_image
from clipforge.update import code_zip, repo_version
from clipforge.worker import WORKER_PROTOCOL

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["money"] = lambda v: f"${v or 0:,.2f}"
templates.env.filters["num"] = lambda v: f"{v or 0:,}"
templates.env.filters["logos"] = logo_list
templates.env.filters["pct"] = lambda v: "—" if v is None else f"{v * 100:.0f}%"
templates.env.filters["secs"] = lambda ms: f"{(ms or 0) / 1000:.0f}s"
templates.env.filters["mmss"] = lambda ms: f"{(ms or 0) // 60000}:{(ms or 0) // 1000 % 60:02d}"

PLATFORMS = ["tiktok", "youtube", "instagram", "x"]
MAX_SHOT_BYTES = 15_000_000


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
    url, _, anchor = url.partition("#")
    if msg:
        url += ("&" if "?" in url else "?") + f"msg={quote(msg)}"
    return RedirectResponse(url + (f"#{anchor}" if anchor else ""), status_code=303)


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

    # ---------- campaigns → bounties (each bounty: footage, requirements, settings) ----------
    def _camp_or_404(conn, cid: int) -> dict:
        c = get_campaign(conn, cid)
        if not c:
            raise HTTPException(404, "no such campaign")
        return c

    def _bounty_or_404(conn, cid: int, bid: int) -> dict:
        b = get_bounty(conn, bid)
        if not b or b["campaign_id"] != cid:
            raise HTTPException(404, "no such bounty")
        return b

    @app.get("/campaigns")
    def campaigns(request: Request, conn: DB, msg: str = ""):
        # Query params pre-fill the Add form (the Scout page's "Add as campaign" link).
        prefill = {k: v for k, v in request.query_params.items() if k != "msg"}
        camps = list_campaigns(conn)
        for c in camps:
            c["bounties"] = list_bounties(conn, c["id"])
            c["to_review"] = conn.execute(
                "SELECT COUNT(*) FROM clips k JOIN candidates ca ON ca.id=k.candidate_id JOIN"
                " sources s ON s.id=ca.source_id WHERE s.campaign_id=? AND k.status='in_review'",
                (c["id"],)).fetchone()[0]
        return page(request, "campaigns.html", campaigns=camps, msg=msg, prefill=prefill)

    @app.post("/campaigns")
    async def create_campaign(request: Request, conn: DB):
        """Add a campaign with its first bounty. A campaign that already exists (same name) just
        gets the new bounty, so Scout's "Add" works for a second episode too."""
        form = {k: v for k, v in dict(await request.form()).items() if v not in ("", None)}
        force = form.pop("force", None) == "on"
        bounty = {"name": form.pop("bounty_name", "main"),
                  "rate_usd": form.pop("rate_usd", 0),
                  "rate_per_views": int(form.pop("rate_mode", 1000)),
                  "min_views": form.pop("min_views", 0), "cap_usd": form.pop("cap_usd", None),
                  "end_date": form.pop("end_date", None)}
        existing = conn.execute("SELECT id FROM campaigns WHERE lower(name)=lower(?)",
                                (form.get("name", ""),)).fetchone()
        try:
            with tx(conn):
                cid = existing["id"] if existing else add_campaign(conn, CampaignIn(**form),
                                                                   force=force)
                add_bounty(conn, cid, BountyIn(**bounty))
        except (ValueError, TypeError) as e:
            return back("/campaigns", str(e).split("\n")[0][:200])
        return back(f"/campaigns/{cid}", f"Bounty {bounty['name']} added" if existing
                    else f"Campaign #{cid} added")

    @app.get("/campaigns/{cid}")
    def campaign_page(request: Request, cid: int, conn: DB, msg: str = ""):
        c = _camp_or_404(conn, cid)
        return page(request, "campaign.html", c=c, bounties=list_bounties(conn, cid), msg=msg,
                    library_dir=s.media_library_dir.name,
                    counts=conn.execute(
                        "SELECT COUNT(DISTINCT s.id) sources, COUNT(k.id) clips FROM sources s"
                        " LEFT JOIN candidates ca ON ca.source_id=s.id LEFT JOIN clips k ON"
                        " k.candidate_id=ca.id WHERE s.campaign_id=?", (cid,)).fetchone())

    @app.post("/campaigns/{cid}/bounties")
    def bounty_add(cid: int, conn: DB, name: str = Form(...), rate_usd: float = Form(0),
                   rate_mode: int = Form(1000), min_views: int = Form(0),
                   end_date: str = Form("")):
        _camp_or_404(conn, cid)
        try:
            bid = add_bounty(conn, cid, BountyIn(name=name, rate_usd=rate_usd,
                                                 rate_per_views=rate_mode, min_views=min_views,
                                                 end_date=end_date or None))
        except ValueError as e:
            return back(f"/campaigns/{cid}", str(e))
        return back(f"/campaigns/{cid}/bounties/{bid}", f"Bounty {name} added")

    @app.post("/campaigns/{cid}/settings")
    def campaign_settings(cid: int, conn: DB, required_tags: str = Form(""),
                          allowed_platforms: str = Form(""), notes: str = Form("")):
        conn.execute("UPDATE campaigns SET required_tags=?, allowed_platforms=?, notes=?"
                     " WHERE id=?", (required_tags.strip(), allowed_platforms.strip()
                                     or "tiktok,youtube", notes.strip(), cid))
        return back(f"/campaigns/{cid}", "Saved")

    @app.post("/campaigns/{cid}/status")
    def campaign_status(cid: int, conn: DB, status: str = Form(...)):
        conn.execute("UPDATE campaigns SET status=? WHERE id=?", (status, cid))
        return back(f"/campaigns/{cid}", "Updated")

    @app.post("/campaigns/{cid}/delete")
    def campaign_delete(cid: int, conn: DB):
        c = _camp_or_404(conn, cid)
        try:
            n = delete_campaign(conn, cid)
        except ValueError as e:
            return back(f"/campaigns/{cid}", str(e))
        return back("/campaigns", f"Deleted {c['name']}: {n['sources']} videos, {n['clips']}"
                    f" clips, {n['posts']} posts")

    @app.post("/campaigns/{cid}/logo")
    async def campaign_logo(cid: int, conn: DB, logo: UploadFile = File(...)):
        try:
            add_logo(conn, cid, await logo.read())
        except ValueError as e:
            return back(f"/campaigns/{cid}", str(e))
        return back(f"/campaigns/{cid}", "Logo added. New renders will include it.")

    @app.post("/campaigns/{cid}/logo/remove")
    def campaign_logo_remove(cid: int, conn: DB, rel: str = Form("")):
        remove_logo(conn, cid, rel or None)
        return back(f"/campaigns/{cid}", "Logo removed")

    @app.get("/campaigns/{cid}/bounties/{bid}")
    def bounty_page(request: Request, cid: int, bid: int, conn: DB, msg: str = ""):
        c = _camp_or_404(conn, cid)
        b = _bounty_or_404(conn, cid, bid)
        docs = conn.execute(
            """SELECT d.*, j.state, j.last_error FROM campaign_docs d
               LEFT JOIN jobs j ON j.dedupe_key='brief:'||d.id
               WHERE d.bounty_id=? ORDER BY d.id""", (bid,)).fetchall()
        sources = conn.execute(
            """SELECT s.*,
                      (SELECT last_error FROM jobs j WHERE j.source_id=s.id AND j.last_error
                       IS NOT NULL ORDER BY j.updated_at DESC LIMIT 1) last_error,
                      (SELECT COUNT(*) FROM candidates WHERE source_id=s.id AND kept=1) kept
               FROM sources s WHERE s.bounty_id=? ORDER BY s.id DESC""", (bid,)).fetchall()
        files = library.files_for(conn, b)
        busy = any(d["state"] in ("queued", "running") for d in docs) or any(
            r["status"] not in ("rendered", "failed") for r in sources)
        return page(request, "bounty.html", c=c, b=b, docs=docs, brief=briefs.merged(conn, bid),
                    sources=sources, files=files, new_files=[f for f in files if not f["queued"]],
                    folders=library.folders(conn), hosts=[s.host_name, *remote_hosts(conn)],
                    busy=busy, library_dir=s.media_library_dir.name, msg=msg)

    @app.post("/campaigns/{cid}/bounties/{bid}/settings")
    def bounty_settings(cid: int, bid: int, conn: DB, rate_usd: float = Form(0),
                        rate_mode: int = Form(1000), min_views: int = Form(0),
                        cap_usd: str = Form(""), end_date: str = Form(""),
                        required_tags: str = Form(""), notes: str = Form(""),
                        auto_queue: bool = Form(False), clips_wanted: int = Form(6)):
        _bounty_or_404(conn, cid, bid)
        conn.execute(
            "UPDATE bounties SET rate_usd=?, rate_per_views=?, min_views=?, cap_usd=?,"
            " end_date=?, required_tags=?, notes=?, auto_queue=?, clips_wanted=? WHERE id=?",
            (rate_usd, rate_mode, min_views, float(cap_usd) if cap_usd.strip() else None,
             end_date or None, required_tags.strip(), notes.strip(), int(auto_queue),
             _clips(clips_wanted), bid))
        return back(f"/campaigns/{cid}/bounties/{bid}", "Saved")

    @app.post("/campaigns/{cid}/bounties/{bid}/status")
    def bounty_status(cid: int, bid: int, conn: DB, status: str = Form(...)):
        conn.execute("UPDATE bounties SET status=? WHERE id=? AND campaign_id=?",
                     (status, bid, cid))
        return back(f"/campaigns/{cid}/bounties/{bid}", "Updated")

    @app.post("/campaigns/{cid}/bounties/{bid}/folder")
    def bounty_folder_set(cid: int, bid: int, conn: DB, folder: str = Form(...)):
        conn.execute("UPDATE bounties SET media_folder=? WHERE id=? AND campaign_id=?",
                     (folder.strip().strip("/") or None, bid, cid))
        return back(f"/campaigns/{cid}/bounties/{bid}", f"Footage folder: {folder}")

    @app.post("/campaigns/{cid}/bounties/{bid}/delete")
    def bounty_delete(cid: int, bid: int, conn: DB):
        b = _bounty_or_404(conn, cid, bid)
        try:
            n = delete_bounty(conn, bid)
        except ValueError as e:
            return back(f"/campaigns/{cid}/bounties/{bid}", str(e))
        return back(f"/campaigns/{cid}", f"Deleted bounty {b['name']}: {n['sources']} videos,"
                    f" {n['clips']} clips, {n['posts']} posts")

    @app.get("/campaigns/{cid}/bounties/{bid}/queue")
    def bounty_queue_confirm(request: Request, cid: int, bid: int, conn: DB):
        c = _camp_or_404(conn, cid)
        b = _bounty_or_404(conn, cid, bid)
        files = [f for f in library.files_for(conn, b) if not f["queued"]]
        ready = [f for f in files if not f["settling"]]
        return page(request, "queue.html", c=c, b=b, files=files,
                    est=library.estimate(ready, b["clips_wanted"]),
                    budget=costs.budget_status(conn), most=s.candidates_requested,
                    render_s=library.RENDER_S_PER_CLIP)

    def _clips(n: int) -> int:
        """Clips to keep per video: at least 1, at most what the selector proposes."""
        return max(1, min(n, s.candidates_requested))

    @app.post("/campaigns/{cid}/bounties/{bid}/queue")
    def bounty_queue(cid: int, bid: int, conn: DB, path: list[str] = Form([]),
                     clips: int = Form(6)):
        b = _bounty_or_404(conn, cid, bid)
        known = {f["path"]: f for f in library.files_for(conn, b)}
        added = 0
        for p in path:
            f = known.get(p)
            if not f or f["queued"] or f["settling"]:
                continue
            with tx(conn):
                _, created = add_source(conn, cid, f["path"], f["host"], bounty_id=bid,
                                        clips=_clips(clips))
            added += created
        return back(f"/campaigns/{cid}/bounties/{bid}",
                    f"Queued {added} video(s) for clipping" if added else "Nothing queued")

    @app.post("/campaigns/{cid}/bounties/{bid}/docs")
    async def bounty_docs_upload(cid: int, bid: int, conn: DB,
                                 docs: list[UploadFile] = File(...)):
        _bounty_or_404(conn, cid, bid)
        added = 0
        for f in docs[:20]:
            data = await f.read(MAX_SHOT_BYTES + 1)
            if not data or len(data) > MAX_SHOT_BYTES:
                continue
            try:
                sha, path = prepare_image(data, s.assets_dir / "briefs")
            except NotAnImage:
                continue
            with tx(conn):
                cur = conn.execute(
                    "INSERT INTO campaign_docs (campaign_id, bounty_id, sha, path) VALUES"
                    " (?,?,?,?) ON CONFLICT(campaign_id, sha) DO NOTHING",
                    (cid, bid, sha, str(path.relative_to(s.assets_dir))))
                if cur.rowcount:
                    jobqueue.enqueue(conn, "brief", f"brief:{cur.lastrowid}",
                                     affinity_host=s.host_name, priority=50)
                    added += 1
        url = f"/campaigns/{cid}/bounties/{bid}"
        return back(url, f"Reading {added} requirement screenshot(s)…" if added
                    else "Nothing new: already uploaded, or not an image")

    @app.post("/campaigns/{cid}/bounties/{bid}/docs/apply")
    def bounty_docs_apply(cid: int, bid: int, conn: DB):
        _bounty_or_404(conn, cid, bid)
        changes = briefs.apply(conn, bid)
        return back(f"/campaigns/{cid}/bounties/{bid}", "Updated " + "; ".join(changes)
                    if changes else "Nothing new to apply")

    @app.post("/campaigns/{cid}/bounties/{bid}/docs/{doc_id}/delete")
    def bounty_docs_delete(cid: int, bid: int, doc_id: int, conn: DB):
        row = conn.execute("SELECT path FROM campaign_docs WHERE id=? AND bounty_id=?",
                           (doc_id, bid)).fetchone()
        if row:
            with tx(conn):
                conn.execute("DELETE FROM jobs WHERE dedupe_key=?", (f"brief:{doc_id}",))
                conn.execute("DELETE FROM campaign_docs WHERE id=?", (doc_id,))
            if not conn.execute("SELECT 1 FROM campaign_docs WHERE path=?",
                                (row["path"],)).fetchone():
                (s.assets_dir / row["path"]).unlink(missing_ok=True)
        return back(f"/campaigns/{cid}/bounties/{bid}", "Screenshot removed")

    @app.get("/sources")
    def sources():  # footage now lives on each bounty's page
        return RedirectResponse("/campaigns", status_code=303)

    @app.post("/sources")
    def create_source(conn: DB, campaign_id: int = Form(...), urls: str = Form(...),
                      host: str = Form(""), bounty_id: int | None = Form(None)):
        url_back = (f"/campaigns/{campaign_id}/bounties/{bounty_id}" if bounty_id
                    else f"/campaigns/{campaign_id}")
        added = dup = 0
        # One per line, not per whitespace: Windows paths often contain spaces.
        for url in filter(None, (u.strip() for u in urls.splitlines())):
            try:
                with tx(conn):
                    _, created = add_source(conn, campaign_id, url, host or None, bounty_id)
            except ValueError as e:
                return back(url_back, str(e))
            added += created
            dup += not created
        return back(url_back, f"Queued {added} source(s)" + (f", {dup} already known" if dup
                                                             else ""))

    @app.post("/sources/{sid}/retry")
    def retry_source(sid: int, conn: DB):
        conn.execute("UPDATE jobs SET state='queued', attempts=0, run_after=? WHERE source_id=?"
                     " AND state IN ('dead','failed','queued')", (utcnow(), sid))
        conn.execute("UPDATE sources SET status=CASE WHEN status='failed' THEN 'new' ELSE status"
                     " END, error=NULL WHERE id=?", (sid,))
        row = conn.execute("SELECT campaign_id, bounty_id FROM sources WHERE id=?",
                           (sid,)).fetchone()
        return back(f"/campaigns/{row['campaign_id']}/bounties/{row['bounty_id']}" if row
                    else "/campaigns", "Retrying")

    # ---------- scouting ----------
    @app.get("/scout")
    def scout_page(request: Request, conn: DB, msg: str = ""):
        shots = conn.execute(
            """SELECT sh.*, j.state, j.last_error, COUNT(b.id) n
               FROM scout_shots sh LEFT JOIN jobs j ON j.dedupe_key='scout:'||sh.id
               LEFT JOIN scout_bounties b ON b.shot_id=sh.id
               GROUP BY sh.id ORDER BY sh.id DESC LIMIT 40""").fetchall()
        pending = sum(1 for r in shots if r["state"] in ("queued", "running"))
        return page(request, "scout.html", ranked=scout.ranked(conn), shots=shots,
                    pending=pending, msg=msg)

    @app.post("/scout/upload")
    async def scout_upload(conn: DB, shots: list[UploadFile] = File(...)):
        added = skipped = 0
        for f in shots[:30]:
            data = await f.read(MAX_SHOT_BYTES + 1)
            if not data or len(data) > MAX_SHOT_BYTES:
                skipped += 1
                continue
            try:
                sha, path = prepare_image(data, s.assets_dir / "scout")
            except NotAnImage:
                skipped += 1
                continue
            with tx(conn):
                cur = conn.execute("INSERT INTO scout_shots (sha, path) VALUES (?,?)"
                                   " ON CONFLICT(sha) DO NOTHING",
                                   (sha, str(path.relative_to(s.assets_dir))))
                if cur.rowcount:
                    jobqueue.enqueue(conn, "scout", f"scout:{cur.lastrowid}",
                                     affinity_host=s.host_name, priority=50)
                    added += 1
                else:
                    skipped += 1
        if not added:
            return back("/scout", "Nothing new: already uploaded, or not an image")
        msg = f"Reading {added} screenshot(s); Discord pings when done"
        return back("/scout", msg + (f" ({skipped} skipped: duplicate or not an image)"
                                     if skipped else ""))

    @app.post("/scout/shots/{shot_id}/delete")
    def scout_delete(shot_id: int, conn: DB):
        row = conn.execute("SELECT path FROM scout_shots WHERE id=?", (shot_id,)).fetchone()
        if row:
            with tx(conn):
                conn.execute("DELETE FROM scout_bounties WHERE shot_id=?", (shot_id,))
                conn.execute("DELETE FROM jobs WHERE dedupe_key=?", (f"scout:{shot_id}",))
                conn.execute("DELETE FROM scout_shots WHERE id=?", (shot_id,))
            (s.assets_dir / row["path"]).unlink(missing_ok=True)
        return back("/scout", "Screenshot and its bounties removed")

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

    class LibraryIn(BaseModel):
        host: str
        files: list[dict]

    @app.get("/api/worker/library/folders", dependencies=[Worker])
    def api_library_folders(conn: DB):
        """Footage folders the media machine should have: one per active campaign."""
        rows = conn.execute(
            "SELECT b.media_folder FROM bounties b JOIN campaigns c ON c.id=b.campaign_id"
            " WHERE b.status='active' AND c.status='active' AND b.media_folder IS NOT NULL"
            " ORDER BY b.id").fetchall()
        return {"folders": [r["media_folder"] for r in rows]}

    @app.post("/api/worker/library", dependencies=[Worker])
    def api_library(body: LibraryIn, conn: DB):
        with tx(conn):
            n = library.store(conn, body.host, body.files)
            library.relink_moved(conn, body.host)
            msg = library.newly_ready(conn, body.host)
        if msg:
            notify.discord(msg)
        return {"ok": True, "files": n}

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
