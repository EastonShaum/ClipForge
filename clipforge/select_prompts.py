"""Prompt versioning: every prompt file in clipforge/prompts/select-*.md becomes an immutable
version named <stem>-<sha6>. Editing a file creates a new version; old ones stay for analytics."""

import hashlib
import sqlite3

from clipforge.stages.select import PROMPTS


def sync_prompts(conn: sqlite3.Connection) -> list[str]:
    present = []
    for f in sorted(PROMPTS.glob("select-*.md")):
        body = f.read_text()
        sha = hashlib.sha256(body.encode()).hexdigest()
        name = f"{f.stem}-{sha[:6]}"
        present.append(name)
        conn.execute(
            "INSERT INTO prompt_versions (name, body_sha, body) VALUES (?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET active=1",
            (name, sha, body),
        )
    if present:
        marks = ",".join("?" * len(present))
        conn.execute(f"UPDATE prompt_versions SET active=0 WHERE name NOT IN ({marks})", present)
    return present


def active_versions(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, body, weight FROM prompt_versions WHERE active=1 ORDER BY id")]
