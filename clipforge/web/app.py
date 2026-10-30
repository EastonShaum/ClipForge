"""FastAPI app: mobile-first review UI, campaign/source forms, dashboard, health, worker API."""

import json
import logging
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.datastructures import UploadFile as StarletteUpload

from clipforge import (
    analytics,
    bounties,
    briefs,
    costs,
    jobqueue,
    library,
    live,
    notify,
    pipeline,
    scout,
    tiktok,
)
from clipforge.bounties import (
    BountyIn,
    add_bounty,
    clip_counts,
    get_bounty,
    label,
    list_bounties,
)
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
templates.env.filters["hm"] = lambda sec: f"{int(sec or 0) // 3600}h{int(sec or 0) % 3600 // 60:02d}"
templates.env.filters["mmss"] = lambda ms: f"{(ms or 0) // 60000}:{(ms or 0) // 1000 % 60:02d}"

PLATFORMS = ["tiktok", "youtube", "instagram", "x"]
MAX_SHOT_BYTES = 15_000_000
MAX_RESULT_BYTES = 200_000_000  # a stage result (JSON); ~10 h of word timings is ~10 MB


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


def _clip_rows(conn, status: str, bounty_id: int, limit: int = 100) -> list[dict]:
    """One bounty's clips at one stage, best first, each with its posts (latest views
    included) and the allowed platforms it hasn't been posted to yet."""
    rows = conn.execute(
        """SELECT k.*, c.hook_text, c.hook_type, c.title, c.rationale, c.score, c.start_ms,
                  c.end_ms, c.source_id, s.title src_title, s.url src_url, cp.name campaign,
                  cp.id campaign_id, cp.allowed_platforms, pv.name prompt
           FROM clips k JOIN candidates c ON c.id=k.candidate_id JOIN sources s ON s.id=c.source_id
           JOIN campaigns cp ON cp.id=s.campaign_id
           LEFT JOIN prompt_versions pv ON pv.id=c.prompt_version_id
           WHERE k.status=? AND s.bounty_id=? ORDER BY c.score DESC, k.id LIMIT ?""",
        (status, bounty_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["posts"] = [dict(p) for p in conn.execute(
            """SELECT p.*,
                      (SELECT views FROM view_snapshots v WHERE v.post_id=p.id
                       ORDER BY v.taken_at DESC, v.id DESC LIMIT 1) views,
                      (SELECT MAX(taken_at) FROM view_snapshots v WHERE v.post_id=p.id) checked
               FROM posts p WHERE clip_id=? ORDER BY id""", (r["id"],))]
        done = {p["platform"] for p in d["posts"]}
        d["todo"] = [p.strip() for p in r["allowed_platforms"].split(",")
                     if p.strip() and p.strip() not in done]
        out.append(d)
    return out


def _bounty_with(conn, status: str) -> sqlite3.Row | None:
    """The bounty with the most clips at this stage (for the old top-level links)."""
    return conn.execute(
        "SELECT s.campaign_id cid, s.bounty_id bid, COUNT(*) n FROM clips k"
        " JOIN candidates c ON c.id=k.candidate_id JOIN sources s ON s.id=c.source_id"
        " WHERE k.status=? AND s.bounty_id IS NOT NULL GROUP BY s.bounty_id"
        " ORDER BY n DESC LIMIT 1", (status,)).fetchone()


def _next(url: str, default: str) -> str:
    """Where a form goes back to: a local path only."""
    return url if url.startswith("/") and not url.startswith("//") else default


def _routes(app: FastAPI) -> None:  # noqa: C901 - flat list of routes reads best
    s = get_settings()
    # The commit this process loaded. Remote workers must run exactly this (see update.py).
    code_version = repo_version() if s.require_worker_version else None

    # Live streams (D34): their own tab, apart from campaigns. Each stream's clips have their
    # own Review / Post / Posted tabs until Easton moves the stream into a paid bounty.
    @app.get("/lives")
    def lives(request: Request, conn: DB, msg: str = ""):
        streamers = []
        for c in live.watched(conn) + [r for r in conn.execute(
                "SELECT * FROM campaigns WHERE status='active' AND watch_live=0"
                " AND live_channels!='' ORDER BY id")]:
            streamers.append({**dict(c), "channels": [
                (ch, live.channel_url(ch), live.channel_status(conn, ch))
                for ch in live.parse_channels(c["live_channels"])[0]]})
        return page(request, "lives.html", streamers=streamers, sessions=live.sessions(conn),
                    paid=bounties.paid_bounties(conn), live_dir=s.live_dir.name, msg=msg,
                    every=s.live_check_s // 60)

    @app.post("/lives/watch")
    def lives_watch(conn: DB, name: str = Form(...), live_channels: str = Form("")):
        chans, bad = live.parse_channels(live_channels)
        if bad:
            return back("/lives", f"Not a Kick or Twitch channel: {' '.join(bad)}"
                        " (use e.g. kick.com/n3on)")
        name = " ".join(name.split())
        row = conn.execute("SELECT id FROM campaigns WHERE lower(name)=lower(?)",
                           (name,)).fetchone()
        try:
            with tx(conn):
                cid = row["id"] if row else add_campaign(conn, CampaignIn(name=name))
                conn.execute("UPDATE campaigns SET watch_live=1, live_channels=CASE WHEN ?=''"
                             " THEN live_channels ELSE ? END WHERE id=?",
                             (" ".join(chans), " ".join(chans), cid))
        except ValueError as e:
            return back("/lives", str(e).split("\n")[0][:200])
        return back("/lives", f"Watching {name}. Channels are checked every"
                    f" {s.live_check_s // 60} min.")

    @app.post("/lives/{session_id}/bounty")
    def lives_bounty(session_id: int, conn: DB, bounty_id: str = Form("")):
        try:
            with tx(conn):
                n = live.move_to(conn, session_id, int(bounty_id) if bounty_id else None)
        except (TypeError, ValueError) as e:
            return back("/lives", str(e))
        b = get_bounty(conn, conn.execute("SELECT bounty_id FROM live_sessions WHERE id=?",
                                          (session_id,)).fetchone()["bounty_id"])
        return back("/lives", f"Moved {n} clip(s) to {label(b['campaign'], b['name'])}."
                    " New clips from this stream go there too." if bounty_id else
                    f"Moved {n} clip(s) back to the stream's own tabs.")

    @app.post("/live/{session_id}/guests")
    def live_guests(session_id: int, conn: DB, guests: str = Form(""), next: str = Form("")):
        conn.execute("UPDATE live_sessions SET guests=? WHERE id=?",
                     (" ".join(guests.split())[:500], session_id))
        return back(_next(next, "/lives"), "Saved. The next chunks use these names.")

    # TikTok inbox drafts (D32). Connect once; each upload is one tap, and Easton posts it.
    @app.get("/auth/tiktok")
    def tiktok_connect(conn: DB):
        if not tiktok.configured(s):
            return back("/campaigns", "Add CF_TIKTOK_CLIENT_KEY and _SECRET to .env first")
        return RedirectResponse(tiktok.authorize_url(conn, s), status_code=303)

    @app.get("/auth/tiktok/callback")
    def tiktok_callback(conn: DB, code: str = "", state: str = "", error: str = "",
                        error_description: str = ""):
        if error or not code:
            return back("/campaigns", f"TikTok said no: {error_description or error or 'no code'}")
        try:
            with tiktok.client() as http:
                a = tiktok.exchange_code(conn, s, code, state, http)
        except (tiktok.TikTokError, httpx.HTTPError) as e:
            return back("/campaigns", f"TikTok connect failed: {e}")
        if "video.upload" not in a["scope"]:
            return back("/campaigns", "Connected, but without the video.upload permission")
        return back("/campaigns", "TikTok connected")

    @app.post("/auth/tiktok/disconnect")
    def tiktok_disconnect(conn: DB):
        tiktok.disconnect(conn)
        return back("/campaigns", "TikTok disconnected")

    @app.post("/clips/{clip_id}/tiktok")
    def tiktok_send(clip_id: int, conn: DB, next: str = Form("")):
        to = _next(next, "/approved") + f"#clip{clip_id}"
        row = conn.execute("SELECT path FROM clips WHERE id=?", (clip_id,)).fetchone()
        if not row or not row["path"]:
            return back(to, "That clip has no video yet")
        try:
            with tiktok.client() as http:
                tiktok.send_to_inbox(conn, s, clip_id, s.clips_dir / row["path"], http)
        except (tiktok.TikTokError, httpx.HTTPError, OSError) as e:
            return back(to, f"TikTok upload failed: {e}")
        return back(to, "Sent. TikTok will notify you in the app when the draft is ready.")

    @app.post("/tiktok/{publish_id}/check")
    def tiktok_check(publish_id: str, conn: DB, next: str = Form("")):
        to = _next(next, "/approved")
        try:
            with tiktok.client() as http:
                st = tiktok.refresh_status(conn, s, publish_id, http)
        except (tiktok.TikTokError, httpx.HTTPError) as e:
            return back(to, f"Couldn't check: {e}")
        return back(to, f"TikTok status: {st}")

    # Terms and privacy pages for the TikTok developer app (TikTok asks for both URLs).
    @app.get("/terms")
    def terms(request: Request):
        return page(request, "legal.html", kind="terms", title="Terms of Service")

    @app.get("/privacy")
    def privacy(request: Request):
        return page(request, "legal.html", kind="privacy", title="Privacy Policy")

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

    # ---------- review → post → posted, inside each bounty ----------
    @app.get("/")
    def home():
        return RedirectResponse("/campaigns")

    # The old top-level pages (and older Discord links) jump to the bounty that needs it.
    @app.get("/review")
    def review_any(conn: DB, msg: str = ""):
        b = _bounty_with(conn, "in_review")
        return back(f"/campaigns/{b['cid']}/bounties/{b['bid']}/review" if b
                    else "/campaigns", msg or ("" if b else "Nothing waiting for review"))

    @app.get("/approved")
    def approved_any(conn: DB, msg: str = ""):
        b = _bounty_with(conn, "approved") or _bounty_with(conn, "posted")
        return back(f"/campaigns/{b['cid']}/bounties/{b['bid']}/post" if b
                    else "/campaigns", msg or ("" if b else "Nothing approved yet"))

    @app.get("/posts")
    def posts_any(conn: DB, msg: str = ""):
        b = _bounty_with(conn, "posted")
        return back(f"/campaigns/{b['cid']}/bounties/{b['bid']}/posted" if b
                    else "/campaigns", msg or ("" if b else "No posts logged yet"))

    def _tab(request: Request, conn, cid: int, bid: int, name: str, **ctx):
        c = _camp_or_404(conn, cid)
        b = _bounty_or_404(conn, cid, bid)
        return page(request, f"{name}.html", c=c, b=b, tabs=clip_counts(conn, bid),
                    here=f"/campaigns/{cid}/bounties/{bid}/{name}", **ctx)

    @app.get("/campaigns/{cid}/bounties/{bid}/review")
    def bounty_review(request: Request, cid: int, bid: int, conn: DB, msg: str = ""):
        return _tab(request, conn, cid, bid, "review", clips=_clip_rows(conn, "in_review", bid),
                    reasons=REJECT_REASONS, msg=msg)

    @app.get("/campaigns/{cid}/bounties/{bid}/post")
    def bounty_post(request: Request, cid: int, bid: int, conn: DB, msg: str = ""):
        # Approved clips, plus posted ones that still have an allowed platform to go.
        clips = _clip_rows(conn, "approved", bid) + [
            r for r in _clip_rows(conn, "posted", bid) if r["todo"]]
        return _tab(request, conn, cid, bid, "post", clips=clips, platforms=PLATFORMS,
                    reminders=PLATFORM_REMINDER, msg=msg,
                    tt={"configured": tiktok.configured(s), "connected": bool(tiktok.auth(conn)),
                        "uploads": tiktok.latest_uploads(conn, [c["id"] for c in clips])})

    @app.get("/campaigns/{cid}/bounties/{bid}/posted")
    def bounty_posted(request: Request, cid: int, bid: int, conn: DB, msg: str = ""):
        return _tab(request, conn, cid, bid, "posted", clips=_clip_rows(conn, "posted", bid),
                    platforms=PLATFORMS, msg=msg)

    @app.post("/clips/{clip_id}/review")
    def decide(clip_id: int, conn: DB, decision: str = Form(...),
               reason: list[str] = Form([]), note: str = Form(""), next: str = Form("")):
        to = _next(next, "/review")
        if decision not in ("approved", "rejected"):
            raise HTTPException(400, "bad decision")
        # Several presets may be ticked; stored comma-joined, unknown keys dropped.
        reasons = ",".join(r for r in reason if r in REJECT_REASONS)
        note = note.strip()
        if decision == "rejected" and not (reasons or note):
            return back(to, "Pick a reason or write a note so the AI can learn from it")
        with tx(conn):
            cur = conn.execute("UPDATE clips SET status=? WHERE id=? AND status='in_review'",
                               (decision, clip_id))
            if cur.rowcount:
                conn.execute("INSERT INTO reviews (clip_id, decision, reason, note)"
                             " VALUES (?,?,?,?)",
                             (clip_id, decision, reasons or None, note or None))
        return back(to, "Approved" if decision == "approved" else "Rejected")

    @app.post("/clips/{clip_id}/caption")
    def edit_caption(clip_id: int, conn: DB, caption: str = Form(...), next: str = Form("")):
        conn.execute("UPDATE clips SET caption=? WHERE id=?", (caption, clip_id))
        return back(_next(next, "/approved") + f"#clip{clip_id}", "Caption saved")

    def _post_fields(platform: str, url: str) -> str | None:
        """The problem with a platform + URL pair, or None if it's fine."""
        if platform not in PLATFORMS:
            return "Pick a platform"
        if not url.strip().startswith("http"):
            return "Paste the full post URL (starting with https://)"
        return None

    @app.post("/clips/{clip_id}/post")
    def log_post(clip_id: int, conn: DB, platform: str = Form(...), url: str = Form(...),
                 next: str = Form("")):
        to = _next(next, "/approved")
        if err := _post_fields(platform, url):
            return back(to, err)
        try:
            with tx(conn):
                conn.execute("INSERT INTO posts (clip_id, platform, url) VALUES (?,?,?)",
                             (clip_id, platform, url.strip()))
                conn.execute("UPDATE clips SET status='posted' WHERE id=?", (clip_id,))
        except sqlite3.IntegrityError:
            return back(to, "That URL is already logged")
        return back(to, f"Logged {platform} post")

    @app.post("/posts/{post_id}/edit")
    def edit_post(post_id: int, conn: DB, platform: str = Form(...), url: str = Form(...),
                  next: str = Form("")):
        to = _next(next, "/posts")
        if err := _post_fields(platform, url):
            return back(to, err)
        try:
            conn.execute("UPDATE posts SET platform=?, url=? WHERE id=?",
                         (platform, url.strip(), post_id))
        except sqlite3.IntegrityError:
            return back(to, "That URL is already logged on another post")
        return back(to, "Post updated")

    @app.post("/posts/{post_id}/delete")
    def delete_post(post_id: int, conn: DB, next: str = Form("")):
        p = conn.execute("SELECT clip_id FROM posts WHERE id=?", (post_id,)).fetchone()
        if p:
            with tx(conn):
                conn.execute("DELETE FROM view_snapshots WHERE post_id=?", (post_id,))
                conn.execute("DELETE FROM posts WHERE id=?", (post_id,))
                # A clip with no posts left goes back to "ready to post".
                conn.execute("UPDATE clips SET status='approved' WHERE id=? AND NOT EXISTS"
                             " (SELECT 1 FROM posts WHERE clip_id=?)", (p["clip_id"], p["clip_id"]))
        return back(_next(next, "/posts"), "Post removed")

    @app.post("/posts/{post_id}/submitted")
    def mark_submitted(post_id: int, conn: DB, undo: str = Form(""), next: str = Form("")):
        conn.execute("UPDATE posts SET submitted_at=? WHERE id=?",
                     (None if undo else utcnow(), post_id))
        return back(_next(next, "/posts"), "Marked not submitted" if undo else "Marked submitted")

    @app.post("/clips/{clip_id}/unreview")
    def unreview(clip_id: int, conn: DB, next: str = Form("")):
        conn.execute("UPDATE clips SET status='in_review' WHERE id=? AND status IN"
                     " ('approved','rejected')", (clip_id,))
        return back(_next(next, "/review"), "Moved back to review")

    @app.post("/posts/{post_id}/views")
    def add_views(post_id: int, conn: DB, views: int = Form(...), likes: str = Form(""),
                  next: str = Form("")):
        conn.execute("INSERT INTO view_snapshots (post_id, views, likes) VALUES (?,?,?)",
                     (post_id, views, int(likes) if likes.strip().isdigit() else None))
        return back(_next(next, "/posts"), "Views saved")

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
        # A streamer added on the Lives tab, with no paid bounty, lives there only.
        camps = [c for c in camps if c["bounties"] or not (c["watch_live"] or c["live_channels"])]
        return page(request, "campaigns.html", campaigns=camps, msg=msg, prefill=prefill)

    @app.post("/campaigns")
    async def create_campaign(request: Request, conn: DB):
        """Add a campaign with its first bounty. A campaign that already exists (same name) just
        gets the new bounty, so Scout's "Add" works for a second episode too."""
        form = {k: v for k, v in dict(await request.form()).items() if v not in ("", None)}
        force = form.pop("force", None) == "on"
        clip_live = form.pop("live_clip", None) == "true"
        chans, bad = live.parse_channels(form.get("live_channels", ""))
        if bad:
            return back("/campaigns", f"Not a Kick or Twitch channel: {' '.join(bad)}"
                        " (use e.g. kick.com/n3on)")
        form["live_channels"] = " ".join(chans)
        form["watch_live"] = bool(chans) or clip_live
        bounty = {"live_clip": clip_live, "render_hold": clip_live,
                  "name": form.pop("bounty_name", "main"),
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
                if existing and (chans or clip_live):  # Scout's "Add & clip live" again
                    conn.execute("UPDATE campaigns SET watch_live=1, live_channels=CASE WHEN"
                                 " ?='' THEN live_channels ELSE ? END WHERE id=?",
                                 (form["live_channels"], form["live_channels"], cid))
                bid = add_bounty(conn, cid, BountyIn(**bounty))
                # "Add & clip live" while they're live: this stream's clips go to the bounty.
                now_live = conn.execute("SELECT id FROM live_sessions WHERE campaign_id=? AND"
                                        " state='live' AND blocked IS NULL", (cid,)).fetchone()
                if clip_live and now_live:
                    live.move_to(conn, now_live["id"], bid)
        except (ValueError, TypeError) as e:
            return back("/campaigns", str(e).split("\n")[0][:200])
        msg = (f"Bounty {bounty['name']} added" if existing else f"Campaign #{cid} added")
        if clip_live:
            return back(f"/campaigns/{cid}/bounties/{bid}",
                        f"{msg}. " + ("Today's stream is in it now; clips" if now_live else
                                      "Move a stream into it from the Lives tab. Clips")
                        + " render once you add the requirement screenshots below.")
        return back(f"/campaigns/{cid}", msg)

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
        with tx(conn):
            n = bounties.release_ready(conn, cid)
        return back(f"/campaigns/{cid}", "Logo added. New renders will include it."
                    + (f" Rendering {n} held clip(s)." if n else ""))

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
                    busy=busy, library_dir=s.media_library_dir.name, msg=msg,
                    tabs=clip_counts(conn, bid), holds=bounties.hold_reasons(conn, b),
                    held=bounties.held_count(conn, bid),
                    sessions=live.sessions(conn, bounty_id=bid))

    @app.post("/campaigns/{cid}/bounties/{bid}/settings")
    def bounty_settings(cid: int, bid: int, conn: DB, rate_usd: float = Form(0),
                        rate_mode: int = Form(1000), min_views: int = Form(0),
                        cap_usd: str = Form(""), end_date: str = Form(""),
                        required_tags: str = Form(""), notes: str = Form(""),
                        auto_queue: bool = Form(False), clips_wanted: int = Form(6),
                        live_clip: bool = Form(False), render_hold: bool = Form(False)):
        b = _bounty_or_404(conn, cid, bid)
        with tx(conn):
            conn.execute(
                "UPDATE bounties SET rate_usd=?, rate_per_views=?, min_views=?, cap_usd=?,"
                " end_date=?, required_tags=?, notes=?, auto_queue=?, clips_wanted=?,"
                " live_clip=?, render_hold=? WHERE id=?",
                (rate_usd, rate_mode, min_views, float(cap_usd) if cap_usd.strip() else None,
                 end_date or None, required_tags.strip(), notes.strip(), int(auto_queue),
                 _clips(clips_wanted), int(live_clip), int(render_hold), bid))
            if b["render_hold"] and not render_hold:
                bounties.release(conn, bid)  # unticking the hold renders what was waiting
        return back(f"/campaigns/{cid}/bounties/{bid}", "Saved")

    @app.post("/campaigns/{cid}/bounties/{bid}/release")
    def bounty_release(cid: int, bid: int, conn: DB):
        _bounty_or_404(conn, cid, bid)
        with tx(conn):
            n = bounties.release(conn, bid)
        return back(f"/campaigns/{cid}/bounties/{bid}",
                    f"Rendering {n} clip(s) now" if n else "No longer holding renders")

    @app.post("/campaigns/{cid}/live")
    def campaign_live(cid: int, conn: DB, live_channels: str = Form(""),
                      watch_live: bool = Form(False)):
        _camp_or_404(conn, cid)
        chans, bad = live.parse_channels(live_channels)
        if bad:
            return back("/lives", f"Not a Kick or Twitch channel:"
                        f" {' '.join(bad)} (use e.g. kick.com/n3on)")
        conn.execute("UPDATE campaigns SET live_channels=?, watch_live=? WHERE id=?",
                     (" ".join(chans), int(watch_live), cid))
        return back("/lives", "Saved. Channels are checked every"
                    f" {s.live_check_s // 60} min.")

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
        with tx(conn):
            changes = briefs.apply(conn, bid)
            n = bounties.release_ready(conn, cid)
        msg = "Updated " + "; ".join(changes) if changes else "Nothing new to apply"
        return back(f"/campaigns/{cid}/bounties/{bid}",
                    f"{msg}. Rendering {n} held clip(s)." if n else msg)

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
    async def api_result(job_id: int, request: Request, conn: DB):
        # Starlette caps form fields at 1 MB; a long stream's word-level transcript is bigger.
        form = await request.form(max_part_size=MAX_RESULT_BYTES)
        if "result" not in form:
            raise HTTPException(400, "missing result")
        file = form.get("file")
        job = _job(conn, job_id)
        data = json.loads(form["result"])
        host = job["locked_by"].rsplit("-", 2)[0]
        if job["kind"] == "render":
            if not isinstance(file, StarletteUpload):
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

    @app.get("/api/worker/live", dependencies=[Worker])
    def api_live(host: str, conn: DB):
        """Live sessions for this host's recorder (clipforge/recorder.py)."""
        return live.for_recorder(conn, host)

    @app.post("/api/worker/live/{session_id}", dependencies=[Worker])
    def api_live_report(session_id: int, body: dict, conn: DB):
        try:
            msg = live.report(conn, session_id, body)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        if msg:
            notify.discord(msg)
        return {"ok": True}

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
