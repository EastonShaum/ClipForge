"""Stage orchestration, split in three so the same handlers run locally or on a remote worker:

  build_context(conn, job)  -> dict      (Pi only: reads the DB, checks budget)
  run_handler(kind, ctx)    -> dict      (any host: does the actual work, touches no DB)
  apply_result(conn, job, result)        (Pi only: persists output, enqueues the next stage)
"""

import json
import logging
import sqlite3
from pathlib import Path

from clipforge import costs, jobqueue, live, notify, scout
from clipforge.campaigns import logo_list
from clipforge.captions import build_caption
from clipforge.config import Settings, get_settings
from clipforge.db import tx, utcnow
from clipforge.feedback import build_feedback, pick_prompt_version
from clipforge.stages import brief as brief_stage
from clipforge.stages import fetch as fetch_stage
from clipforge.stages import render as render_stage
from clipforge.stages import scout as scout_stage
from clipforge.stages import select as select_stage
from clipforge.stages import transcribe as transcribe_stage

log = logging.getLogger(__name__)

PAID_STAGES = {"transcribe", "select", "scout", "brief"}


class Deferred(Exception):
    """Job can't run right now (budget, disk, missing key). Re-queue without burning an attempt."""

    def __init__(self, reason: str, seconds: float = 3600):
        super().__init__(reason)
        self.seconds = seconds


def _source(conn, sid: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT s.*, c.category, c.name AS campaign_name, c.disclosure, c.logo_path FROM sources s"
        " JOIN campaigns c ON c.id = s.campaign_id WHERE s.id = ?", (sid,)
    ).fetchone()


def _words(conn, sid: int) -> list[dict]:
    row = conn.execute("SELECT words_json FROM transcripts WHERE source_id=?", (sid,)).fetchone()
    return json.loads(row["words_json"]) if row else []


def build_context(conn: sqlite3.Connection, s: Settings, job: sqlite3.Row) -> dict:
    kind = job["kind"]
    if kind == "fetch":
        src = _source(conn, job["source_id"])
        return {"source_id": src["id"], "url": src["url"]}
    if kind == "transcribe":
        src = _source(conn, job["source_id"])
        if s.transcribe_provider in ("groq", "openai"):
            try:
                costs.check_budget(conn, costs.stt_cost(s.transcribe_provider, s.transcribe_model,
                                                        src["duration_s"] or 3600))
            except costs.BudgetExceeded as e:
                raise Deferred(str(e), 6 * 3600) from e
        return {"source_id": src["id"], "media_path": src["media_path"]}
    if kind == "select":
        src = _source(conn, job["source_id"])
        words = _words(conn, src["id"])
        stream = live.stream_info(conn, src["id"])
        pv = pick_prompt_version(conn, live=bool(stream))
        prompt = select_stage.build_prompt(
            s, pv["body"], words=words, title=src["title"], category=src["category"],
            feedback=build_feedback(conn, None), live=stream,
        )
        try:
            costs.check_budget(conn, select_stage.estimate_usd(s, prompt))
        except costs.BudgetExceeded as e:
            raise Deferred(str(e), 6 * 3600) from e
        lengths = ((s.live_min_clip_s, s.live_max_clip_s) if stream
                   else (s.min_clip_s, s.max_clip_s))
        return {"source_id": src["id"], "prompt": prompt, "words": words,
                "prompt_version_id": pv["id"], "min_s": lengths[0], "max_s": lengths[1]}
    if kind == "render":
        cand = conn.execute("SELECT * FROM candidates WHERE id=?", (job["candidate_id"],)).fetchone()
        src = _source(conn, cand["source_id"])
        info = live.stream_info(conn, src["id"])
        return {"candidate_id": cand["id"], "source_id": src["id"],
                "media_path": src["media_path"],
                "start_ms": cand["start_ms"], "end_ms": cand["end_ms"],
                "hook": cand["hook_text"], "out_rel": f"{src['id']}/c{cand['id']}.mp4",
                "disclosure": src["disclosure"], "logo_rels": logo_list(src["logo_path"]),
                "layout": (info["live_layout"] or s.live_layout) if info else None}
    if kind == "scout":
        shot = _shot(conn, job)
        try:
            costs.check_budget(conn, scout_stage.estimate_usd(s))
        except costs.BudgetExceeded as e:
            raise Deferred(str(e), 6 * 3600) from e
        return {"shot_id": shot["id"], "image_rel": shot["path"]}
    if kind == "brief":
        doc = conn.execute("SELECT * FROM campaign_docs WHERE id=?",
                           (int(job["dedupe_key"].split(":")[1]),)).fetchone()
        try:
            costs.check_budget(conn, brief_stage.estimate_usd(s))
        except costs.BudgetExceeded as e:
            raise Deferred(str(e), 6 * 3600) from e
        return {"doc_id": doc["id"], "image_rel": doc["path"]}
    raise ValueError(f"unknown job kind {kind}")


