"""PostgreSQL schema and the data access the web app and the worker share.

Every row that belongs to a customer hangs off `organizations` with ON DELETE CASCADE, so
removing the GitHub App deletes everything the organization ever stored here in one statement.
A personal GitHub account is an organization with one member."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..analyzers import Finding
from ..health import scores_for, source_of

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS organizations (
    id BIGSERIAL PRIMARY KEY,
    installation_id BIGINT NOT NULL UNIQUE,
    account_login TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    account_type TEXT NOT NULL DEFAULT 'User',
    model_provider TEXT NOT NULL DEFAULT 'anthropic',
    model_base_url TEXT NOT NULL DEFAULT '',
    models TEXT[] NOT NULL DEFAULT '{}',
    model_key BYTEA,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY,  -- GitHub user id
    login TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS members (
    org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'member')),
    PRIMARY KEY (org_id, user_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS repositories (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    github_id BIGINT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    overall INTEGER,
    last_scan_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS scans (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    scan_type TEXT NOT NULL DEFAULT 'push',
    commit_sha TEXT NOT NULL,
    files INTEGER NOT NULL,
    seconds REAL NOT NULL,
    tools TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS findings (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    rule TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    file TEXT NOT NULL,
    line INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    data JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    first_scan BIGINT NOT NULL,
    last_scan BIGINT NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    reopened INTEGER NOT NULL DEFAULT 0,
    UNIQUE (repository_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS findings_open ON findings (repository_id, status);

CREATE TABLE IF NOT EXISTS snapshots (
    scan_id BIGINT PRIMARY KEY REFERENCES scans(id) ON DELETE CASCADE,
    repository_id BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    overall INTEGER NOT NULL,
    scores JSONB NOT NULL,
    open_findings INTEGER NOT NULL,
    new_findings INTEGER NOT NULL,
    resolved_findings INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pull_requests (
    repository_id BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    comment_id BIGINT,
    score INTEGER,
    level TEXT,
    report JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repository_id, number)
);

CREATE TABLE IF NOT EXISTS fixes (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    finding_id BIGINT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
        -- queued -> proposed -> approved -> pushed; or failed / rejected
    requested_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    approved_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    base_sha TEXT,
    changes JSONB,
    summary JSONB,
    branch TEXT,
    pr_url TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS model_usage (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    repository_id BIGINT REFERENCES repositories(id) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT REFERENCES organizations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    dedupe_key TEXT,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued, running, done, failed
    attempts INTEGER NOT NULL DEFAULT 0,
    run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by TEXT,
    locked_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (status, run_after);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe ON jobs (dedupe_key)
    WHERE status = 'queued' AND dedupe_key IS NOT NULL;

-- version 2: idempotent webhooks, event order, heartbeats, one open fix per finding
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,  -- GitHub's X-GitHub-Delivery
    event TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS webhook_deliveries_age ON webhook_deliveries (received_at);
-- GitHub never reuses an installation id: a late "created" for a removed one is ignored
CREATE TABLE IF NOT EXISTS removed_installations (
    installation_id BIGINT PRIMARY KEY,
    removed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE repositories ADD COLUMN IF NOT EXISTS last_event_at TEXT;
ALTER TABLE pull_requests ADD COLUMN IF NOT EXISTS event_at TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS fixes_one_open ON fixes (finding_id)
    WHERE status IN ('queued', 'proposed', 'approved');
"""


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, row_factory=dict_row, autocommit=True)


