"""Durable job queue on SQLite.

Workers claim one job at a time with a lease (locked_until). If a worker dies mid-job the lease
expires and another worker re-claims it, so every stage handler must be idempotent.
"""

import sqlite3

from clipforge.db import tx, utc_in, utcnow

LEASE_S = 30 * 60  # long enough for a slow Pi render
BACKOFF_S = [60, 300, 900, 3600, 3 * 3600]


def enqueue(
    conn: sqlite3.Connection,
    kind: str,
    dedupe_key: str,
    *,
    source_id: int | None = None,
    candidate_id: int | None = None,
    affinity_host: str | None = None,
    priority: int = 100,
) -> int | None:
    """Insert a job unless one with the same dedupe_key exists. Returns the new id or None."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO jobs (kind, dedupe_key, source_id, candidate_id, affinity_host,
                                       priority)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (kind, dedupe_key, source_id, candidate_id, affinity_host, priority),
    )
    return cur.lastrowid if cur.rowcount else None


def claim(
    conn: sqlite3.Connection,
    worker: str,
    host: str,
    kinds: list[str],
    *,
    pinned_only: list[str] | None = None,
) -> sqlite3.Row | None:
    """Atomically claim the next runnable job of the given kinds for this host.

    Kinds listed in pinned_only are only claimed when pinned to this host. The Pi uses this to
    leave unpinned fetch jobs to the laptop while the laptop is online.
    """
    if not kinds:
        return None
    now = utcnow()
    marks = ",".join("?" * len(kinds))
    pinned_only = pinned_only or []
    pin_clause = ""
    if pinned_only:
        pin_clause = (f"AND NOT (kind IN ({','.join('?' * len(pinned_only))})"
                      " AND affinity_host IS NULL)")
    with tx(conn):
        row = conn.execute(
            f"""SELECT id FROM jobs
                WHERE kind IN ({marks})
                  AND (affinity_host IS NULL OR affinity_host = ?)
                  AND run_after <= ?
                  AND (state = 'queued' OR (state = 'running' AND locked_until < ?))
                  {pin_clause}
                ORDER BY priority, id LIMIT 1""",
            (*kinds, host, now, now, *pinned_only),
        ).fetchone()
        if not row:
            return None
        return conn.execute(
            """UPDATE jobs SET state='running', locked_by=?, locked_until=?,
                      attempts=attempts+1, updated_at=?
               WHERE id=? RETURNING *""",
            (worker, utc_in(LEASE_S), now, row["id"]),
        ).fetchone()


def complete(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET state='done', locked_by=NULL, locked_until=NULL, last_error=NULL,"
        " updated_at=? WHERE id=?",
        (utcnow(), job_id),
    )


def fail(conn: sqlite3.Connection, job_id: int, error: str, *, retry: bool = True) -> str:
    """Record a failure. Retries with backoff until max_attempts, then marks the job dead."""
    job = conn.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
    dead = not retry or job["attempts"] >= job["max_attempts"]
    state = "dead" if dead else "queued"
    delay = BACKOFF_S[min(job["attempts"] - 1, len(BACKOFF_S) - 1)]
    conn.execute(
        "UPDATE jobs SET state=?, last_error=?, locked_by=NULL, locked_until=NULL, run_after=?,"
        " updated_at=? WHERE id=?",
        (state, error[:2000], utc_in(delay), utcnow(), job_id),
    )
    return state


def defer(conn: sqlite3.Connection, job_id: int, seconds: float, reason: str) -> None:
    """Put a job back without counting it as a failed attempt (budget pause, disk full...)."""
    conn.execute(
        "UPDATE jobs SET state='queued', attempts=MAX(attempts-1,0), last_error=?, locked_by=NULL,"
        " locked_until=NULL, run_after=?, updated_at=? WHERE id=?",
        (reason, utc_in(seconds), utcnow(), job_id),
    )


def heartbeat(conn: sqlite3.Connection, name: str, kinds: list[str]) -> None:
    conn.execute(
        "INSERT INTO workers (name, kinds, last_seen) VALUES (?, ?, ?)"
        " ON CONFLICT(name) DO UPDATE SET kinds=excluded.kinds, last_seen=excluded.last_seen",
        (name, ",".join(kinds), utcnow()),
    )


def remote_worker_alive(conn: sqlite3.Connection, local_host: str, grace_s: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM workers WHERE name NOT LIKE ? AND last_seen >= ? LIMIT 1",
        (f"{local_host}%", utc_in(-grace_s)),
    ).fetchone()
    return row is not None


def counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT kind, state, COUNT(*) n FROM jobs GROUP BY kind, state"):
        out.setdefault(r["kind"], {})[r["state"]] = r["n"]
    return out
