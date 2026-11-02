"""FastAPI app: mobile-first review UI, campaign/source forms, dashboard, health, worker API."""

import json
import logging
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from zoneinfo import ZoneInfo

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
    inbox,
    jobqueue,
    library,
    live,
    money,
    notify,
    pipeline,
    posting,
    scout,
    storage,
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


def local_time(iso: str | None, style: str = "dt") -> str:
    """A stored UTC time in Easton's time zone (MDT/MST): "Oct 31, 5:03 PM" (dt), "5:03 PM"
    (t), "Oct 31" (d) or "Sat Oct 31" (day)."""
    if not iso:
        return ""
    t = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).astimezone(
        ZoneInfo(get_settings().timezone))
    clock = f"{t.hour % 12 or 12}:{t:%M} {t:%p}"
    return {"t": clock, "d": f"{t:%b} {t.day}", "day": f"{t:%a %b} {t.day}"}.get(
        style, f"{t:%b} {t.day}, {clock}")


templates.env.filters["local"] = local_time
templates.env.filters["gb"] = lambda b: f"{(b or 0) / 1e9:.1f} GB" if (b or 0) >= 1e8 \
    else f"{(b or 0) / 1e6:.0f} MB"
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


def _live_now() -> bool:
    """For the tab bar's dot: is a watched streamer live?"""
    conn = connect()
    try:
        return bool(conn.execute("SELECT 1 FROM live_sessions WHERE state='live'").fetchone())
    except sqlite3.Error:
        return False
    finally:
        conn.close()


templates.env.globals["live_now"] = _live_now
templates.env.globals |= {"marketplaces": money.MARKETPLACES, "payout": money.payout,
                          "campaign_types": money.CAMPAIGN_TYPES,
                          "payer_types": money.PAYER_TYPES, "cap_views": money.cap_views,
                          "all_platforms": ["tiktok", "youtube", "instagram", "x"],
                          "platform_names": {"tiktok": "TikTok", "youtube": "YouTube Shorts",
                                             "instagram": "Instagram Reels", "x": "X"},
                          "categories": ["podcast", "interview", "comedy", "startup", "soccer",
                                         "gaming", "streamer", "music", "brand", "political",
                                         "other"]}


def page(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {"active": name.split(".")[0], **ctx})


def _num(v) -> float | None:
    try:
        return float(v) if v is not None and str(v).strip() != "" else None
    except ValueError:
        return None


