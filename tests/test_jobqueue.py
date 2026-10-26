from clipforge import jobqueue
from clipforge.db import utc_in


def test_enqueue_is_idempotent(conn):
    assert jobqueue.enqueue(conn, "fetch", "fetch:1", source_id=1)
    assert jobqueue.enqueue(conn, "fetch", "fetch:1", source_id=1) is None
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_claim_respects_kind_priority_and_affinity(conn):
    jobqueue.enqueue(conn, "render", "r:1", affinity_host="laptop", priority=10)
    jobqueue.enqueue(conn, "fetch", "f:1", priority=50)
    jobqueue.enqueue(conn, "fetch", "f:2", priority=40)
    job = jobqueue.claim(conn, "pi-a", "pi", ["fetch", "render"])
    assert job["dedupe_key"] == "f:2"  # render is pinned to the laptop
    assert job["state"] == "running" and job["attempts"] == 1
    job2 = jobqueue.claim(conn, "laptop-a", "laptop", ["render"])
    assert job2["dedupe_key"] == "r:1"


def test_claimed_job_is_not_claimed_twice(conn):
    jobqueue.enqueue(conn, "fetch", "f:1")
    assert jobqueue.claim(conn, "a", "pi", ["fetch"])
    assert jobqueue.claim(conn, "b", "pi", ["fetch"]) is None


def test_expired_lease_is_reclaimed(conn):
    jid = jobqueue.enqueue(conn, "fetch", "f:1")
    jobqueue.claim(conn, "a", "pi", ["fetch"])
    conn.execute("UPDATE jobs SET locked_until=? WHERE id=?", (utc_in(-5), jid))
    job = jobqueue.claim(conn, "b", "pi", ["fetch"])
    assert job["locked_by"] == "b" and job["attempts"] == 2


def test_pinned_only_leaves_unpinned_jobs_for_remote(conn):
    jobqueue.enqueue(conn, "fetch", "f:1")
    jobqueue.enqueue(conn, "render", "r:1", affinity_host="pi")
    job = jobqueue.claim(conn, "pi-a", "pi", ["fetch", "render"], pinned_only=["fetch", "render"])
    assert job["dedupe_key"] == "r:1"
    assert jobqueue.claim(conn, "pi-a", "pi", ["fetch"], pinned_only=["fetch"]) is None


def test_fail_backs_off_then_dies(conn):
    jid = jobqueue.enqueue(conn, "fetch", "f:1")
    conn.execute("UPDATE jobs SET max_attempts=2 WHERE id=?", (jid,))
    jobqueue.claim(conn, "a", "pi", ["fetch"])
    assert jobqueue.fail(conn, jid, "boom") == "queued"
    assert jobqueue.claim(conn, "a", "pi", ["fetch"]) is None  # backoff: run_after in future
    conn.execute("UPDATE jobs SET run_after=? WHERE id=?", (utc_in(-1), jid))
    jobqueue.claim(conn, "a", "pi", ["fetch"])
    assert jobqueue.fail(conn, jid, "boom again") == "dead"


def test_defer_does_not_burn_attempt(conn):
    jid = jobqueue.enqueue(conn, "select", "s:1")
    jobqueue.claim(conn, "a", "pi", ["select"])
    jobqueue.defer(conn, jid, 3600, "budget")
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["state"] == "queued" and row["attempts"] == 0 and row["last_error"] == "budget"


def test_remote_worker_alive(conn):
    assert not jobqueue.remote_worker_alive(conn, "pi", 600)
    jobqueue.heartbeat(conn, "pi-main-1", ["fetch"])
    assert not jobqueue.remote_worker_alive(conn, "pi", 600)
    jobqueue.heartbeat(conn, "laptop-remote-9", ["render"])
    assert jobqueue.remote_worker_alive(conn, "pi", 600)
