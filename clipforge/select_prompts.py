"""Prompt versioning: every prompt file in clipforge/prompts/select-*.md (ordinary footage) and
live-*.md (live stream chunks, D33) becomes an immutable version named <stem>-<sha6>. Editing a
file creates a new version; old ones stay for analytics."""

import hashlib
import sqlite3

from clipforge.stages.select import PROMPTS


def sync_prompts(conn: sqlite3.Connection) -> list[str]:
    present = []
    for f in sorted([*PROMPTS.glob("select-*.md"), *PROMPTS.glob("live-*.md")]):
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


def active_versions(conn: sqlite3.Connection, live: bool = False) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, body, weight FROM prompt_versions WHERE active=1 AND name LIKE ?"
        " ORDER BY id", ("live-%" if live else "select-%",))]
