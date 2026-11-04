"""Bounties: the paid units inside a campaign (Man On → episode-4, episode-5…). Each has its own
rate, budget threshold, end date, requirements and footage folder; sources belong to one."""

import re
import sqlite3

from pydantic import BaseModel, Field

from clipforge import jobqueue
from clipforge.briefs import merged
from clipforge.campaigns import effective_per_1k, folder_name, logo_list
from clipforge.db import utcnow
from clipforge.money import min_views_for


class BountyIn(BaseModel):
    name: str
    rate_usd: float = 0.0
    rate_per_views: int = 1000
    min_views: int = 0
    cap_usd: float | None = None
    end_date: str | None = None
    status: str = Field(default="active")
    required_tags: str = ""
    notes: str = ""
    source_links: str = ""
    live_clip: bool = False  # clip the streamer's live streams into this bounty (D30)
    render_hold: bool = False  # pick clips, but render only once the requirements are in
    # Content Rewards terms (D38). min_payout_usd decides min_views (money.min_views_for).
    campaign_type: str = "cpm"  # cpm | per_post | retainer | prize_pool
    min_payout_usd: float | None = None
    flat_fee_bonus_usd: float | None = None
    budget_total_usd: float | None = None
    budget_remaining_usd: float | None = None
    audience_rules: str = ""  # "50%+ US viewers"
    footage_scope: str = ""  # "the sponsor segment only"
    ai_video_banned: bool = False
    onscreen_ad_label_required: bool = False
    brief_url: str = ""  # the campaign page, where each clip is submitted
    brief_text: str = ""
    pays_on: str = "unknown"  # views | engaged_views | unknown (YouTube)
    people: str = ""  # D40: "Lacy, StableRonaldo (Ron)": names the hooks use
    must_mention: str = ""  # D40: the caption and hook must name one of these
    mention_all: bool = False  # D47: the caption must name every one of them
    min_clip_s: float | None = None  # D47: the brief's clip length (None: the default)
    max_clip_s: float | None = None


def bounty_folder(campaign: sqlite3.Row | dict, bounty_name: str) -> str:
    """Campaigns/<campaign folder>/<bounty>: the campaign's folder (its name, or the folder you
    already use, like ManOn), then the bounty's name. A campaign's unnamed default bounty
    ("main") uses the campaign folder itself."""
    base = campaign["media_folder"] or folder_name(campaign["name"])
    return base if bounty_name == "main" else f"{base}/{folder_name(bounty_name)}"


def add_bounty(conn: sqlite3.Connection, campaign_id: int, b: BountyIn) -> int:
    camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not camp:
        raise ValueError(f"campaign {campaign_id} not found")
    name = b.name.strip()
    if not name:
        raise ValueError("Give the bounty a name, e.g. episode-4")
    if conn.execute("SELECT 1 FROM bounties WHERE campaign_id=? AND lower(name)=lower(?)",
                    (campaign_id, name)).fetchone():
        raise ValueError(f"{camp['name']} already has a bounty called {name}")
    data = {**b.model_dump(), "name": name, "campaign_id": campaign_id,
            "media_folder": bounty_folder(camp, name)}
    data["min_views"] = min_views_for(data)
    if data["budget_remaining_usd"] is not None:
        data["budget_checked_at"] = utcnow()
    cur = conn.execute(f"INSERT INTO bounties ({','.join(data)}) VALUES"
                       f" ({','.join('?' * len(data))})", tuple(data.values()))
    return cur.lastrowid


def get_bounty(conn: sqlite3.Connection, bounty_id: int) -> dict | None:
    r = conn.execute(
        "SELECT b.*, c.name campaign, c.required_tags campaign_tags, c.marketplace FROM bounties b"
        " JOIN campaigns c ON c.id=b.campaign_id WHERE b.id=?", (bounty_id,)).fetchone()
    return _with_rate(r) if r else None


def _count(status: str) -> str:
    return (f"(SELECT COUNT(*) FROM clips k JOIN candidates ca ON ca.id=k.candidate_id"
            f" JOIN sources s ON s.id=ca.source_id WHERE s.bounty_id=b.id"
            f" AND k.status='{status}') {status}")


# Per-bounty clip counts by stage, for the tabs and the campaign list.
CLIP_COUNTS = ", ".join(_count(st) for st in ("in_review", "approved", "posted"))


def clip_counts(conn: sqlite3.Connection, bounty_id: int) -> dict:
    return dict(conn.execute(f"SELECT {CLIP_COUNTS} FROM bounties b WHERE b.id=?",
                             (bounty_id,)).fetchone())