def _shot(conn, job: sqlite3.Row) -> sqlite3.Row:
    """Scout jobs carry their screenshot id in the dedupe key: scout:<shot_id>."""
    shot_id = int(job["dedupe_key"].split(":")[1])
    return conn.execute("SELECT * FROM scout_shots WHERE id=?", (shot_id,)).fetchone()


def run_handler(s: Settings, kind: str, ctx: dict, out_dir: Path | None = None) -> dict:
    """Do the work. Pure with respect to the DB, so a remote worker can run it."""
    work = s.work_dir / f"{kind}-{ctx.get('source_id')}-{ctx.get('candidate_id', 0)}"
    try:
        if kind == "fetch":
            return fetch_stage.fetch(s, ctx["source_id"], ctx["url"])
        if kind == "transcribe":
            return transcribe_stage.transcribe(s, ctx["media_path"], work)
        if kind == "select":
            cands, meta = select_stage.select(s, ctx["prompt"], ctx["words"],
                                              ctx.get("min_s"), ctx.get("max_s"))
            return {"candidates": cands, "meta": meta}
        if kind == "render":
            out = (out_dir or s.clips_dir) / ctx["out_rel"]
            return render_stage.render(s, media_path=ctx["media_path"],
                                       start_ms=ctx["start_ms"], end_ms=ctx["end_ms"],
                                       hook=ctx["hook"] if s.hook_card else "", out_path=out, work=work,
                                       layout=ctx.get("layout"),
                                       disclosure=(_onscreen_disclosure(ctx.get("disclosure"))
                                                   if s.onscreen_disclosure else ""),
                                       logo_paths=_logos(s, ctx.get("logo_rels", [])))
        if kind == "scout":
            bounties, meta = scout_stage.read_screenshot(s, s.assets_dir / ctx["image_rel"])
            return {"shot_id": ctx["shot_id"], "bounties": bounties, "meta": meta}
        if kind == "brief":
            brief, meta = brief_stage.read_brief(s, s.assets_dir / ctx["image_rel"])
            return {"doc_id": ctx["doc_id"], "brief": brief, "meta": meta}
    except fetch_stage.DiskFull as e:
        raise Deferred(str(e), 1800) from e
    except transcribe_stage.MissingKey as e:
        raise Deferred(str(e), 6 * 3600) from e
    finally:
        _rm_tree(work)
    raise ValueError(f"unknown job kind {kind}")


def _logos(s: Settings, rels: list[str]) -> list[str]:
    """Campaign logos are requirements, so a missing file is an error, never a silent skip."""
    paths = [s.assets_dir / rel for rel in rels]
    for rel, p in zip(rels, paths, strict=True):
        if not p.is_file():
            raise FileNotFoundError(f"campaign logo {rel} missing on this machine")
    return [str(p) for p in paths]


def _onscreen_disclosure(text: str | None) -> str:
    """'#ad' alone is easy to miss on screen; spell it out."""
    text = (text or "").strip()
    return "Paid partnership #ad" if text.lower() in ("#ad", "ad") else text