def migrate(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(7262701)")  # one migrator at a time
        conn.execute(SCHEMA)
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version VALUES (%s)", (SCHEMA_VERSION,))
        elif row["version"] < SCHEMA_VERSION:  # every change above is written to re-run safely
            conn.execute("UPDATE schema_version SET version = %s", (SCHEMA_VERSION,))


@contextmanager
def tx(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    with conn.transaction():
        yield conn


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- organizations and members


def upsert_user(conn, user_id: int, login: str) -> None:
    conn.execute(
        "INSERT INTO users (id, login) VALUES (%s, %s)"
        " ON CONFLICT (id) DO UPDATE SET login = EXCLUDED.login",
        (user_id, login),
    )


def upsert_org(conn, installation_id: int, login: str, account_id: int, account_type: str) -> int:
    row = conn.execute(
        "INSERT INTO organizations (installation_id, account_login, account_id, account_type)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (installation_id) DO UPDATE SET"
        " account_login = EXCLUDED.account_login RETURNING id",
        (installation_id, login, account_id, account_type),
    ).fetchone()
    return row["id"]


def set_member(conn, org_id: int, user_id: int, role: str) -> None:
    """Adds a member; never demotes an owner by seeing them again as a member."""
    conn.execute(
        "INSERT INTO members (org_id, user_id, role) VALUES (%s, %s, %s)"
        " ON CONFLICT (org_id, user_id) DO UPDATE SET role ="
        " CASE WHEN members.role = 'owner' THEN 'owner' ELSE EXCLUDED.role END",
        (org_id, user_id, role),
    )


def org_by_installation(conn, installation_id: int) -> dict | None:
    return conn.execute(
        "SELECT * FROM organizations WHERE installation_id = %s", (installation_id,)
    ).fetchone()


def delete_org(conn, installation_id: int) -> bool:
    conn.execute(
        "INSERT INTO removed_installations (installation_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (installation_id,),
    )
    done = conn.execute("DELETE FROM organizations WHERE installation_id = %s", (installation_id,))
    return done.rowcount > 0


def installation_removed(conn, installation_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM removed_installations WHERE installation_id = %s", (installation_id,)
        ).fetchone()
        is not None
    )


def role_in(conn, user_id: int, org_id: int) -> str | None:
    row = conn.execute(
        "SELECT role FROM members WHERE org_id = %s AND user_id = %s", (org_id, user_id)
    ).fetchone()
    return row["role"] if row else None


def orgs_of(conn, user_id: int) -> list[dict]:
    return conn.execute(
        "SELECT o.id, o.account_login, o.account_type, o.model_provider, o.models,"
        " (o.model_key IS NOT NULL) AS has_model_key, m.role,"
        " EXISTS (SELECT 1 FROM jobs j WHERE j.org_id = o.id AND j.kind = 'sync'"
        "  AND j.status IN ('queued', 'running')) AS syncing FROM organizations o"
        " JOIN members m ON m.org_id = o.id WHERE m.user_id = %s ORDER BY o.account_login",
        (user_id,),
    ).fetchall()


# ---------------------------------------------------------------- repositories


def upsert_repo(conn, org_id: int, github_id: int, full_name: str, branch: str = "main") -> int:
    row = conn.execute(
        "INSERT INTO repositories (org_id, github_id, full_name, default_branch)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (github_id) DO UPDATE SET"
        " full_name = EXCLUDED.full_name, default_branch = EXCLUDED.default_branch,"
        " org_id = EXCLUDED.org_id RETURNING id",
        (org_id, github_id, full_name, branch or "main"),
    ).fetchone()
    return row["id"]


def repo_by_github_id(conn, github_id: int) -> dict | None:
    return conn.execute(
        "SELECT r.*, o.installation_id FROM repositories r"
        " JOIN organizations o ON o.id = r.org_id WHERE r.github_id = %s",
        (github_id,),
    ).fetchone()


def repo(conn, repo_id: int) -> dict | None:
    return conn.execute(
        "SELECT r.*, o.installation_id FROM repositories r"
        " JOIN organizations o ON o.id = r.org_id WHERE r.id = %s",
        (repo_id,),
    ).fetchone()


def remove_repo(conn, github_id: int) -> None:
    conn.execute("DELETE FROM repositories WHERE github_id = %s", (github_id,))


# ---------------------------------------------------------------- findings (RepoDoctor)


def record_scan(
    conn,
    repo_id: int,
    commit: str,
    findings: list[Finding],
    file_count: int,
    seconds: float,
    sources: set[str],
    scan_type: str = "push",
) -> dict:
    """The server twin of HealthStore.record: same identities and state changes, per repo."""
    with conn.transaction():
        conn.execute("SELECT id FROM repositories WHERE id = %s FOR UPDATE", (repo_id,))
        ts = now()
        scan_id = conn.execute(
            "INSERT INTO scans (repository_id, scan_type, commit_sha, files, seconds, tools)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (repo_id, scan_type, commit, file_count, seconds,
             sorted(sources - {"builtin"})),
        ).fetchone()["id"]  # fmt: skip
        seen: set[str] = set()
        new = reopened = 0
        for f in findings:
            fp = f.fingerprint
            if fp in seen:
                continue
            seen.add(fp)
            row = conn.execute(
                "SELECT id, status FROM findings WHERE repository_id = %s AND fingerprint = %s",
                (repo_id, fp),
            ).fetchone()
            if row is None:
                new += 1
                conn.execute(
                    "INSERT INTO findings (repository_id, fingerprint, rule, category, severity,"
                    " file, line, title, detail, data, first_scan, last_scan, first_seen,"
                    " last_seen) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (repo_id, fp, f.rule, f.category, f.severity, f.file, f.line, f.title,
                     f.detail, Jsonb(f.data), scan_id, scan_id, ts, ts),
                )  # fmt: skip
                continue
            was_resolved = row["status"] == "resolved"
            reopened += was_resolved
            conn.execute(
                "UPDATE findings SET severity = %s, line = %s, title = %s, detail = %s,"
                " data = %s, last_scan = %s, last_seen = %s, resolved_at = NULL,"
                " status = CASE WHEN status = 'resolved' THEN 'open' ELSE status END,"
                " reopened = reopened + %s WHERE id = %s",
                (f.severity, f.line, f.title, f.detail, Jsonb(f.data), scan_id, ts,
                 int(was_resolved), row["id"]),
            )  # fmt: skip
        resolved = 0
        stale = conn.execute(
            "SELECT id, rule FROM findings WHERE repository_id = %s AND status = 'open'"
            " AND last_scan < %s",
            (repo_id, scan_id),
        ).fetchall()
        for row in stale:
            if source_of(row["rule"]) in sources:
                done = conn.execute(  # a person may have ignored it in the meantime
                    "UPDATE findings SET status = 'resolved', resolved_at = %s"
                    " WHERE id = %s AND status = 'open'",
                    (ts, row["id"]),
                )
                resolved += done.rowcount
        open_rows = conn.execute(
            "SELECT category, severity FROM findings WHERE repository_id = %s AND status = 'open'",
            (repo_id,),
        ).fetchall()
        scores = scores_for(open_rows, file_count)
        conn.execute(
            "INSERT INTO snapshots (scan_id, repository_id, overall, scores, open_findings,"
            " new_findings, resolved_findings) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (scan_id, repo_id, scores["overall"], Jsonb(scores), len(open_rows), new, resolved),
        )
        conn.execute(
            "UPDATE repositories SET overall = %s, last_scan_at = %s WHERE id = %s",
            (scores["overall"], ts, repo_id),
        )
    return {"scan_id": scan_id, "scores": scores, "open": len(open_rows), "new": new,
            "resolved": resolved, "reopened": reopened}  # fmt: skip


