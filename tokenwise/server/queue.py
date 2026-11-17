"""The job queue, kept in PostgreSQL itself: one fewer service to run than Redis and Celery.

Workers take jobs with `FOR UPDATE SKIP LOCKED`, so two workers never take the same job.
Jobs that share a `dedupe_key` (one repository's scans, one pull request's report, one fix)
never run at the same time: a worker skips such a job while another one is running. A running
job's worker writes a heartbeat; a job without one for STALE_MINUTES lost its worker and goes
back to the queue. Failed jobs are retried with a growing delay, then left as `failed`.

Input order: a job's payload may carry `event_at` (ISO time of the GitHub event). A queued job
keeps the newest input, so a late, older webhook cannot replace a newer one."""

from __future__ import annotations

from psycopg.types.json import Jsonb

MAX_ATTEMPTS = 3
STALE_MINUTES = 10  # no heartbeat for this long: the worker is gone
HEARTBEAT_SECONDS = 60


def enqueue(conn, kind: str, payload: dict, org_id: int | None = None,
            dedupe_key: str | None = None) -> int | None:  # fmt: skip
    """Adds a job. With `dedupe_key`, a second queued job with the same key is not added
    (two quick pushes to the same branch need one scan of the newest commit)."""
    row = conn.execute(
        "INSERT INTO jobs (org_id, kind, payload, dedupe_key) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (dedupe_key) WHERE status = 'queued' AND dedupe_key IS NOT NULL"
        " DO UPDATE SET payload = CASE WHEN COALESCE(EXCLUDED.payload->>'event_at', '')"
        "  >= COALESCE(jobs.payload->>'event_at', '') THEN EXCLUDED.payload"
        "  ELSE jobs.payload END RETURNING id",
        (org_id, kind, Jsonb(payload), dedupe_key),
    ).fetchone()
    return row["id"] if row else None


def claim(conn, worker: str) -> dict | None:
    with conn.transaction():
        # claims are short; one at a time makes the "same key already running" check exact
        conn.execute("SELECT pg_advisory_xact_lock(7262702)")
        stale = conn.execute(
            "SELECT * FROM jobs WHERE status = 'running'"
            " AND locked_at < now() - make_interval(mins => %s) FOR UPDATE",
            (STALE_MINUTES,),
        ).fetchall()
        for job in stale:
            fail(conn, job, f"worker {job['locked_by']} stopped answering")
        return conn.execute(
            "UPDATE jobs SET status = 'running', locked_by = %s, locked_at = now(),"
            " started_at = now(), attempts = attempts + 1 WHERE id = ("
            "  SELECT j.id FROM jobs j WHERE j.status = 'queued' AND j.run_after <= now()"
            "  AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.status = 'running'"
            "                  AND r.dedupe_key = j.dedupe_key)"
            "  ORDER BY j.id FOR UPDATE SKIP LOCKED LIMIT 1"
            ") RETURNING *",
            (worker,),
        ).fetchone()


def heartbeat(conn, job_id: int, worker: str) -> bool:
    """Tells the queue the job is still being worked on; False if it was taken away."""
    done = conn.execute(
        "UPDATE jobs SET locked_at = now() WHERE id = %s AND status = 'running' AND locked_by = %s",
        (job_id, worker),
    )
    return done.rowcount > 0


def finish(conn, job_id: int, worker: str | None = None) -> bool:
    """Marks the job done; with `worker`, only if that worker still holds it."""
    done = conn.execute(
        "UPDATE jobs SET status = 'done', finished_at = now(), error = NULL WHERE id = %s"
        " AND (%s::text IS NULL OR (status = 'running' AND locked_by = %s))",
        (job_id, worker, worker),
    )
    return done.rowcount > 0


def fail(conn, job: dict, error: str, retry: bool = True) -> str:
    """Puts the job back with a delay, or marks it failed after MAX_ATTEMPTS."""
    if retry and job.get("dedupe_key"):
        newer = conn.execute(
            "SELECT id, payload FROM jobs WHERE dedupe_key = %s AND status = 'queued' AND id <> %s",
            (job["dedupe_key"], job["id"]),
        ).fetchone()
        if newer:  # the same work is queued again: that one will do it, with the newest input
            mine = (job.get("payload") or {}).get("event_at") or ""
            if mine > ((newer["payload"] or {}).get("event_at") or ""):
                conn.execute("UPDATE jobs SET payload = %s WHERE id = %s",
                             (Jsonb(job["payload"]), newer["id"]))  # fmt: skip
            conn.execute(
                "UPDATE jobs SET status = 'done', finished_at = now(), error = %s WHERE id = %s",
                (f"superseded by job {newer['id']}: {error}"[:4000], job["id"]),
            )
            return "superseded"
    if retry and job["attempts"] < MAX_ATTEMPTS:
        conn.execute(
            "UPDATE jobs SET status = 'queued', locked_by = NULL, locked_at = NULL, error = %s,"
            " run_after = now() + make_interval(secs => %s) WHERE id = %s",
            (error[:4000], 30 * 2 ** job["attempts"], job["id"]),
        )
        return "queued"
    conn.execute(
        "UPDATE jobs SET status = 'failed', finished_at = now(), error = %s WHERE id = %s",
        (error[:4000], job["id"]),
    )
    return "failed"


def counts(conn) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
