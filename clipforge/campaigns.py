"""Campaign registry: add/list campaigns, compute effective pay, enforce the blocklist."""

import hashlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from clipforge.config import get_settings
from clipforge.db import tx
from clipforge.tools import ffmpeg


class CampaignIn(BaseModel):
    name: str
    platform: str = ""  # replaced by marketplace (D38); kept for old rows
    marketplace: str = "content_rewards"  # content_rewards | clipping_net | other
    payer_type: str | None = None  # who pays for a streamer's clips; "none" = don't record
    category: str = ""
    rate_usd: float = 0.0
    rate_per_views: int = 1000
    min_views: int = 0
    cap_usd: float | None = None
    allowed_platforms: str = "tiktok,youtube"
    required_tags: str = ""
    caption_notes: str = ""
    disclosure: str = "#ad"
    source_links: str = ""
    watch_feeds: str = ""
    end_date: str | None = None
    notes: str = ""
    status: str = Field(default="active")
    media_folder: str | None = None  # under the media library; defaults to folder_name(name)
    live_channels: str = ""  # "kick.com/n3on twitch.tv/n3on" (D30)
    watch_live: bool = False  # ping and record when they go live, even with no live bounty


def effective_per_1k(rate_usd: float, rate_per_views: int) -> float:
    return rate_usd * 1000 / rate_per_views if rate_per_views else 0.0


def blocked_reason(c: CampaignIn) -> str | None:
    s = get_settings()
    for cat in s.blocked_categories:
        if cat.lower() in c.category.lower():
            return f"blocked category: {cat}"
    for cat in s.terms_needed_categories:
        if cat.lower() in c.category.lower():
            return (f"{cat} campaign: add it only if the brief names the sponsor and its"
                    " disclosure terms")
    return blocked_text(" ".join([c.name, c.category, c.notes, c.source_links]))


def blocked_text(text: str) -> str | None:
    """The blocked keyword this text contains (a campaign, or a live stream's title), or None."""
    hay = text.lower()
    for kw in get_settings().blocked_keywords:
        # Word boundaries so "stake" doesn't block "mistake" or "stakeholder".
        if re.search(rf"\b{re.escape(kw.lower())}\b", hay):
            return f"blocked keyword: {kw}"
    return None


def folder_name(name: str) -> str:
    """A campaign's footage folder: its name minus characters Windows forbids in file names."""
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(". ")
    return clean[:80] or "campaign"


def add_campaign(conn: sqlite3.Connection, c: CampaignIn, *, force: bool = False) -> int:
    reason = blocked_reason(c)
    if reason and not force:
        raise ValueError(f"Campaign rejected by blocklist ({reason}). Use force to override.")
    data = c.model_dump()
    data["media_folder"] = data["media_folder"] or folder_name(c.name)
    cols = ",".join(data)
    cur = conn.execute(
        f"INSERT INTO campaigns ({cols}) VALUES ({','.join('?' * len(data))})",
        tuple(data.values()),
    )
    return cur.lastrowid


def list_campaigns(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    q = "SELECT * FROM campaigns" + (" WHERE status=?" if status else "") + " ORDER BY id DESC"
    rows = conn.execute(q, (status,) if status else ()).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["per_1k"] = effective_per_1k(r["rate_usd"], r["rate_per_views"])
        out.append(d)
    return out


def get_campaign(conn: sqlite3.Connection, campaign_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["per_1k"] = effective_per_1k(r["rate_usd"], r["rate_per_views"])
    return d


LOGO_STORE_W = 480  # stored size; render scales down further. Keeps transfers to the laptop small.


def logo_list(value: str | None) -> list[str]:
    """campaigns.logo_path holds every required logo, comma-separated, in display order."""
    return [v for v in (value or "").split(",") if v]


def add_logo(conn: sqlite3.Connection, campaign_id: int, data: bytes) -> str:
    """Store a required logo (any image ffmpeg reads) as a shrunk PNG with alpha and append it to
    the campaign's logos. The file name carries a content hash, so a replaced logo never hits a
    stale laptop cache."""
    if not conn.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
        raise ValueError(f"campaign {campaign_id} not found")
    s = get_settings()
    rel = f"logos/c{campaign_id}-{hashlib.sha256(data).hexdigest()[:10]}.png"
    dest = s.assets_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Temp dir on the same filesystem as dest so the final rename is atomic.
    with tempfile.TemporaryDirectory(dir=s.work_dir) as tmp:
        src = Path(tmp) / "upload"
        src.write_bytes(data)
        part = Path(tmp) / "logo.png"
        r = subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", str(src), "-frames:v", "1",
                            "-vf", f"scale='min({LOGO_STORE_W},iw)':-1,format=rgba", str(part)],
                           capture_output=True, text=True)
        if r.returncode:
            raise ValueError(f"not a readable image: {r.stderr.strip()[:200]}")
        part.replace(dest)
    have = logo_list(conn.execute("SELECT logo_path FROM campaigns WHERE id=?",
                                  (campaign_id,)).fetchone()[0])
    if rel not in have:
        conn.execute("UPDATE campaigns SET logo_path=? WHERE id=?",
                     (",".join([*have, rel]), campaign_id))
    return rel


