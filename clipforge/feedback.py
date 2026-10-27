"""Feedback loop: turn review decisions and view counts into few-shot guidance for the selector.

Deliberately simple and explainable: the prompt gets the best-performing approved hooks and the
most recent rejections with their reasons. No model training, no embeddings.
"""

import random
import sqlite3

from clipforge.select_prompts import active_versions

REJECT_REASONS = {
    "weak_hook": "hook doesn't grab in 2 seconds",
    "no_payoff": "no payoff / ends flat",
    "needs_context": "needs earlier context",
    "boring": "boring / low energy",
    "bad_cut": "starts or ends at a bad spot",
    "too_long": "too long / slow",
    "off_brand": "doesn't fit the campaign",
    "caption_issue": "captions wrong or ugly",
    "hook_text": "on-screen hook text is wrong or misspelled",
    "other": "other",
}


def build_feedback(conn: sqlite3.Connection, campaign_id: int | None = None, k: int = 6) -> str:
    where = "AND s.campaign_id = ?" if campaign_id else ""
    args = (campaign_id,) if campaign_id else ()
    liked = conn.execute(
        f"""SELECT c.hook_text, c.hook_type, (c.end_ms-c.start_ms)/1000 AS secs,
                   COALESCE(MAX(v.views),0) AS views
            FROM clips k JOIN candidates c ON c.id=k.candidate_id
            JOIN sources s ON s.id=c.source_id
            LEFT JOIN posts p ON p.clip_id=k.id LEFT JOIN view_snapshots v ON v.post_id=p.id
            WHERE k.status IN ('approved','posted') {where}
            GROUP BY k.id ORDER BY views DESC, k.id DESC LIMIT ?""",
        (*args, k),
    ).fetchall()
    disliked = conn.execute(
        f"""SELECT c.hook_text, c.hook_type, r.reason, r.note
            FROM reviews r JOIN clips k ON k.id=r.clip_id JOIN candidates c ON c.id=k.candidate_id
            JOIN sources s ON s.id=c.source_id
            WHERE r.decision='rejected' {where} ORDER BY r.id DESC LIMIT ?""",
        (*args, k),
    ).fetchall()
    lines = []
    for r in liked:
        views = f", {r['views']:,} views" if r["views"] else ""
        lines.append(f"- LIKED: \"{r['hook_text']}\" ({r['hook_type']}, {r['secs']}s{views})")
    for r in disliked:
        why = "; ".join(REJECT_REASONS.get(k, k) for k in (r["reason"] or "").split(",") if k)
        why = "; ".join(x for x in (why, r["note"]) if x) or "no reason given"
        lines.append(f"- REJECTED: \"{r['hook_text']}\" ({r['hook_type']}): {why}")
    return "\n".join(lines)


def pick_prompt_version(conn: sqlite3.Connection, rng: random.Random | None = None):
    """Weighted random choice among active prompt versions (the A/B split)."""
    versions = active_versions(conn)
    if not versions:
        raise RuntimeError("no active prompt versions")
    rng = rng or random.Random()
    return rng.choices(versions, weights=[v["weight"] for v in versions], k=1)[0]
