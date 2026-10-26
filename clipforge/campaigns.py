"""Campaign registry: add/list campaigns, compute effective pay, enforce the blocklist."""

import hashlib
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from clipforge.config import get_settings


class CampaignIn(BaseModel):
    name: str
    platform: str = "clipping.net"
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


def effective_per_1k(rate_usd: float, rate_per_views: int) -> float:
    return rate_usd * 1000 / rate_per_views if rate_per_views else 0.0


def blocked_reason(c: CampaignIn) -> str | None:
    s = get_settings()
    hay = " ".join([c.name, c.category, c.notes, c.source_links]).lower()
    for cat in s.blocked_categories:
        if cat.lower() in c.category.lower():
            return f"blocked category: {cat}"
    for kw in s.blocked_keywords:
        # Word boundaries so "stake" doesn't block "mistake" or "stakeholder".
        if re.search(rf"\b{re.escape(kw.lower())}\b", hay):
            return f"blocked keyword: {kw}"
    return None


def add_campaign(conn: sqlite3.Connection, c: CampaignIn, *, force: bool = False) -> int:
    reason = blocked_reason(c)
    if reason and not force:
        raise ValueError(f"Campaign rejected by blocklist ({reason}). Use force to override.")
    data = c.model_dump()
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


def set_logo(conn: sqlite3.Connection, campaign_id: int, data: bytes) -> str:
    """Store a campaign's required logo (any image ffmpeg reads) as a shrunk PNG with alpha.
    The file name carries a content hash, so a replaced logo never hits a stale laptop cache."""
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
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-frames:v", "1",
                            "-vf", f"scale='min({LOGO_STORE_W},iw)':-1,format=rgba", str(part)],
                           capture_output=True, text=True)
        if r.returncode:
            raise ValueError(f"not a readable image: {r.stderr.strip()[:200]}")
        part.replace(dest)
    conn.execute("UPDATE campaigns SET logo_path=? WHERE id=?", (rel, campaign_id))
    return rel


def clear_logo(conn: sqlite3.Connection, campaign_id: int) -> None:
    conn.execute("UPDATE campaigns SET logo_path=NULL WHERE id=?", (campaign_id,))


def estimate_payout(campaign: dict, views: int) -> float:
    """Payout for one clip: zero below threshold, linear above, clipped at the cap."""
    if views < (campaign.get("min_views") or 0):
        return 0.0
    pay = views / 1000 * campaign["per_1k"]
    cap = campaign.get("cap_usd")
    return min(pay, cap) if cap else pay
