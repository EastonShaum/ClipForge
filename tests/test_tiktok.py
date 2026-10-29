"""TikTok inbox drafts (D32): connect once, then one tap uploads a clip as a draft."""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from clipforge import tiktok
from tests.test_bounty_tabs import _clip


@pytest.fixture
def tt(monkeypatch, settings):
    """A fake TikTok API; returns the list of requests it saw."""
    monkeypatch.setenv("CF_TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setenv("CF_TIKTOK_CLIENT_SECRET", "cs")
    from clipforge.config import get_settings
    get_settings.cache_clear()
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        p = req.url.path
        if p == "/v2/oauth/token/":
            return httpx.Response(200, json={
                "open_id": "u1", "scope": "user.info.basic,video.upload", "access_token": "at",
                "expires_in": 86400, "refresh_token": "rt", "refresh_expires_in": 31536000})
        if p == "/v2/post/publish/inbox/video/init/":
            return httpx.Response(200, json={
                "data": {"publish_id": "v_inbox_1", "upload_url": "https://up.tiktok/u"},
                "error": {"code": "ok"}})
        if req.url.host == "up.tiktok":
            return httpx.Response(201)
        if p == "/v2/post/publish/status/fetch/":
            return httpx.Response(200, json={"data": {"status": "SEND_TO_USER_INBOX"},
                                             "error": {"code": "ok"}})
        return httpx.Response(404)

    monkeypatch.setattr(tiktok, "client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    yield seen
    get_settings.cache_clear()


def _connect(client) -> None:
    r = client.get("/auth/tiktok", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["scope"] == ["user.info.basic,video.upload"] and q["client_key"] == ["ck"]
    r = client.get("/auth/tiktok/callback", params={"code": "c1", "state": q["state"][0]})
    assert "TikTok connected" in r.text


def test_callback_rejects_a_wrong_state(tt, client):
    client.get("/auth/tiktok", follow_redirects=False)
    r = client.get("/auth/tiktok/callback", params={"code": "c1", "state": "forged"})
    assert "state didn" in r.text and not any(q.url.path == "/v2/oauth/token/" for q in tt)


def test_send_to_inbox_uploads_the_clip_and_tracks_status(tt, client, conn, settings):
    client.post("/campaigns", data={"name": "Man On", "bounty_name": "episode-4",
                                    "rate_usd": "125", "rate_mode": "100000"})
    cid = _clip(conn, 1, 1, "Ep4 hook", "approved")
    path = conn.execute("SELECT path FROM clips WHERE id=?", (cid,)).fetchone()["path"]
    video = settings.clips_dir / path
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"x" * 1000)

    assert "Connect TikTok" in client.get("/campaigns/1/bounties/1/post").text
    _connect(client)
    assert "Send to TikTok inbox" in client.get("/campaigns/1/bounties/1/post").text

    r = client.post(f"/clips/{cid}/tiktok", data={"next": "/campaigns/1/bounties/1/post"})
    assert "Sent." in r.text
    init = next(q for q in tt if q.url.path.endswith("/inbox/video/init/"))
    assert init.headers["authorization"] == "Bearer at"
    assert json.loads(init.content)["source_info"] == {
        "source": "FILE_UPLOAD", "video_size": 1000, "chunk_size": 1000, "total_chunk_count": 1}
    put = next(q for q in tt if q.url.host == "up.tiktok")
    assert put.headers["content-range"] == "bytes 0-999/1000" and len(put.content) == 1000

    r = client.post("/tiktok/v_inbox_1/check", data={"next": "/campaigns/1/bounties/1/post"})
    assert "SEND_TO_USER_INBOX" in r.text


def test_big_files_split_into_valid_chunks():
    assert tiktok.chunks(50 * 2**20) == (50 * 2**20, 1)
    size, n = tiktok.chunks(100 * 2**20)
    assert size == 32 * 2**20 and n == 3 and 100 * 2**20 - size * (n - 1) <= 128 * 2**20