def findings(conn, repo_id: int, status: str | None = "open") -> list[dict]:
    if status is None:
        return conn.execute(
            "SELECT * FROM findings WHERE repository_id = %s ORDER BY id", (repo_id,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM findings WHERE repository_id = %s AND status = %s ORDER BY id",
        (repo_id, status),
    ).fetchall()


def finding(conn, repo_id: int, finding_id: int) -> dict | None:
    return conn.execute(
        "SELECT * FROM findings WHERE repository_id = %s AND id = %s", (repo_id, finding_id)
    ).fetchone()


def set_finding_status(conn, repo_id: int, finding_id: int, status: str) -> dict | None:
    if status not in ("open", "ignored"):
        raise ValueError("status must be open or ignored")
    return conn.execute(
        "UPDATE findings SET status = %s WHERE repository_id = %s AND id = %s"
        " AND status IN ('open', 'ignored') RETURNING *",
        (status, repo_id, finding_id),
    ).fetchone()


def snapshots(conn, repo_id: int, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT s.*, c.commit_sha FROM snapshots s JOIN scans c ON c.id = s.scan_id"
        " WHERE s.repository_id = %s ORDER BY s.scan_id DESC LIMIT %s",
        (repo_id, limit),
    ).fetchall()
    return list(reversed(rows))


# ---------------------------------------------------------------- pull requests and fixes


def save_pr(conn, repo_id: int, number: int, head_sha: str, report: dict,
            event_at: str | None = None) -> dict | None:  # fmt: skip
    """Stores the report; returns the earlier row (with its comment id) if there was one."""
    old = conn.execute(
        "SELECT * FROM pull_requests WHERE repository_id = %s AND number = %s",
        (repo_id, number),
    ).fetchone()
    conn.execute(
        "INSERT INTO pull_requests (repository_id, number, head_sha, score, level, report,"
        " event_at) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (repository_id, number)"
        " DO UPDATE SET head_sha = EXCLUDED.head_sha, score = EXCLUDED.score,"
        " level = EXCLUDED.level, report = EXCLUDED.report, updated_at = now(),"
        " event_at = GREATEST(pull_requests.event_at, EXCLUDED.event_at)",
        (repo_id, number, head_sha, report["score"], report["level"], Jsonb(report), event_at),
    )
    return old


def set_pr_comment(conn, repo_id: int, number: int, comment_id: int) -> None:
    conn.execute(
        "UPDATE pull_requests SET comment_id = %s WHERE repository_id = %s AND number = %s",
        (comment_id, repo_id, number),
    )


def pull_requests(conn, repo_id: int) -> list[dict]:
    return conn.execute(
        "SELECT number, head_sha, score, level, updated_at FROM pull_requests"
        " WHERE repository_id = %s ORDER BY updated_at DESC LIMIT 50",
        (repo_id,),
    ).fetchall()


def create_fix(conn, repo_id: int, finding_id: int, user_id: int) -> tuple[dict, bool]:
    """A new fix, or the finding's fix that is still open (two clicks ask the model once).
    Returns (fix, created)."""
    row = conn.execute(
        "INSERT INTO fixes (repository_id, finding_id, requested_by) VALUES (%s, %s, %s)"
        " ON CONFLICT (finding_id) WHERE status IN ('queued', 'proposed', 'approved')"
        " DO NOTHING RETURNING *",
        (repo_id, finding_id, user_id),
    ).fetchone()
    if row is not None:
        return row, True
    return conn.execute(
        "SELECT * FROM fixes WHERE finding_id = %s AND status IN ('queued', 'proposed',"
        " 'approved')",
        (finding_id,),
    ).fetchone(), False


def fix(conn, fix_id: int) -> dict | None:
    return conn.execute("SELECT * FROM fixes WHERE id = %s", (fix_id,)).fetchone()


def update_fix(conn, fix_id: int, **values) -> dict | None:
    if not values:
        return fix(conn, fix_id)
    sets = ", ".join(f"{k} = %s" for k in values)
    args = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in values.values()]
    return conn.execute(
        f"UPDATE fixes SET {sets}, updated_at = now() WHERE id = %s RETURNING *",  # noqa: S608
        (*args, fix_id),
    ).fetchone()