def _rm_tree(p: Path) -> None:
    if p.exists():
        for f in sorted(p.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        p.rmdir()


def apply_result(conn: sqlite3.Connection, s: Settings, job: sqlite3.Row, result: dict,
                 host: str) -> None:
    kind, now = job["kind"], utcnow()
    with tx(conn):
        if kind == "fetch":
            sid = job["source_id"]
            conn.execute(
                "UPDATE sources SET media_host=?, media_path=?, title=COALESCE(NULLIF(title,''),?),"
                " duration_s=?, status='fetched', error=NULL, updated_at=? WHERE id=?",
                (host, result["media_path"], result.get("title", ""), result.get("duration_s"),
                 now, sid),
            )
            jobqueue.enqueue(conn, "transcribe", f"transcribe:{sid}", source_id=sid,
                             affinity_host=host, priority=60)
        elif kind == "transcribe":
            sid = job["source_id"]
            words = result["words"]
            conn.execute(
                "INSERT INTO transcripts (source_id, provider, model, language, words_json, text)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(source_id) DO NOTHING",
                (sid, result["provider"], result["model"], result.get("language"),
                 json.dumps(words, separators=(",", ":")), " ".join(w["w"] for w in words)),
            )
            costs.record(conn, provider=result["provider"], model=result["model"],
                         stage="transcribe", units=f"{result.get('audio_s', 0):.0f} audio s",
                         usd=result.get("usd", 0.0), source_id=sid)
            conn.execute("UPDATE sources SET status='transcribed', updated_at=? WHERE id=?",
                         (now, sid))
            jobqueue.enqueue(conn, "select", f"select:{sid}", source_id=sid,
                             affinity_host=s.host_name, priority=70)
        elif kind == "select":
            sid = job["source_id"]
            meta = result["meta"]
            pv_id = result.get("prompt_version_id")
            src = conn.execute(
                "SELECT s.media_host, s.skip_ms, COALESCE(b.render_hold, 0) hold,"
                " COALESCE(s.clips_wanted, b.clips_wanted, ?) keep"
                " FROM sources s LEFT JOIN bounties b ON b.id=s.bounty_id WHERE s.id=?",
                (s.candidates_keep, sid)).fetchone()
            n_kept = 0
            for rank, c in enumerate(result["candidates"], start=1):
                # A live chunk starts with the end of the previous one (D30); a moment wholly
                # inside that overlap was already offered there.
                kept = n_kept < src["keep"] and c["end_ms"] > src["skip_ms"]
                n_kept += kept
                cur = conn.execute(
                    """INSERT INTO candidates (source_id, start_ms, end_ms, hook_text, hook_type,
                           title, rationale, score, rank, kept, prompt_version_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_id, start_ms, end_ms) DO NOTHING""",
                    (sid, c["start_ms"], c["end_ms"], c["hook_text"], c["hook_type"],
                     c.get("title", ""), c.get("rationale", ""), c["score"], rank, int(kept), pv_id),
                )
                # A held bounty renders once its requirements are in (bounties.release).
                if kept and cur.rowcount and not src["hold"]:
                    jobqueue.enqueue(conn, "render", f"render:{cur.lastrowid}", source_id=sid,
                                     candidate_id=cur.lastrowid, affinity_host=src["media_host"],
                                     priority=80)
            costs.record(conn, provider=meta["provider"], model=meta["model"], stage="select",
                         units=f"{meta.get('tokens_in', 0)} in, {meta.get('tokens_out', 0)} out",
                         usd=meta.get("usd", 0.0), usd_equiv=meta.get("usd_equiv", 0.0),
                         source_id=sid)
            conn.execute("UPDATE sources SET status='selected', updated_at=? WHERE id=?",
                         (now, sid))
        elif kind == "render":
            cand = conn.execute(
                "SELECT c.*, s.campaign_id, b.required_tags bounty_tags FROM candidates c"
                " JOIN sources s ON s.id=c.source_id LEFT JOIN bounties b ON b.id=s.bounty_id"
                " WHERE c.id=?", (job["candidate_id"],)).fetchone()
            camp = conn.execute("SELECT * FROM campaigns WHERE id=?",
                                (cand["campaign_id"],)).fetchone()
            tags = f"{camp['required_tags']} {cand['bounty_tags'] or ''}"
            if stream := live.stream_info(conn, cand["source_id"]):  # D33: #n3on #alabamabarker
                tags += " " + " ".join(live.hashtags(
                    stream["streamer"], stream["guests"], f"{cand['hook_text']} {cand['title']}"))
            caption = build_caption(title=cand["title"], hook=cand["hook_text"],
                                    category=camp["category"], required_tags=tags,
                                    caption_notes=camp["caption_notes"],
                                    disclosure=camp["disclosure"])
            conn.execute(
                """INSERT INTO clips (candidate_id, path, size_bytes, render_s, rendered_by, caption)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(candidate_id, preset) DO UPDATE SET path=excluded.path,
                     size_bytes=excluded.size_bytes, render_s=excluded.render_s,
                     rendered_by=excluded.rendered_by""",
                (cand["id"], result["rel_path"], result["size_bytes"], result["render_s"], host,
                 caption),
            )
        elif kind == "scout":
            meta = result["meta"]
            n = scout.store(conn, result["shot_id"], result["bounties"])
            costs.record(conn, provider=meta["provider"], model=meta["model"], stage="scout",
                         units=f"{meta.get('tokens_in', 0)} in, {meta.get('tokens_out', 0)} out"
                               f" ({n} bounties)", usd=meta.get("usd", 0.0))
        elif kind == "brief":
            meta = result["meta"]
            conn.execute("UPDATE campaign_docs SET extracted=? WHERE id=?",
                         (json.dumps(result["brief"]), result["doc_id"]))
            costs.record(conn, provider=meta["provider"], model=meta["model"], stage="brief",
                         units=f"{meta.get('tokens_in', 0)} in, {meta.get('tokens_out', 0)} out",
                         usd=meta.get("usd", 0.0))
        jobqueue.complete(conn, job["id"])
    if kind == "render":
        _maybe_finish_source(conn, s, job["source_id"])
    if kind == "scout":
        _maybe_finish_scout(conn, s)


def _maybe_finish_source(conn: sqlite3.Connection, s: Settings, sid: int) -> None:
    """When every kept candidate of a source has a clip, mark it rendered and ping Discord."""
    pending = conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE kind='render' AND source_id=? AND state IN"
        " ('queued','running')", (sid,)).fetchone()["n"]
    if pending:
        return
    cur = conn.execute(
        "UPDATE sources SET status='rendered', updated_at=? WHERE id=? AND status != 'rendered'",
        (utcnow(), sid))
    if not cur.rowcount:
        return
    n = conn.execute(
        "SELECT COUNT(*) n FROM clips k JOIN candidates c ON c.id=k.candidate_id"
        " WHERE c.source_id=? AND k.status='in_review'", (sid,)).fetchone()["n"]
    src = _source(conn, sid)
    notify.discord(f"🎬 **{n} new clips ready** from *{src['title'] or src['url']}* "
                   f"({src['campaign_name']})\n{s.public_base_url}/campaigns/"
                   f"{src['campaign_id']}/bounties/{src['bounty_id']}/review")