def remove_logo(conn: sqlite3.Connection, campaign_id: int, rel: str | None = None) -> None:
    """Drop one logo, or all of them when rel is None."""
    have = logo_list(conn.execute("SELECT logo_path FROM campaigns WHERE id=?",
                                  (campaign_id,)).fetchone()[0])
    keep = [r for r in have if rel is not None and r != rel]
    conn.execute("UPDATE campaigns SET logo_path=? WHERE id=?",
                 (",".join(keep) or None, campaign_id))


def estimate_payout(bounty: dict, views: int) -> float:
    """One clip's pay after the marketplace fee (money.payout: minimum, cap, bonus, type)."""
    from clipforge.money import payout
    return payout(bounty, views)


def _purge(conn: sqlite3.Connection, sids: list[int], doc_ids: list[int]) -> dict:
    """Delete sources and everything made from them, plus brief screenshots, in one
    transaction. Refuses while one of their jobs runs. Returns counts and files to remove."""
    marks = ",".join("?" * len(sids)) or "NULL"
    if conn.execute(f"SELECT 1 FROM jobs WHERE state='running' AND source_id IN ({marks})",
                    sids).fetchone():
        raise ValueError("A job for this is running; try again when it finishes")
    cands = f"SELECT id FROM candidates WHERE source_id IN ({marks})"
    clips = f"SELECT id FROM clips WHERE candidate_id IN ({cands})"
    posts = f"SELECT id FROM posts WHERE clip_id IN ({clips})"
    counts = {
        "sources": len(sids),
        "clips": conn.execute(f"SELECT COUNT(*) FROM ({clips})", sids).fetchone()[0],
        "posts": conn.execute(f"SELECT COUNT(*) FROM ({posts})", sids).fetchone()[0],
    }
    dmarks = ",".join("?" * len(doc_ids)) or "NULL"
    docs = [r[0] for r in conn.execute(f"SELECT path FROM campaign_docs WHERE id IN ({dmarks})",
                                       doc_ids)]
    conn.execute(f"DELETE FROM view_snapshots WHERE post_id IN ({posts})", sids)
    conn.execute(f"DELETE FROM posts WHERE clip_id IN ({clips})", sids)
    conn.execute(f"DELETE FROM reviews WHERE clip_id IN ({clips})", sids)
    conn.execute(f"DELETE FROM jobs WHERE source_id IN ({marks}) OR candidate_id IN ({cands})",
                 [*sids, *sids])
    conn.execute(f"DELETE FROM clips WHERE candidate_id IN ({cands})", sids)
    conn.execute(f"DELETE FROM candidates WHERE source_id IN ({marks})", sids)
    conn.execute(f"DELETE FROM transcripts WHERE source_id IN ({marks})", sids)
    conn.execute(f"DELETE FROM sources WHERE id IN ({marks})", sids)
    for d in doc_ids:
        conn.execute("DELETE FROM jobs WHERE dedupe_key=?", (f"brief:{d}",))
    conn.execute(f"DELETE FROM campaign_docs WHERE id IN ({dmarks})", doc_ids)
    counts["files"] = docs
    return counts


def _remove_files(sids: list[int], rels: list[str]) -> None:
    """ClipForge's own renders, downloads and assets; never the user's footage."""
    s = get_settings()
    for sid in sids:
        for d in (s.clips_dir / str(sid), s.raw_dir / str(sid)):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
    for rel in rels:
        (s.assets_dir / rel).unlink(missing_ok=True)


def delete_bounty(conn: sqlite3.Connection, bounty_id: int) -> dict:
    """Delete one bounty with its sources, clips, posts, views, jobs and brief screenshots.
    Footage files and api_costs stay."""
    sids = [r[0] for r in conn.execute("SELECT id FROM sources WHERE bounty_id=?", (bounty_id,))]
    doc_ids = [r[0] for r in conn.execute("SELECT id FROM campaign_docs WHERE bounty_id=?",
                                          (bounty_id,))]
    with tx(conn):
        counts = _purge(conn, sids, doc_ids)
        conn.execute("DELETE FROM bounties WHERE id=?", (bounty_id,))
    _remove_files(sids, counts.pop("files"))
    return counts


def delete_campaign(conn: sqlite3.Connection, campaign_id: int) -> dict:
    """Delete a campaign with all its bounties, sources, clips, posts, views, jobs and stored
    files. Your own footage files are never touched; only ClipForge's copies and renders are.
    API cost rows stay: that money was spent. Refuses while a job for it is running."""
    sids = [r[0] for r in conn.execute("SELECT id FROM sources WHERE campaign_id=?",
                                       (campaign_id,))]
    doc_ids = [r[0] for r in conn.execute("SELECT id FROM campaign_docs WHERE campaign_id=?",
                                          (campaign_id,))]
    row = conn.execute("SELECT logo_path FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    with tx(conn):
        counts = _purge(conn, sids, doc_ids)
        conn.execute("DELETE FROM bounties WHERE campaign_id=?", (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
    _remove_files(sids, [*counts.pop("files"), *logo_list(row["logo_path"] if row else None)])
    return counts
