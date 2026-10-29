"""TikTok inbox uploads (D32). ClipForge only ever sends a draft to Easton's TikTok inbox; he
opens it in the app, adds the on-screen text, sets the disclosure toggle and posts it himself.

Official Content Posting API, `video.upload` scope only (no Direct Post). Tokens live in
settings_kv under 'tiktok:auth': the DB is gitignored, and the client key/secret stay in .env.
"""

import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from clipforge.config import Settings
from clipforge.db import utcnow

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com"
SCOPES = "user.info.basic,video.upload"
CHUNK = 32 * 1024 * 1024  # TikTok: chunks 5-64 MB, files up to 64 MB may go as one chunk
ONE_CHUNK_MAX = 64 * 1024 * 1024
REFRESH_MARGIN_S = 600


class TikTokError(Exception):
    pass


def client() -> httpx.Client:  # tests swap this for a mock transport
    return httpx.Client(timeout=60)


def configured(s: Settings) -> bool:
    return bool(s.tiktok_client_key and s.tiktok_client_secret)


def _kv(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings_kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_kv(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings_kv (key, value) VALUES (?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def authorize_url(conn, s: Settings) -> str:
    state = secrets.token_urlsafe(24)
    _set_kv(conn, "tiktok:state", state)
    return AUTHORIZE_URL + "?" + urlencode({
        "client_key": s.tiktok_client_key, "response_type": "code", "scope": SCOPES,
        "redirect_uri": s.tiktok_redirect_uri, "state": state})


def _store_token(conn, data: dict) -> dict:
    if "access_token" not in data:
        raise TikTokError(data.get("error_description") or data.get("error") or str(data))
    now = time.time()
    auth = {"open_id": data.get("open_id"), "scope": data.get("scope", ""),
            "access_token": data["access_token"], "refresh_token": data["refresh_token"],
            "expires_at": now + int(data.get("expires_in", 86400)),
            "refresh_expires_at": now + int(data.get("refresh_expires_in", 31536000)),
            "connected_at": utcnow()}
    _set_kv(conn, "tiktok:auth", json.dumps(auth))
    return auth


def exchange_code(conn, s: Settings, code: str, state: str, http: httpx.Client) -> dict:
    expected = _kv(conn, "tiktok:state")
    if not expected or not secrets.compare_digest(state, expected):
        raise TikTokError("login state didn't match; start the connect step again")
    conn.execute("DELETE FROM settings_kv WHERE key='tiktok:state'")
    r = http.post(f"{API}/v2/oauth/token/", data={
        "client_key": s.tiktok_client_key, "client_secret": s.tiktok_client_secret,
        "code": code, "grant_type": "authorization_code", "redirect_uri": s.tiktok_redirect_uri})
    return _store_token(conn, r.json())


def auth(conn) -> dict | None:
    raw = _kv(conn, "tiktok:auth")
    return json.loads(raw) if raw else None


def disconnect(conn) -> None:
    conn.execute("DELETE FROM settings_kv WHERE key='tiktok:auth'")


def access_token(conn, s: Settings, http: httpx.Client) -> str:
    a = auth(conn)
    if not a:
        raise TikTokError("TikTok isn't connected")
    if a["expires_at"] - time.time() > REFRESH_MARGIN_S:
        return a["access_token"]
    if a["refresh_expires_at"] <= time.time():
        raise TikTokError("the TikTok login expired; connect again")
    r = http.post(f"{API}/v2/oauth/token/", data={
        "client_key": s.tiktok_client_key, "client_secret": s.tiktok_client_secret,
        "grant_type": "refresh_token", "refresh_token": a["refresh_token"]})
    return _store_token(conn, r.json())["access_token"]


def _check(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        raise TikTokError(f"HTTP {r.status_code}: {r.text[:300]}") from None
    err = body.get("error") or {}
    if r.status_code >= 400 or err.get("code") not in (None, "ok"):
        raise TikTokError(f"{err.get('code')}: {err.get('message') or r.text[:300]}")
    return body.get("data") or {}


def chunks(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok's rules; the last chunk takes the rest."""
    if size <= ONE_CHUNK_MAX:
        return size, 1
    return CHUNK, size // CHUNK


def send_to_inbox(conn, s: Settings, clip_id: int, video: Path, http: httpx.Client) -> str:
    """Upload one clip as a draft to the connected account's inbox; returns the publish_id."""
    token = access_token(conn, s, http)
    size = video.stat().st_size
    chunk_size, count = chunks(size)
    data = _check(http.post(
        f"{API}/v2/post/publish/inbox/video/init/",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8"},
        json={"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                              "chunk_size": chunk_size, "total_chunk_count": count}}))
    publish_id, upload_url = data["publish_id"], data["upload_url"]
    conn.execute("INSERT INTO tiktok_uploads (clip_id, publish_id, status) VALUES (?,?,?)",
                 (clip_id, publish_id, "UPLOADING"))
    try:
        with video.open("rb") as fh:
            for i in range(count):
                first = i * chunk_size
                last = size - 1 if i == count - 1 else first + chunk_size - 1
                body = fh.read(last - first + 1)
                r = http.put(upload_url, content=body, timeout=300, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {first}-{last}/{size}"})
                if r.status_code not in (200, 201, 206):
                    raise TikTokError(f"upload chunk {i + 1}/{count}: HTTP {r.status_code} "
                                      f"{r.text[:200]}")
    except (OSError, httpx.HTTPError, TikTokError) as e:
        _record(conn, publish_id, "FAILED", str(e))
        raise TikTokError(str(e)) from e
    _record(conn, publish_id, "PROCESSING_UPLOAD")
    return publish_id


def _record(conn, publish_id: str, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE tiktok_uploads SET status=?, error=?, updated_at=? WHERE publish_id=?",
                 (status, error, utcnow(), publish_id))


def latest_uploads(conn, clip_ids: list[int]) -> dict[int, dict]:
    """Each clip's most recent inbox upload."""
    out: dict[int, dict] = {}
    for r in conn.execute(
            f"SELECT * FROM tiktok_uploads WHERE clip_id IN ({','.join('?' * len(clip_ids))})"
            " ORDER BY id", clip_ids):
        out[r["clip_id"]] = dict(r)
    return out


def refresh_status(conn, s: Settings, publish_id: str, http: httpx.Client) -> str:
    """Ask TikTok how an upload is going. SEND_TO_USER_INBOX means the draft is waiting."""
    data = _check(http.post(
        f"{API}/v2/post/publish/status/fetch/",
        headers={"Authorization": f"Bearer {access_token(conn, s, http)}",
                 "Content-Type": "application/json; charset=UTF-8"},
        json={"publish_id": publish_id}))
    status = data.get("status", "UNKNOWN")
    _record(conn, publish_id, status, data.get("fail_reason") or None)
    return status