def _maybe_finish_scout(conn: sqlite3.Connection, s: Settings) -> None:
    """One Discord summary per upload batch: sent when the last screenshot is read."""
    pending = conn.execute("SELECT COUNT(*) n FROM jobs WHERE kind='scout' AND state IN"
                           " ('queued','running')").fetchone()["n"]
    if not pending:
        notify.discord(scout.summary(conn, s.public_base_url))


def mark_failed_source(conn: sqlite3.Connection, job: sqlite3.Row, error: str) -> None:
    if job["source_id"]:
        conn.execute("UPDATE sources SET status='failed', error=?, updated_at=? WHERE id=?",
                     (error[:1000], utcnow(), job["source_id"]))


def run_local_job(conn: sqlite3.Connection, job: sqlite3.Row, s: Settings | None = None) -> str:
    """Execute one claimed job on this host (the Pi). Returns the final job state."""
    s = s or get_settings()
    try:
        ctx = build_context(conn, s, job)
        if job["kind"] == "select":
            prompt_version_id = ctx["prompt_version_id"]
        result = run_handler(s, job["kind"], ctx)
        if job["kind"] == "select":
            result["prompt_version_id"] = prompt_version_id
        if job["kind"] == "render":
            result["rel_path"] = ctx["out_rel"]
        apply_result(conn, s, job, result, host=s.host_name)
        return "done"
    except Deferred as e:
        log.warning("job %s deferred: %s", job["id"], e)
        jobqueue.defer(conn, job["id"], e.seconds, str(e))
        return "deferred"
    except Exception as e:  # noqa: BLE001 - any failure is recorded on the job
        log.exception("job %s (%s) failed", job["id"], job["kind"])
        state = jobqueue.fail(conn, job["id"], f"{type(e).__name__}: {e}")
        if state == "dead":
            mark_failed_source(conn, job, str(e))
        return state