def list_bounties(conn: sqlite3.Connection, campaign_id: int) -> list[dict]:
    rows = conn.execute(
        f"""SELECT b.*, c.name campaign, c.marketplace,
                  (SELECT COUNT(*) FROM sources s WHERE s.bounty_id=b.id) sources,
                  (SELECT COUNT(*) FROM clips k JOIN candidates ca ON ca.id=k.candidate_id
                   JOIN sources s ON s.id=ca.source_id WHERE s.bounty_id=b.id) clips,
                  {CLIP_COUNTS}
           FROM bounties b JOIN campaigns c ON c.id=b.campaign_id
           WHERE b.campaign_id=? AND b.live_session_id IS NULL
           ORDER BY b.status='ended', b.id""", (campaign_id,)).fetchall()
    return [_with_rate(r) for r in rows]


def default_bounty(conn: sqlite3.Connection, campaign_id: int) -> int:
    """Where footage goes when no bounty is named: the campaign's only (or first active) one.
    Never a live stream's own bounty (live.own_bounty)."""
    r = conn.execute("SELECT id FROM bounties WHERE campaign_id=? AND live_session_id IS NULL"
                     " ORDER BY status!='active', id LIMIT 1", (campaign_id,)).fetchone()
    if not r:
        raise ValueError("This campaign has no bounties yet. Add one first.")
    return r["id"]


def paid_bounties(conn: sqlite3.Connection) -> list[dict]:
    """Active bounties a live stream can be moved into, labeled "N3on · halloween"."""
    return [{**dict(r), "label": label(r["campaign"], r["name"])} for r in conn.execute(
        "SELECT b.id, b.name, b.campaign_id, c.name campaign FROM bounties b JOIN campaigns c"
        " ON c.id=b.campaign_id WHERE b.status='active' AND c.status='active'"
        " AND b.live_session_id IS NULL ORDER BY c.name, b.id")]


def label(campaign: str, bounty: str | None) -> str:
    return f"{campaign} · {bounty}" if bounty and bounty != "main" else campaign


def _with_rate(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["per_1k"] = effective_per_1k(d["rate_usd"], d["rate_per_views"])
    return d


# A brief that bans logos or makes them optional doesn't need one; anything else that talks
# about logos does. Unclear cases wait, and "Render now" overrides.
_NO_LOGO = re.compile(r"\b(no|without|banned|prohibited|not allowed|optional|not required|"
                      r"not needed|don'?t (add|use|include|put))\b", re.I)


def hold_reasons(conn: sqlite3.Connection, b: sqlite3.Row | dict) -> list[str]:
    """What a held bounty still needs before its clips render. Empty = nothing (or not held)."""
    if not b["render_hold"]:
        return []
    docs = conn.execute("SELECT d.applied_at, j.state FROM campaign_docs d LEFT JOIN jobs j ON"
                        " j.dedupe_key='brief:'||d.id WHERE d.bounty_id=?", (b["id"],)).fetchall()
    out = []
    if not docs:
        out.append("requirement screenshots")
    elif any(d["state"] in ("queued", "running") for d in docs):
        out.append("requirements (still being read)")
    elif not any(d["applied_at"] for d in docs):
        out.append("“Apply to bounty” on the requirements")
    rule = " ".join(merged(conn, b["id"])["logo_rule"])
    logos = conn.execute("SELECT logo_path FROM campaigns WHERE id=?",
                         (b["campaign_id"],)).fetchone()["logo_path"]
    if re.search(r"\blogos?\b", rule, re.I) and not _NO_LOGO.search(rule) \
            and not logo_list(logos):
        out.append("campaign logo")
    return out


# Kept candidates of a bounty that have no clip yet.
_UNRENDERED = """SELECT c.id, c.source_id, s.media_host FROM candidates c
                 JOIN sources s ON s.id=c.source_id
                 WHERE s.bounty_id=? AND c.kept=1
                   AND NOT EXISTS (SELECT 1 FROM clips k WHERE k.candidate_id=c.id)"""


def held_count(conn: sqlite3.Connection, bounty_id: int) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM ({_UNRENDERED}) u WHERE NOT EXISTS (SELECT 1"
                        " FROM jobs j WHERE j.dedupe_key='render:'||u.id)",
                        (bounty_id,)).fetchone()[0]


def release(conn: sqlite3.Connection, bounty_id: int) -> int:
    """Stop holding and queue every picked clip that hasn't rendered. Returns renders queued."""
    conn.execute("UPDATE bounties SET render_hold=0 WHERE id=?", (bounty_id,))
    n = 0
    for c in conn.execute(_UNRENDERED, (bounty_id,)).fetchall():
        n += jobqueue.enqueue(conn, "render", f"render:{c['id']}", source_id=c["source_id"],
                              candidate_id=c["id"], affinity_host=c["media_host"],
                              priority=80) is not None
    return n


def release_ready(conn: sqlite3.Connection, campaign_id: int | None = None) -> int:
    """Release every held bounty that now has everything it needs."""
    rows = conn.execute("SELECT * FROM bounties WHERE render_hold=1 AND (? IS NULL OR"
                        " campaign_id=?)", (campaign_id, campaign_id)).fetchall()
    return sum(release(conn, b["id"]) for b in rows if not hold_reasons(conn, b))
