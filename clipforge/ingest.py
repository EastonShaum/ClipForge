"""Source ingest: manual links plus optional YouTube RSS polling for campaign-supplied channels."""

import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

import httpx

from clipforge import jobqueue

YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def source_key(url: str) -> str:
    """Stable identity for a source so the same video pasted twice is only processed once."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    vid = None
    if host == "youtu.be":
        vid = u.path.lstrip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        if u.path == "/watch":
            vid = parse_qs(u.query).get("v", [None])[0]
        else:
            m = re.match(r"^/(shorts|live|embed)/([^/?]+)", u.path)
            vid = m.group(2) if m else None
    if vid and YT_ID.match(vid):
        return f"youtube:{vid}"
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", u.path) if "drive.google.com" in host else None
    if m:
        return f"gdrive:{m.group(1)}"
    norm = f"{host}{u.path}".rstrip("/") + (f"?{u.query}" if u.query else "")
    return "url:" + hashlib.sha1(norm.encode()).hexdigest()[:16]


def add_source(conn: sqlite3.Connection, campaign_id: int, url: str) -> tuple[int, bool]:
    """Add a source and queue its fetch job. Returns (source_id, created)."""
    if not conn.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
        raise ValueError(f"campaign {campaign_id} not found")
    key = source_key(url)
    existing = conn.execute("SELECT id FROM sources WHERE source_key=?", (key,)).fetchone()
    if existing:
        return existing["id"], False
    sid = conn.execute(
        "INSERT INTO sources (campaign_id, url, source_key) VALUES (?, ?, ?)",
        (campaign_id, url.strip(), key),
    ).lastrowid
    jobqueue.enqueue(conn, "fetch", f"fetch:{sid}", source_id=sid, priority=50)
    return sid, True


ATOM = {"a": "http://www.w3.org/2005/Atom"}


def poll_feeds(conn: sqlite3.Connection, client: httpx.Client | None = None) -> int:
    """Poll each active campaign's YouTube RSS feeds; add unseen videos. Returns count added."""
    client = client or httpx.Client(timeout=20, follow_redirects=True)
    added = 0
    rows = conn.execute(
        "SELECT id, watch_feeds FROM campaigns WHERE status='active' AND watch_feeds != ''"
    ).fetchall()
    for row in rows:
        for feed in filter(None, (f.strip() for f in row["watch_feeds"].split())):
            try:
                resp = client.get(feed)
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
            except (httpx.HTTPError, ET.ParseError):
                continue
            for entry in root.findall("a:entry", ATOM):
                link = entry.find("a:link", ATOM)
                if link is not None and link.get("href"):
                    _, created = add_source(conn, row["id"], link.get("href"))
                    added += created
    return added
