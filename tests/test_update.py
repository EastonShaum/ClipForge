"""Laptop self-update: the Pi serves its running commit; outdated workers are turned away."""

import io
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from clipforge import config, update
from clipforge.web.app import create_app
from clipforge.worker import WORKER_PROTOCOL, Outdated, RemoteClient

H = {"Authorization": "Bearer test-token"}


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_keeps_secrets_and_data_and_stays_inside(tmp_path):
    dest = tmp_path / "repo"
    (dest / "data").mkdir(parents=True)
    (dest / ".env").write_text("SECRET=1")
    (dest / "data" / "x.db").write_text("db")
    n = update.extract(_zip({
        "CLipping/clipforge/new.py": b"print(1)",
        "CLipping/.env": b"SECRET=overwritten",
        "CLipping/data/x.db": b"overwritten",
        "CLipping/../evil.py": b"bad",
        "CLipping/scripts/laptop/start-worker.bat": b"@echo off\r\n",
    }), dest)
    assert n == 2
    assert (dest / "clipforge" / "new.py").read_bytes() == b"print(1)"
    assert (dest / ".env").read_text() == "SECRET=1"
    assert (dest / "data" / "x.db").read_text() == "db"
    assert not (tmp_path / "evil.py").exists()


@pytest.fixture
def strict(monkeypatch, conn):
    monkeypatch.setenv("CF_REQUIRE_WORKER_VERSION", "true")
    config.get_settings.cache_clear()
    yield TestClient(create_app())
    config.get_settings.cache_clear()


def test_pi_serves_its_commit_and_refuses_other_versions(strict, conn):
    version = update.repo_version()
    assert version, "tests run from the git checkout"
    assert strict.get("/api/worker/code/version").status_code == 401
    assert strict.get("/api/worker/code/version", headers=H).json() == {"version": version}
    r = strict.get("/api/worker/code", headers=H)
    assert r.headers["x-clipforge-version"] == version
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "CLipping/clipforge/worker.py" in names and "CLipping/.env" not in names

    body = {"worker": "lap-remote-1", "host": "lap", "kinds": ["fetch"],
            "protocol": WORKER_PROTOCOL}
    assert strict.post("/api/worker/claim", headers=H,
                       json={**body, "version": "old"}).status_code == 426
    assert strict.post("/api/worker/claim", headers=H,
                       json={**body, "version": version}).status_code == 204


def test_self_update_installs_then_is_a_noop(strict, tmp_path, monkeypatch):
    version = update.repo_version()
    dest = tmp_path / "laptop"
    dest.mkdir()
    (dest / ".env").write_text("CF_WORKER_TOKEN=test-token")
    monkeypatch.setattr(update, "VERSION_FILE", dest / ".clipforge-version")

    # Route the updater's HTTP calls into the test app.
    monkeypatch.setattr(update.httpx, "Client", lambda *, base_url, timeout, headers: TestClient(
        strict.app, base_url="http://testserver", headers=headers))
    assert update.self_update("http://pi", "test-token", dest) is True
    assert (dest / "clipforge" / "worker.py").is_file()
    assert (dest / ".clipforge-version").read_text() == version
    assert (dest / ".env").read_text() == "CF_WORKER_TOKEN=test-token"
    assert update.self_update("http://pi", "test-token", dest) is False


def test_self_update_tolerates_unreachable_pi(tmp_path):
    assert update.self_update("http://127.0.0.1:9", "t", tmp_path) is False


def test_remote_client_raises_outdated_on_426():
    c = RemoteClient("http://pi", "t")
    c.http = httpx.Client(base_url="http://pi", transport=httpx.MockTransport(
        lambda req: httpx.Response(426, text="outdated")))
    with pytest.raises(Outdated):
        c.claim("w", "h", ["fetch"])