def terms_from(form) -> dict:
    """A bounty's money terms and rules from the _terms.html fields (D38). min_views is
    derived from the minimum payout when one is given (money.min_views_for)."""
    kind = form.get("campaign_type") or "cpm"
    if kind not in money.CAMPAIGN_TYPES:
        raise ValueError(f"unknown pay type {kind}")
    t = {"campaign_type": kind, "rate_usd": _num(form.get("rate_usd")) or 0.0,
         "rate_per_views": int(form.get("rate_mode") or 1000),
         "min_views": int(_num(form.get("min_views")) or 0),
         "end_date": form.get("end_date") or None,
         "pays_on": form.get("pays_on") if form.get("pays_on") in (
             "views", "engaged_views") else "unknown"}
    for k in ("cap_usd", "min_payout_usd", "flat_fee_bonus_usd", "budget_total_usd",
              "budget_remaining_usd"):
        t[k] = _num(form.get(k))
    for k in ("brief_url", "audience_rules", "footage_scope", "brief_text"):
        t[k] = (form.get(k) or "").strip()
    for k in ("onscreen_ad_label_required", "ai_video_banned"):
        t[k] = form.get(k) in ("true", "on", True)
    t["min_views"] = money.min_views_for(t)
    return t


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
    def lives(request: Request, conn: DB, msg: str = "", tab: str = "streams"):
        streamers = []
        for c in live.watched(conn) + [r for r in conn.execute(
                "SELECT * FROM campaigns WHERE status='active' AND watch_live=0"
                " AND live_channels!='' ORDER BY id")]:
            streamers.append({**dict(c), "channels": [
                (ch, live.channel_url(ch), live.channel_status(conn, ch))
                for ch in live.parse_channels(c["live_channels"])[0]]})
        sessions = live.sessions(conn)
        for ls in sessions:
            ls["tl"] = live.timeline(conn, ls)
        return page(request, "lives.html", streamers=streamers, sessions=sessions,
                    tab="streamers" if tab == "streamers" else "streams",
                    paid=bounties.paid_bounties(conn), live_dir=s.live_dir.name, msg=msg,
                    every=s.live_check_s // 60)

    @app.post("/lives/{session_id}/recording")
    def lives_recording(session_id: int, conn: DB, on: str = Form(...)):
        turn_on = on == "1"
        if not live.set_recording(conn, session_id, turn_on):
            return back("/lives", "That stream isn't live any more")
        return back(f"/lives#live{session_id}", "Recording and clipping again" if turn_on else
                    "Stopped: not recording or clipping this stream. Clips already made stay.")

    @app.get("/files")
    def files(request: Request, conn: DB, msg: str = ""):
        return page(request, "files.html", recs=storage.recordings(conn, s),
                    downloads=storage.downloads(conn, s), clips=storage.clip_files(conn, s),
                    groups=storage.CLIP_GROUPS, disk=storage.disk(s), s=s, msg=msg,
                    purged=conn.execute("SELECT COUNT(*) FROM live_sessions"
                                        " WHERE purged_at IS NOT NULL").fetchone()[0])

    @app.post("/files/recordings/{session_id}/delete")
    def files_delete_recording(session_id: int, conn: DB):
        ok = live.delete_recording(conn, session_id)
        return back("/files", "Deleting it the next time the laptop checks in" if ok
                    else "Can't delete a stream while it's recording: turn it off first")

    @app.post("/files/downloads/{source_id}/delete")
    def files_delete_download(source_id: int, conn: DB):
        freed = storage.delete_download(conn, s, source_id)
        return back("/files", f"Deleted, freed {freed / 1e6:.0f} MB" if freed
                    else "Nothing to delete")

    @app.post("/files/clips/delete")
    def files_delete_clips(conn: DB, bounty_id: int = Form(...), status: str = Form(...)):
        if status not in storage.CLIP_GROUPS:
            raise HTTPException(400, "bad status")
        n, freed = storage.delete_clip_files(conn, s, bounty_id, status)
        return back("/files", f"Deleted {n} video(s), freed {freed / 1e6:.0f} MB")

    @app.post("/lives/watch")
    def lives_watch(conn: DB, name: str = Form(...), live_channels: str = Form("")):
        chans, bad = live.parse_channels(live_channels)
        if bad:
            return back("/lives?tab=streamers", f"Not a Kick or Twitch channel: {' '.join(bad)}"
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
            return back("/lives?tab=streamers", str(e).split("\n")[0][:200])
        return back("/lives?tab=streamers", f"Watching {name}. Channels are checked every"
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
    def home(request: Request, conn: DB, msg: str = ""):
        return page(request, "home.html", n=inbox.needs_you(conn), msg=msg,
                    streams=[r for r in live.sessions(conn, limit=5) if r["state"] == "live"])

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
                    prog=inbox.progress(conn, b),
                    here=ctx.pop("here", f"/campaigns/{cid}/bounties/{bid}/{name}"), **ctx)

    @app.get("/campaigns/{cid}/bounties/{bid}/review")
    def bounty_review(request: Request, cid: int, bid: int, conn: DB, msg: str = "",
                      at: int = 0):
        """One clip at a time, best first. Deciding takes it out of the list, so the same
        position shows the next one; Skip moves along."""
        clips = _clip_rows(conn, "in_review", bid)
        at = max(0, min(at, len(clips) - 1)) if clips else 0
        here = f"/campaigns/{cid}/bounties/{bid}/review" + (f"?at={at}" if at else "")
        return _tab(request, conn, cid, bid, "review", clip=clips[at] if clips else None,
                    at=at, total=len(clips), reasons=REJECT_REASONS, msg=msg, here=here)

    @app.get("/campaigns/{cid}/bounties/{bid}/post")
    def bounty_post(request: Request, cid: int, bid: int, conn: DB, msg: str = ""):
        # Approved clips, plus posted ones that still have an allowed platform to go.
        clips = _clip_rows(conn, "approved", bid) + [
            r for r in _clip_rows(conn, "posted", bid) if r["todo"]]
        b = _bounty_or_404(conn, cid, bid)
        c = _camp_or_404(conn, cid)
        tags = f"{c['required_tags']} {b['required_tags']}"
        posted_hooks = {r[0].strip().lower() for r in conn.execute(
            "SELECT DISTINCT ca.hook_text FROM posts p JOIN clips k ON k.id=p.clip_id JOIN"
            " candidates ca ON ca.id=k.candidate_id")}
        for k in clips:
            k["problems"] = posting.caption_problems(k["caption"], tags, c["disclosure"])
            k["same_hook"] = not k["posts"] and k["hook_text"].strip().lower() in posted_hooks
        accts = [a for a in posting.accounts(conn, enabled_only=True)]
        return _tab(request, conn, cid, bid, "post", clips=clips, platforms=PLATFORMS,
                    accounts=accts,
                    reminders=PLATFORM_REMINDER, msg=msg,
                    tt={"configured": tiktok.configured(s), "connected": bool(tiktok.auth(conn)),
                        "uploads": tiktok.latest_uploads(conn, [c["id"] for c in clips])})

    @app.get("/campaigns/{cid}/bounties/{bid}/posted")
    def bounty_posted(request: Request, cid: int, bid: int, conn: DB, msg: str = ""):
        return _tab(request, conn, cid, bid, "posted", clips=_clip_rows(conn, "posted", bid),
                    platforms=PLATFORMS, accounts=posting.accounts(conn, enabled_only=True),
                    handles={a["id"]: a["handle"] for a in posting.accounts(conn)}, msg=msg)

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
    def log_post(clip_id: int, conn: DB, platform: str = Form(""), url: str = Form(...),
                 account_id: str = Form(""), next: str = Form("")):
        to = _next(next, "/approved")
        acct = conn.execute("SELECT * FROM accounts WHERE id=?",
                            (int(account_id),)).fetchone() if account_id.isdigit() else None
        platform = acct["platform"] if acct else platform
        if err := _post_fields(platform, url):
            return back(to, err)
        if other := posting.already_posted(conn, clip_id, platform):
            return back(to, f"Not logged: clip #{other} already put this moment on {platform}."
                        " Post this one somewhere else.")
        try:
            with tx(conn):
                conn.execute("INSERT INTO posts (clip_id, platform, url, account_id)"
                             " VALUES (?,?,?,?)",
                             (clip_id, platform, url.strip(), acct["id"] if acct else None))
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
        conn.execute("UPDATE posts SET submitted_at=?, review_status=? WHERE id=?",
                     (None, None, post_id) if undo else (utcnow(), "pending", post_id))
        return back(_next(next, "/posts"), "Marked not submitted" if undo else
                    "Marked submitted: now waiting for the brand's review")

    @app.post("/posts/{post_id}/review")
    def post_review(post_id: int, conn: DB, step: str = Form(...), reason: str = Form(""),
                    approved_views: str = Form(""), earned_usd: str = Form(""),
                    next: str = Form("")):
        to = _next(next, "/posts")
        try:
            msg = posting.set_review(
                conn, post_id, step, reason=reason,
                approved_views=int(_num(approved_views)) if _num(approved_views) else None,
                earned_usd=_num(earned_usd))
        except ValueError as e:
            return back(to, str(e))
        return back(to, msg)

    @app.get("/accounts")
    def accounts_page(request: Request, conn: DB, msg: str = ""):
        return page(request, "accounts.html", accounts=posting.accounts(conn),
                    today=posting.cadence(conn), msg=msg)

    @app.post("/accounts")
    def accounts_add(conn: DB, platform: str = Form(...), handle: str = Form(...),
                     niche: str = Form("")):
        if platform not in PLATFORMS:
            return back("/accounts", "Pick a platform")
        try:
            posting.add_account(conn, platform, handle, niche)
        except ValueError as e:
            return back("/accounts", str(e))
        return back("/accounts", "Account added" + (
            ": off for now (Instagram stops recommending accounts that mostly repost)"
            if platform == "instagram" else ""))

    @app.post("/accounts/{account_id}/enabled")
    def accounts_enable(account_id: int, conn: DB, on: str = Form(...)):
        conn.execute("UPDATE accounts SET enabled=? WHERE id=?", (int(on == "1"), account_id))
        return back("/accounts", "Turned on" if on == "1" else "Turned off")

    @app.post("/clips/{clip_id}/caption/fix")
    def fix_caption(clip_id: int, conn: DB, next: str = Form("")):
        r = conn.execute(
            "SELECT k.caption, cp.required_tags ct, b.required_tags bt, cp.disclosure FROM clips k"
            " JOIN candidates c ON c.id=k.candidate_id JOIN sources s ON s.id=c.source_id"
            " JOIN campaigns cp ON cp.id=s.campaign_id LEFT JOIN bounties b ON b.id=s.bounty_id"
            " WHERE k.id=?", (clip_id,)).fetchone()
        if r:
            conn.execute("UPDATE clips SET caption=? WHERE id=?", (posting.fix_caption(
                r["caption"], f"{r['ct']} {r['bt'] or ''}", r["disclosure"]), clip_id))
        return back(_next(next, "/approved") + f"#clip{clip_id}", "Caption fixed")

    @app.post("/clips/{clip_id}/unreview")
    def unreview(clip_id: int, conn: DB, next: str = Form("")):
        conn.execute("UPDATE clips SET status='in_review' WHERE id=? AND status IN"
                     " ('approved','rejected') AND file_deleted_at IS NULL", (clip_id,))
        return back(_next(next, "/review"), "Moved back to review")

    @app.post("/posts/{post_id}/views")
    def add_views(post_id: int, conn: DB, views: int = Form(...), likes: str = Form(""),
                  engaged: str = Form(""), next: str = Form("")):
        conn.execute("INSERT INTO view_snapshots (post_id, views, likes, engaged_views)"
                     " VALUES (?,?,?,?)",
                     (post_id, views, int(likes) if likes.strip().isdigit() else None,
                      int(engaged) if engaged.strip().isdigit() else None))
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
    def campaigns(request: Request, conn: DB, msg: str = "", m: str = ""):
        # Query params pre-fill the Add form (the Scout page's "Add as campaign" link).
        prefill = {k: v for k, v in request.query_params.items() if k not in ("msg", "m")}
        if prefill.get("rate_mode", "").isdigit():
            prefill["rate_per_views"] = int(prefill["rate_mode"])
        for k in ("rate_usd", "min_payout_usd", "cap_usd", "flat_fee_bonus_usd",
                  "budget_total_usd", "budget_remaining_usd", "min_views"):
            if k in prefill:
                prefill[k] = _num(prefill[k])
        camps = list_campaigns(conn)
        used = {c["marketplace"] for c in camps}
        if m in money.MARKETPLACES:
            camps = [c for c in camps if c["marketplace"] == m]
        for c in camps:
            c["bounties"] = list_bounties(conn, c["id"])
            for b in c["bounties"]:
                b["prog"] = inbox.progress(conn, b)
        # A streamer added on the Lives tab, with no paid bounty, lives there only.
        camps = [c for c in camps if c["bounties"] or not (c["watch_live"] or c["live_channels"])]
        return page(request, "campaigns.html", campaigns=camps, msg=msg, prefill=prefill,
                    market=m if m in money.MARKETPLACES else "", markets_used=used)

    @app.post("/campaigns")
    async def create_campaign(request: Request, conn: DB):
        """Add a campaign with its first bounty. A campaign that already exists (same name) just
        gets the new bounty, so Scout's "Add" works for a second episode too."""
        raw = dict(await request.form())
        form = {k: v for k, v in raw.items() if v not in ("", None)}
        force = form.pop("force", None) == "on"
        clip_live = form.pop("live_clip", None) == "true"
        chans, bad = live.parse_channels(form.get("live_channels", ""))
        if bad:
            return back("/campaigns", f"Not a Kick or Twitch channel: {' '.join(bad)}"
                        " (use e.g. kick.com/n3on)")
        form["live_channels"] = " ".join(chans)
        form["watch_live"] = bool(chans) or clip_live
        try:
            terms = terms_from(raw)
        except ValueError as e:
            return back("/campaigns", str(e))
        for k in (*terms, "rate_mode"):
            form.pop(k, None)
        form["allowed_platforms"] = ",".join(
            p for p in PLATFORMS if raw.get(f"pf_{p}")) or form.get("allowed_platforms",
                                                                    "tiktok,youtube")
        bounty = {"live_clip": clip_live, "render_hold": clip_live,
                  "name": form.pop("bounty_name", "main"), **terms}
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
    async def bounty_add(request: Request, cid: int, conn: DB):
        _camp_or_404(conn, cid)
        form = dict(await request.form())
        name = (form.get("name") or "").strip()
        try:
            bid = add_bounty(conn, cid, BountyIn(name=name, **terms_from(form)))
        except ValueError as e:
            return back(f"/campaigns/{cid}", str(e))
        return back(f"/campaigns/{cid}/bounties/{bid}", f"Bounty {name} added")

    @app.post("/campaigns/{cid}/settings")
    async def campaign_settings(request: Request, cid: int, conn: DB):
        form = dict(await request.form())
        plats = ",".join(p for p in PLATFORMS if form.get(f"pf_{p}")) or "tiktok,youtube"
        conn.execute("UPDATE campaigns SET required_tags=?, allowed_platforms=?, notes=?,"
                     " marketplace=?, category=? WHERE id=?",
                     ((form.get("required_tags") or "").strip(), plats,
                      (form.get("notes") or "").strip(),
                      form.get("marketplace") if form.get("marketplace") in money.MARKETPLACES
                      else "other", (form.get("category") or "").strip(), cid))
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

    def _time(conn, bid: int) -> dict:
        r = conn.execute("SELECT COALESCE(SUM(minutes), 0) m, COUNT(*) n FROM work_log"
                         " WHERE bounty_id=?", (bid,)).fetchone()
        return {"minutes": r["m"], "entries": r["n"]}

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
                    tabs=clip_counts(conn, bid), prog=inbox.progress(conn, b),
                    holds=bounties.hold_reasons(conn, b),
                    held=bounties.held_count(conn, bid),
                    sessions=live.sessions(conn, bounty_id=bid), time=_time(conn, bid),
                    target=s.target_hourly_usd)

    @app.post("/campaigns/{cid}/bounties/{bid}/settings")
    async def bounty_settings(request: Request, cid: int, bid: int, conn: DB):
        b = _bounty_or_404(conn, cid, bid)
        form = dict(await request.form())
        try:
            t = terms_from(form)
        except ValueError as e:
            return back(f"/campaigns/{cid}/bounties/{bid}", str(e))
        t |= {"required_tags": (form.get("required_tags") or "").strip(),
              "notes": (form.get("notes") or "").strip(),
              "auto_queue": int(form.get("auto_queue") == "true"),
              "clips_wanted": _clips(int(_num(form.get("clips_wanted")) or 6)),
              "live_clip": int(form.get("live_clip") == "true"),
              "render_hold": int(form.get("render_hold") == "true")}
        if t["budget_remaining_usd"] != b["budget_remaining_usd"]:
            t["budget_checked_at"] = utcnow()
        render_hold = t["render_hold"]
        with tx(conn):
            conn.execute(f"UPDATE bounties SET {', '.join(f'{k}=?' for k in t)} WHERE id=?",
                         (*t.values(), bid))
            if b["render_hold"] and not render_hold:
                bounties.release(conn, bid)  # unticking the hold renders what was waiting
        return back(f"/campaigns/{cid}/bounties/{bid}", "Saved")

    @app.post("/campaigns/{cid}/bounties/{bid}/time")
    def bounty_time(cid: int, bid: int, conn: DB, minutes: int = Form(...), note: str = Form(""),
                    day: str = Form("")):
        _bounty_or_404(conn, cid, bid)
        if not 0 < minutes <= 24 * 60:
            return back(f"/campaigns/{cid}/bounties/{bid}#time", "Minutes: 1 to 1440")
        conn.execute("INSERT INTO work_log (bounty_id, minutes, day, note) VALUES (?,?,?,?)",
                     (bid, minutes, day or posting.today_local().isoformat(), note.strip()))
        return back(f"/campaigns/{cid}/bounties/{bid}#time", f"Logged {minutes} min")

    @app.post("/campaigns/{cid}/bounties/{bid}/release")
    def bounty_release(cid: int, bid: int, conn: DB):
        _bounty_or_404(conn, cid, bid)
        with tx(conn):
            n = bounties.release(conn, bid)
        return back(f"/campaigns/{cid}/bounties/{bid}",
                    f"Rendering {n} clip(s) now" if n else "No longer holding renders")

    @app.post("/campaigns/{cid}/live")
    def campaign_live(cid: int, conn: DB, live_channels: str = Form(""),
                      watch_live: bool = Form(False), live_layout: str = Form("")):
        _camp_or_404(conn, cid)
        chans, bad = live.parse_channels(live_channels)
        if bad:
            return back("/lives?tab=streamers", f"Not a Kick or Twitch channel:"
                        f" {' '.join(bad)} (use e.g. kick.com/n3on)")
        conn.execute("UPDATE campaigns SET live_channels=?, watch_live=?, live_layout=? WHERE id=?",
                     (" ".join(chans), int(watch_live),
                      live_layout if live_layout in ("blurfill", "crop") else None, cid))
        return back("/lives?tab=streamers", "Saved. Channels are checked every"
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
                    m=analytics.money_stats(conn),
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
