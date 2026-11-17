"""The job queue, kept in PostgreSQL itself: one fewer service to run than Redis and Celery.

Workers take jobs with `FOR UPDATE SKIP LOCKED`, so two workers never take the same job and
neither waits for the other. A job whose worker died (locked too long) goes back to the queue.
Failed jobs are retried with a growing delay, then left as `failed` with the error."""

from __future__ import annotations

from psycopg.types.json import Jsonb

MAX_ATTEMPTS = 3
STALE_MINUTES = 120  # a running job older than this is assumed to have lost its worker


def enqueue(conn, kind: str, payload: dict, org_id: int | None = None,
            dedupe_key: str | None = None) -> int | None:  # fmt: skip
    """Adds a job. With `dedupe_key`, a second queued job with the same key is not added
    (two quick pushes to the same branch need one scan of the newest commit)."""
    row = conn.execute(
        "INSERT INTO jobs (org_id, kind, payload, dedupe_key) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (dedupe_key) WHERE status = 'queued' AND dedupe_key IS NOT NULL"
        " DO UPDATE SET payload = EXCLUDED.payload RETURNING id",
        (org_id, kind, Jsonb(payload), dedupe_key),
    ).fetchone()
    return row["id"] if row else None


def claim(conn, worker: str) -> dict | None:
    with conn.transaction():
        conn.execute(
            "UPDATE jobs SET status = 'queued', locked_by = NULL, locked_at = NULL"
            " WHERE status = 'running'"
            " AND locked_at < now() - make_interval(mins => %s)",
            (STALE_MINUTES,),
        )
        return conn.execute(
            "UPDATE jobs SET status = 'running', locked_by = %s, locked_at = now(),"
            " attempts = attempts + 1 WHERE id = ("
            "  SELECT id FROM jobs WHERE status = 'queued' AND run_after <= now()"
            "  ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
            ") RETURNING *",
            (worker,),
        ).fetchone()


def finish(conn, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'done', finished_at = now(), error = NULL WHERE id = %s",
        (job_id,),
    )


def fail(conn, job: dict, error: str, retry: bool = True) -> str:
    """Puts the job back with a delay, or marks it failed after MAX_ATTEMPTS."""
    if retry and job.get("dedupe_key"):
        newer = conn.execute(
            "SELECT id FROM jobs WHERE dedupe_key = %s AND status = 'queued' AND id <> %s",
            (job["dedupe_key"], job["id"]),
        ).fetchone()
        if newer:  # the same work is queued again with newer input: that one will do it
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