def move_fix(conn, fix_id: int, frm: str, to: str, **values) -> dict | None:
    """Changes status only if it is still `frm`: two clicks on approve start one push."""
    sets = ", ".join(["status = %s"] + [f"{k} = %s" for k in values])
    args = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in values.values()]
    return conn.execute(
        f"UPDATE fixes SET {sets}, updated_at = now() WHERE id = %s AND status = %s"  # noqa: S608
        " RETURNING *",
        (to, *args, fix_id, frm),
    ).fetchone()


def fixes(conn, repo_id: int) -> list[dict]:
    return conn.execute(
        "SELECT id, finding_id, status, branch, pr_url, error, summary, created_at"
        " FROM fixes WHERE repository_id = %s ORDER BY id DESC LIMIT 50",
        (repo_id,),
    ).fetchall()


def log_usage(conn, org_id: int, repo_id: int | None, user_id: int | None, purpose: str,
              calls: list[dict]) -> None:  # fmt: skip
    for call in calls:
        conn.execute(
            "INSERT INTO model_usage (org_id, repository_id, user_id, purpose, model,"
            " input_tokens, output_tokens, cost_usd) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (org_id, repo_id, user_id, purpose, call["model"], call["input"], call["output"],
             call["cost_usd"]),
        )  # fmt: skip


