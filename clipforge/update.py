"""Keep the laptop's code identical to the Pi's.

The Pi serves `git archive` of the commit it is running. The laptop's start script runs
`clipforge self-update` before each worker start, and the worker exits with code 3 when the Pi
says it is outdated, so the script loops back and updates. Everything stays on the tailnet.
"""

import io
import logging
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

import httpx

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO / ".clipforge-version"
# Never overwritten by an update: local secrets, local data, the local venv.
KEEP = {".env", "data", ".venv", ".clipforge-version", "Campaigns", "Live"}
UPDATE_EXIT_CODE = 3


def repo_version(repo: Path = REPO) -> str | None:
    """Commit the Pi is running (None outside a git checkout, e.g. on the laptop)."""
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def code_zip(version: str, repo: Path = REPO) -> bytes:
    return subprocess.run(["git", "-C", str(repo), "archive", "--format=zip",
                           "--prefix=CLipping/", version], capture_output=True, check=True).stdout


def local_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return ""


def extract(data: bytes, dest: Path) -> int:
    """Unpack a code zip over dest. Skips KEEP paths and anything escaping dest."""
    n = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            parts = PurePosixPath(info.filename).parts[1:]  # drop the CLipping/ prefix
            if info.is_dir() or not parts or parts[0] in KEEP or ".." in parts:
                continue
            target = dest.joinpath(*parts)
            if not target.resolve().is_relative_to(dest.resolve()):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".new")
            tmp.write_bytes(zf.read(info))
            tmp.replace(target)
            n += 1
    return n


def self_update(base_url: str, token: str, dest: Path = REPO) -> bool:
    """Returns True if new code was installed. Never raises for network trouble: an
    unreachable Pi shouldn't stop the worker from starting (it will wait for the Pi anyway)."""
    try:
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=120,
                          headers={"Authorization": f"Bearer {token}"}) as http:
            head = http.get("/api/worker/code/version")
            head.raise_for_status()
            want = head.json()["version"]
            if want == local_version():
                log.info("code is up to date (%s)", want[:8])
                return False
            r = http.get("/api/worker/code")
            r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("could not check for updates (%s); starting with the current code", e)
        return False
    n = extract(r.content, dest)
    (dest / ".clipforge-version").write_text(want)
    log.info("updated to %s (%d files)", want[:8], n)
    return True
