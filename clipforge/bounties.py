"""Bounties: the paid units inside a campaign (Man On → episode-4, episode-5…). Each has its own
rate, budget threshold, end date, requirements and footage folder; sources belong to one."""

import sqlite3

from pydantic import BaseModel, Field

from clipforge.campaigns import effective_per_1k, folder_name


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
    cur = conn.execute(f"INSERT INTO bounties ({','.join(data)}) VALUES"
                       f" ({','.join('?' * len(data))})", tuple(data.values()))
    return cur.lastrowid


def get_bounty(conn: sqlite3.Connection, bounty_id: int) -> dict | None:
    r = conn.execute(
        "SELECT b.*, c.name campaign, c.required_tags campaign_tags FROM bounties b"
        " JOIN campaigns c ON c.id=b.campaign_id WHERE b.id=?", (bounty_id,)).fetchone()
    return _with_rate(r) if r else None


def list_bounties(conn: sqlite3.Connection, campaign_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT b.*, c.name campaign,
                  (SELECT COUNT(*) FROM sources s WHERE s.bounty_id=b.id) sources,
                  (SELECT COUNT(*) FROM clips k JOIN candidates ca ON ca.id=k.candidate_id
                   JOIN sources s ON s.id=ca.source_id WHERE s.bounty_id=b.id) clips
           FROM bounties b JOIN campaigns c ON c.id=b.campaign_id WHERE b.campaign_id=?
           ORDER BY b.status='ended', b.id""", (campaign_id,)).fetchall()
    return [_with_rate(r) for r in rows]


def default_bounty(conn: sqlite3.Connection, campaign_id: int) -> int:
    """Where footage goes when no bounty is named: the campaign's only (or first active) one."""
    r = conn.execute("SELECT id FROM bounties WHERE campaign_id=? ORDER BY status!='active', id"
                     " LIMIT 1", (campaign_id,)).fetchone()
    if not r:
        raise ValueError("This campaign has no bounties yet. Add one first.")
    return r["id"]


def label(campaign: str, bounty: str | None) -> str:
    return f"{campaign} · {bounty}" if bounty and bounty != "main" else campaign


def _with_rate(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["per_1k"] = effective_per_1k(d["rate_usd"], d["rate_per_views"])
    return d
