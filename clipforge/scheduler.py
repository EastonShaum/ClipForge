"""Periodic housekeeping: poll campaign feeds, check watched streamers for live streams, delete
old raw media, end expired bounties, release held renders, send reminders."""

import logging
import signal
import time

from clipforge import channels, costs, live, notify, reminders
from clipforge.bounties import release_ready
from clipforge.config import get_settings
from clipforge.db import session, tx, utc_in, utcnow
from clipforge.ingest import poll_feeds
from clipforge.stages.fetch import cleanup_raw

log = logging.getLogger(__name__)


def cleanup_once() -> int:
    """Delete raw media for sources rendered (or failed) longer ago than the retention window."""
    s = get_settings()
    with session() as conn:
        rows = conn.execute(
            "SELECT id FROM sources WHERE media_host=? AND raw_deleted_at IS NULL"
            " AND status IN ('rendered','failed') AND updated_at < ?",
            (s.host_name, utc_in(-s.raw_retention_hours * 3600)),
        ).fetchall()
        ids = [r["id"] for r in rows]
        freed = cleanup_raw(s, ids)
        if ids:
            marks = ",".join("?" * len(ids))
            conn.execute(f"UPDATE sources SET raw_deleted_at=? WHERE id IN ({marks})",
                         (utcnow(), *ids))
            log.info("cleaned %d sources, freed %.2f GB", len(ids), freed / 1e9)
        conn.execute("UPDATE bounties SET status='ended' WHERE status='active' AND end_date"
                     " IS NOT NULL AND end_date < date('now')")
        conn.execute("DELETE FROM workers WHERE last_seen < ?", (utc_in(-7 * 86400),))
    return freed


def budget_alert() -> None:
    with session() as conn:
        b = costs.budget_status(conn)
        key = f"budget_alert:{b['state']}:{utcnow()[:7]}"
        if b["state"] == "ok" or conn.execute(
                "SELECT 1 FROM settings_kv WHERE key=?", (key,)).fetchone():
            return
        conn.execute("INSERT INTO settings_kv (key, value) VALUES (?, ?)", (key, utcnow()))
    verb = "PAUSED paid API calls" if b["state"] == "paused" else "is at 80%+"
    notify.discord(f"⚠️ ClipForge budget {verb}: ${b['month']:.2f} of ${b['cap']:.2f} this month.")


def run(poll_every_s: int = 1800, cleanup_every_s: int = 3600):
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    last_poll = last_clean = last_remind = last_live = 0.0
    while not stop:
        now = time.monotonic()
        try:
            if now - last_poll >= poll_every_s:
                with session() as conn:
                    n = poll_feeds(conn)
                    for msg in channels.poll(conn):  # D41
                        notify.discord(msg)
                if n:
                    log.info("feed poll added %d sources", n)
                last_poll = now
            if now - last_live >= get_settings().live_check_s:
                last_live = now
                with session() as conn:
                    for msg in live.check(conn):
                        notify.discord(msg)
            if now - last_remind >= 60:
                with session() as conn:
                    with tx(conn):
                        if n := release_ready(conn):
                            log.info("released %d held renders", n)
                    for msg in reminders.tick(conn):
                        log.info("reminder sent: %s", msg.splitlines()[0])
                last_remind = now
            if now - last_clean >= cleanup_every_s:
                cleanup_once()
                budget_alert()
                last_clean = now
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("scheduler tick failed")
        time.sleep(15)
