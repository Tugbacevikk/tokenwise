"""Cost log: every request and every model call, in .tokenwise/ledger.db (SQLite)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Settings
from .pricing import reply_cost

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'tokenwise',
    request TEXT NOT NULL,
    files TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    model TEXT NOT NULL,
    ok INTEGER NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    added INTEGER NOT NULL,
    removed INTEGER NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS calls (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    role TEXT NOT NULL,
    model TEXT NOT NULL,
    cached INTEGER NOT NULL,
    input INTEGER NOT NULL,
    output INTEGER NOT NULL,
    cache_read INTEGER NOT NULL,
    cache_write INTEGER NOT NULL,
    cost REAL NOT NULL
);
"""


def ledger_path(settings: Settings) -> Path:
    return settings.state_dir / "ledger.db"


def _since(days: int | None) -> str:
    if days is None:
        return ""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __del__(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    def record(self, result, method: str = "tokenwise") -> int:
        """Stores a pipeline.Result (or anything with the same fields). Returns the run id."""
        added, removed = result.lines
        cursor = self.db.execute(
            "INSERT INTO runs (ts, method, request, files, difficulty, model, ok, added, removed,"
            " error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(timespec="seconds"),
                method,
                result.request,
                ",".join(result.files),
                result.level,
                result.model,
                int(result.ok),
                added,
                removed,
                result.error,
            ),
        )
        run_id = cursor.lastrowid
        self.db.executemany(
            "INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    r.role,
                    r.model,
                    int(r.cached),
                    r.usage.input_tokens,
                    r.usage.output_tokens,
                    r.usage.cache_read_tokens,
                    r.usage.cache_write_tokens,
                    reply_cost(r, result.prices),
                )
                for r in result.replies
            ],
        )
        self.db.commit()
        return run_id

    def mark_applied(self, run_id: int) -> None:
        self.db.execute("UPDATE runs SET applied = 1 WHERE id = ?", (run_id,))
        self.db.commit()

    def summary(self, days: int | None = None, method: str = "tokenwise") -> dict:
        since = _since(days)
        runs = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(ok), 0) ok, COALESCE(SUM(applied), 0) applied,"
            " COALESCE(SUM(added), 0) added, COALESCE(SUM(removed), 0) removed"
            " FROM runs WHERE ts >= ? AND method = ?",
            (since, method),
        ).fetchone()
        calls = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(c.cached), 0) cached,"
            " COALESCE(SUM(c.input), 0) input, COALESCE(SUM(c.output), 0) output,"
            " COALESCE(SUM(c.cache_read), 0) cache_read, COALESCE(SUM(c.cost), 0) cost"
            " FROM calls c JOIN runs r ON r.id = c.run_id WHERE r.ts >= ? AND r.method = ?",
            (since, method),
        ).fetchone()
        by_model = self.db.execute(
            "SELECT c.model, COUNT(*) calls, SUM(c.cached) cached, SUM(c.input) input,"
            " SUM(c.output) output, SUM(c.cost) cost"
            " FROM calls c JOIN runs r ON r.id = c.run_id WHERE r.ts >= ? AND r.method = ?"
            " GROUP BY c.model ORDER BY cost DESC, calls DESC",
            (since, method),
        ).fetchall()
        n = runs["n"]
        return {
            "days": days,
            "runs": n,
            "ok": runs["ok"],
            "applied": runs["applied"],
            "lines_added": runs["added"],
            "lines_removed": runs["removed"],
            "calls": calls["n"],
            "cache_hits": calls["cached"],
            "cache_hit_rate": calls["cached"] / calls["n"] if calls["n"] else 0.0,
            "input_tokens": calls["input"],
            "output_tokens": calls["output"],
            "cache_read_tokens": calls["cache_read"],
            "cost_usd": calls["cost"],
            "avg_cost_per_run": calls["cost"] / n if n else 0.0,
            "by_model": [dict(row) for row in by_model],
        }

    def rows(self, days: int | None = None) -> list[dict]:
        """One row per run with its totals, for CSV export."""
        cursor = self.db.execute(
            "SELECT r.id, r.ts, r.method, r.request, r.files, r.difficulty, r.model, r.ok,"
            " r.applied, r.added, r.removed, COUNT(c.run_id) calls,"
            " COALESCE(SUM(c.input), 0) input, COALESCE(SUM(c.output), 0) output,"
            " COALESCE(SUM(c.cache_read), 0) cache_read, COALESCE(SUM(c.cost), 0) cost"
            " FROM runs r LEFT JOIN calls c ON c.run_id = r.id WHERE r.ts >= ?"
            " GROUP BY r.id ORDER BY r.id",
            (_since(days),),
        )
        return [dict(row) for row in cursor.fetchall()]

    def backdate(self, run_id: int, ts: str) -> None:
        """For tests: move a run to another time."""
        self.db.execute("UPDATE runs SET ts = ? WHERE id = ?", (ts, run_id))
        self.db.commit()