def usage(conn, org_id: int, days: int = 30) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens), 0) AS input,"
        " COALESCE(SUM(output_tokens), 0) AS output, COALESCE(SUM(cost_usd), 0) AS cost_usd"
        " FROM model_usage WHERE org_id = %s AND created_at > now() - make_interval(days => %s)",
        (org_id, days),
    ).fetchone()
    return dict(row)


def first_delivery(conn, delivery_id: str, event: str) -> bool:
    """Records a webhook delivery; False if GitHub already sent this one (a redelivery)."""
    row = conn.execute(
        "INSERT INTO webhook_deliveries (delivery_id, event) VALUES (%s, %s)"
        " ON CONFLICT (delivery_id) DO NOTHING RETURNING delivery_id",
        (delivery_id, event),
    ).fetchone()
    return row is not None


def prune_deliveries(conn, days: int = 30) -> int:
    return conn.execute(
        "DELETE FROM webhook_deliveries WHERE received_at < now() - make_interval(days => %s)",
        (days,),
    ).rowcount


def metrics(conn) -> str:
    """The queue, webhooks and fixes in Prometheus' text format."""
    lines: list[str] = []

    def gauge(name: str, help_text: str, rows, label_keys=()) -> None:
        lines.append(f"# HELP tokenwise_{name} {help_text}")
        lines.append(f"# TYPE tokenwise_{name} gauge")
        for row in rows:
            labels = ",".join(f'{k}="{row[k]}"' for k in label_keys)
            value = row["value"] if row["value"] is not None else 0
            lines.append(f"tokenwise_{name}{{{labels}}} {float(value):g}" if labels
                         else f"tokenwise_{name} {float(value):g}")  # fmt: skip

    q = conn.execute
    gauge("jobs", "Jobs by kind and status.", q(
        "SELECT kind, status, COUNT(*) AS value FROM jobs GROUP BY kind, status"
    ).fetchall(), ("kind", "status"))  # fmt: skip
    gauge("jobs_failed_last_hour", "Jobs that ended failed in the last hour.", q(
        "SELECT kind, COUNT(*) AS value FROM jobs WHERE status = 'failed'"
        " AND finished_at > now() - interval '1 hour' GROUP BY kind"
    ).fetchall(), ("kind",))  # fmt: skip
    gauge("job_seconds_last_hour", "Average run time of jobs finished in the last hour.", q(
        "SELECT kind, AVG(EXTRACT(EPOCH FROM finished_at - started_at)) AS value FROM jobs"
        " WHERE finished_at > now() - interval '1 hour' AND started_at IS NOT NULL GROUP BY kind"
    ).fetchall(), ("kind",))  # fmt: skip
    gauge("oldest_queued_job_seconds", "How long the oldest ready job has waited.", q(
        "SELECT COALESCE(MAX(EXTRACT(EPOCH FROM now() - run_after)), 0) AS value FROM jobs"
        " WHERE status = 'queued' AND run_after <= now()"
    ).fetchall())  # fmt: skip
    gauge("webhooks_last_hour", "Webhook deliveries received in the last hour.", q(
        "SELECT event, COUNT(*) AS value FROM webhook_deliveries"
        " WHERE received_at > now() - interval '1 hour' GROUP BY event"
    ).fetchall(), ("event",))  # fmt: skip
    gauge("fixes", "Fixes by status.", q(
        "SELECT status, COUNT(*) AS value FROM fixes GROUP BY status"
    ).fetchall(), ("status",))  # fmt: skip
    gauge("organizations", "Installed organizations.",
          q("SELECT COUNT(*) AS value FROM organizations").fetchall())  # fmt: skip
    gauge("repositories", "Repositories watched.",
          q("SELECT COUNT(*) AS value FROM repositories").fetchall())  # fmt: skip
    return "\n".join(lines) + "\n"


def to_json(value) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)
