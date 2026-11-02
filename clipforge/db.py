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
                 ("sources", "clips_wanted", "INTEGER"),
                 ("campaigns", "live_channels", "TEXT NOT NULL DEFAULT ''"),
                 ("campaigns", "watch_live", "INTEGER NOT NULL DEFAULT 0"),
                 ("bounties", "live_clip", "INTEGER NOT NULL DEFAULT 0"),
                 ("bounties", "render_hold", "INTEGER NOT NULL DEFAULT 0"),
                 ("sources", "skip_ms", "INTEGER NOT NULL DEFAULT 0"),
                 ("live_sessions", "guests", "TEXT NOT NULL DEFAULT ''"),
                 ("live_sessions", "blocked", "TEXT"),
                 ("bounties", "live_session_id", "INTEGER"),
                 ("campaigns", "live_layout", "TEXT"),
                 ("live_sessions", "stopped_at", "TEXT"),
                 ("live_sessions", "delete_asked_at", "TEXT"),
                 ("clips", "file_deleted_at", "TEXT"),
                 # D38: Content Rewards. The marketplace is the campaign's; the money terms are
                 # the bounty's (a Content Rewards "campaign" is a ClipForge bounty).
                 ("campaigns", "marketplace", "TEXT NOT NULL DEFAULT 'content_rewards'"),
                 ("campaigns", "payer_type", "TEXT"),
                 ("bounties", "campaign_type", "TEXT NOT NULL DEFAULT 'cpm'"),
                 ("bounties", "min_payout_usd", "REAL"),
                 ("bounties", "flat_fee_bonus_usd", "REAL"),
                 ("bounties", "budget_total_usd", "REAL"),
                 ("bounties", "budget_remaining_usd", "REAL"),
                 ("bounties", "budget_checked_at", "TEXT"),
                 ("bounties", "audience_rules", "TEXT NOT NULL DEFAULT ''"),
                 ("bounties", "footage_scope", "TEXT NOT NULL DEFAULT ''"),
                 ("bounties", "ai_video_banned", "INTEGER NOT NULL DEFAULT 0"),
                 ("bounties", "onscreen_ad_label_required", "INTEGER NOT NULL DEFAULT 0"),
                 ("bounties", "brief_url", "TEXT NOT NULL DEFAULT ''"),
                 ("bounties", "brief_text", "TEXT NOT NULL DEFAULT ''"),
                 ("bounties", "pays_on", "TEXT NOT NULL DEFAULT 'unknown'"),
                 ("bounties", "layout", "TEXT"),
                 ("bounties", "facecam", "TEXT"),
                 ("campaigns", "facecam", "TEXT"),
                 ("posts", "account_id", "INTEGER"),
                 ("posts", "review_status", "TEXT"),
                 ("posts", "reject_reason", "TEXT"),
                 ("posts", "approved_views", "INTEGER"),
                 ("posts", "earned_usd", "REAL"),
                 ("posts", "approved_at", "TEXT"),
                 ("posts", "paid_at", "TEXT"),
                 ("view_snapshots", "engaged_views", "INTEGER"),
                 ("scout_bounties", "marketplace", "TEXT NOT NULL DEFAULT ''"),
                 ("scout_bounties", "campaign_type", "TEXT NOT NULL DEFAULT ''"),
                 ("scout_bounties", "min_payout_usd", "REAL"),
                 ("scout_bounties", "max_payout_usd", "REAL"),
                 ("scout_bounties", "bonus_usd", "REAL"),
                 ("scout_bounties", "budget_total_usd", "REAL"),
                 ("scout_bounties", "budget_remaining_usd", "REAL"),
                 ("scout_bounties", "end_date", "TEXT"),
                 ("scout_bounties", "platforms", "TEXT NOT NULL DEFAULT ''"),
                 ("scout_bounties", "footage_scope", "TEXT NOT NULL DEFAULT ''")]

# One-time (D38): campaigns made before the marketplace field keep their marketplace (all of
# them were Clipping.net); posts already submitted wait for the brand's review.
MARKETPLACE_MIGRATION = """
UPDATE campaigns SET marketplace = CASE
    WHEN lower(platform) LIKE 'clipping%' THEN 'clipping_net'
    WHEN lower(replace(platform, ' ', '')) IN ('contentrewards', 'content_rewards', 'whop')
        THEN 'content_rewards'
    ELSE 'other' END;
UPDATE posts SET review_status = 'pending' WHERE submitted_at IS NOT NULL AND review_status IS NULL;
INSERT INTO settings_kv (key, value)
    VALUES ('migrated:marketplace', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
"""

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
        if not conn.execute("SELECT 1 FROM settings_kv WHERE key='migrated:marketplace'"
                            ).fetchone():
            script = MARKETPLACE_MIGRATION
            if "platform" not in {r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")}:
                # Older than the platform column: every campaign then was Clipping.net.
                script = script.replace(script[script.index("UPDATE campaigns"):
                                               script.index("END;") + 4],
                                        "UPDATE campaigns SET marketplace = 'clipping_net';")
            conn.executescript("BEGIN;" + script + "COMMIT;")
