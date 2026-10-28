"""SQLite access. One short-lived connection per unit of work; WAL lets the web UI read while
workers write."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from clipforge.config import get_settings

SCHEMA = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_in(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or get_settings().db_path
    # check_same_thread=False: FastAPI may hand a per-request connection between threads.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE takes the write lock up front, so two workers can't both read-then-write."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS won't add them to an
# existing DB, so add any that are missing. Additive only: never drop or retype.
ADDED_COLUMNS = [("campaigns", "logo_path", "TEXT"), ("posts", "submitted_at", "TEXT"),
                 ("campaigns", "media_folder", "TEXT"), ("sources", "bounty_id", "INTEGER"),
                 ("campaign_docs", "bounty_id", "INTEGER"),
                 ("bounties", "auto_queue", "INTEGER NOT NULL DEFAULT 0"),
                 ("bounties", "clips_wanted", "INTEGER NOT NULL DEFAULT 6"),
                 ("sources", "clips_wanted", "INTEGER")]

# One-time: campaigns made before bounties existed become a campaign with one bounty that
# carries their rate, end date and folder; their sources and brief screenshots move to it.
BOUNTY_MIGRATION = """
INSERT INTO bounties (campaign_id, name, rate_usd, rate_per_views, min_views, cap_usd,
                      end_date, status, media_folder)
SELECT id, 'main', rate_usd, rate_per_views, min_views, cap_usd, end_date,
       CASE status WHEN 'ended' THEN 'ended' ELSE 'active' END, media_folder
FROM campaigns c WHERE NOT EXISTS (SELECT 1 FROM bounties b WHERE b.campaign_id = c.id);
UPDATE sources SET bounty_id = (SELECT MIN(id) FROM bounties b
                                WHERE b.campaign_id = sources.campaign_id)
WHERE bounty_id IS NULL;
UPDATE campaign_docs SET bounty_id = (SELECT MIN(id) FROM bounties b
                                      WHERE b.campaign_id = campaign_docs.campaign_id)
WHERE bounty_id IS NULL;
INSERT INTO settings_kv (key, value) VALUES ('migrated:bounties', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
"""


def init_db(path: Path | None = None) -> None:
    with session(path) as conn:
        conn.executescript(SCHEMA.read_text())
        for table, col, decl in ADDED_COLUMNS:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        if not conn.execute("SELECT 1 FROM settings_kv WHERE key='migrated:bounties'").fetchone():
            conn.executescript("BEGIN;" + BOUNTY_MIGRATION + "COMMIT;")
