"""Source ingest: manual links plus optional YouTube RSS polling for campaign-supplied channels."""

import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

import httpx

from clipforge import jobqueue
from clipforge.bounties import default_bounty
from clipforge.config import get_settings
from clipforge.stages.fetch import is_windows_path, local_path

YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def source_key(url: str, host: str = "") -> str:
    """Stable identity for a source so the same video pasted twice is only processed once.
    Local files are identified by machine + path."""
    if (path := local_path(url)) is not None:
        norm = path.replace("\\", "/").lower() if is_windows_path(path) else path
        return "file:" + hashlib.sha1(f"{host}|{norm}".encode()).hexdigest()[:16]
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


def remote_hosts(conn: sqlite3.Connection) -> list[str]:
    """Hosts of remote workers seen so far, most recent first (worker names: <host>-remote-<pid>)."""
    rows = conn.execute("SELECT name FROM workers WHERE name LIKE '%-remote-%'"
                        " ORDER BY last_seen DESC").fetchall()
    return list(dict.fromkeys(r["name"].rsplit("-", 2)[0] for r in rows))


def file_host(conn: sqlite3.Connection, url: str, host: str | None = None) -> str | None:
    """Which machine a source's fetch must run on. Links: any (None). Local files: the machine
    that has the file: given explicitly, else a Windows path means the laptop and a POSIX path
    means the Pi."""
    path = local_path(url)
    if path is None:
        return host or None
    if host:
        return host
    if not is_windows_path(path):
        return get_settings().host_name
    hosts = remote_hosts(conn)
    if not hosts:
        raise ValueError("That's a Windows path, but no laptop worker has connected yet. "
                         "Start the laptop worker first.")
    return hosts[0]


def add_source(conn: sqlite3.Connection, campaign_id: int, url: str,
               host: str | None = None, bounty_id: int | None = None,
               clips: int | None = None) -> tuple[int, bool]:
    """Add a source and queue its fetch job. Returns (source_id, created).
    host pins the fetch to one machine; local files are pinned automatically (see file_host).
    Without a bounty_id it goes to the campaign's default bounty. clips is how many clips to
    keep from it (default: the bounty's setting)."""
    if not conn.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
        raise ValueError(f"campaign {campaign_id} not found")
    if bounty_id is None:
        bounty_id = default_bounty(conn, campaign_id)
    elif not conn.execute("SELECT 1 FROM bounties WHERE id=? AND campaign_id=?",
                          (bounty_id, campaign_id)).fetchone():
        raise ValueError(f"bounty {bounty_id} isn't part of campaign {campaign_id}")
    url = url.strip().strip('"')
    from clipforge.channels import channel_base

    if channel_base(url):
        raise ValueError("That's a whole channel, not one video: add it under YouTube channels"
                         " to pick its videos")
    pin = file_host(conn, url, host)
    key = source_key(url, pin or "")
    existing = conn.execute("SELECT id FROM sources WHERE source_key=?", (key,)).fetchone()
    if existing:
        return existing["id"], False
    sid = conn.execute(
        "INSERT INTO sources (campaign_id, bounty_id, url, source_key, clips_wanted)"
        " VALUES (?, ?, ?, ?, ?)", (campaign_id, bounty_id, url, key, clips),
    ).lastrowid
    jobqueue.enqueue(conn, "fetch", f"fetch:{sid}", source_id=sid, affinity_host=pin,
                     priority=50)
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
