"""The worker: takes jobs from the queue and does the slow work in a throwaway clone.

  sync         the installation's repositories (default branches); first scan of new ones
  scan         RepoDoctor on the default branch after a push
  pr_risk      ReleaseGuard on a pull request; one comment per PR, updated on every push
  fix_propose  the Agent drafts a change for a finding, with the org's own model key
  fix_push     after an owner approved it: a `tokenwise/fix-N` branch and a draft PR

Nothing here writes to a default branch. The project's check runs only in a container."""

from __future__ import annotations

import logging
import socket
import subprocess
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from ..editor import Change, apply_changes
from ..graph import build_graph
from ..health import collect, task_for
from ..pipeline import run as run_agent
from ..release import GuardError, assess, to_markdown
from . import db, queue
from .config import ServerConfig, make_model, repo_settings
from .github import GitHub, GitHubError, mask

log = logging.getLogger("tokenwise.worker")
BOT_NAME = "tokenwise"
BOT_EMAIL = "tokenwise@users.noreply.github.com"


class JobError(Exception):
    """A failure that retrying will not fix (missing row, bad state). `fix_status` is what a
    fix job leaves its fix as: "failed", or "stale" when the code moved on."""

    def __init__(self, message: str, fix_status: str = "failed") -> None:
        super().__init__(message)
        self.fix_status = fix_status


class Heartbeat:
    """Keeps a running job's lock fresh from a side thread with its own connection, so a
    long job is not mistaken for a dead worker."""

    def __init__(self, connect: Callable, job_id: int, worker: str,
                 every: float = queue.HEARTBEAT_SECONDS) -> None:  # fmt: skip
        self.connect, self.job_id, self.worker, self.every = connect, job_id, worker, every
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            conn = self.connect()
        except Exception:  # noqa: BLE001 - no heartbeat is safer than a crashed job
            return
        try:
            while not self.stop.wait(self.every):
                queue.heartbeat(conn, self.job_id, self.worker)
        finally:
            conn.close()

    def __enter__(self) -> Heartbeat:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join(timeout=5)


class Worker:
    def __init__(
        self,
        conn,
        github: GitHub,
        server: ServerConfig,
        name: str = "",
        model_factory: Callable | None = None,
        connect: Callable | None = None,
    ) -> None:
        self.conn = conn
        self.connect = connect or (lambda: db.connect(server.database_url))
        self.github = github
        self.server = server
        self.name = name or f"{socket.gethostname()}-{id(self):x}"
        self.model_factory = model_factory or (lambda s, key: make_model(s, key, server))
        self.handlers = {
            "sync": self.sync,
            "scan": self.scan,
            "pr_risk": self.pr_risk,
            "fix_propose": self.fix_propose,
            "fix_push": self.fix_push,
        }

    # ------------------------------------------------------------ loop

    def run_once(self) -> dict | None:
        """Takes and runs one job. Returns the job, or None when the queue is empty."""
        job = queue.claim(self.conn, self.name)
        if job is None:
            return None
        handler = self.handlers.get(job["kind"])
        try:
            if handler is None:
                raise JobError(f"unknown job kind {job['kind']}")
            with Heartbeat(self.connect, job["id"], self.name):
                handler(job["payload"])
        except JobError as exc:
            job["status"] = queue.fail(self.conn, job, mask(str(exc)), retry=False)
            self._job_failed(job, str(exc), exc.fix_status)
        except Exception as exc:  # noqa: BLE001 - one job must never stop the worker
            message = mask(f"{type(exc).__name__}: {exc}")
            log.warning("job %s failed: %s\n%s", job["id"], message, mask(traceback.format_exc()))
            job["status"] = queue.fail(self.conn, job, message)
            if job["status"] == "failed":
                self._job_failed(job, message)
        else:
            queue.finish(self.conn, job["id"], self.name)
            job["status"] = "done"
        return job

    def run_forever(self, idle: float = 2.0) -> None:
        while True:
            if self.run_once() is None:
                time.sleep(idle)

    def _job_failed(self, job: dict, message: str, fix_status: str = "failed") -> None:
        if job["kind"] in ("fix_propose", "fix_push"):
            db.update_fix(self.conn, job["payload"]["fix_id"], status=fix_status,
                          error=mask(message)[:2000])  # fmt: skip

    def _workspace(self) -> tempfile.TemporaryDirectory:
        return tempfile.TemporaryDirectory(
            prefix="tokenwise-job-", dir=self.server.work_dir or None
        )

    def _repo(self, repo_id: int) -> dict:
        row = db.repo(self.conn, repo_id)
        if row is None:
            raise JobError(f"repository {repo_id} is gone")
        return row

    def _org(self, org_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM organizations WHERE id = %s", (org_id,)).fetchone()
        if row is None:
            raise JobError("organization is gone")
        return row

    # ------------------------------------------------------------ repositories

    def sync(self, payload: dict) -> None:
        org = self._org(payload["org_id"])
        listed = self.github.installation_repos(org["installation_id"])
        keep = set()
        for item in listed:
            keep.add(item["id"])
            repo_id = db.upsert_repo(self.conn, org["id"], item["id"], item["full_name"],
                                     item.get("default_branch") or "main")  # fmt: skip
            scanned = db.repo(self.conn, repo_id)["last_scan_at"]
            if scanned is None:
                queue.enqueue(self.conn, "scan", {"repository_id": repo_id,
                                                  "scan_type": "initial"},
                              org["id"], dedupe_key=f"scan:{repo_id}")  # fmt: skip
        for row in self.conn.execute("SELECT github_id FROM repositories WHERE org_id = %s",
                                     (org["id"],)).fetchall():  # fmt: skip
            if row["github_id"] not in keep:
                db.remove_repo(self.conn, row["github_id"])

    # ------------------------------------------------------------ RepoDoctor

    def _older_than_seen(self, payload: dict, seen: str | None) -> bool:
        """A webhook that GitHub sent before the one already handled (they can arrive late)."""
        event_at = payload.get("event_at") or ""
        return bool(event_at and seen and event_at < seen)

    def scan(self, payload: dict) -> None:
        repo = self._repo(payload["repository_id"])
        if self._older_than_seen(payload, repo.get("last_event_at")):
            log.info("scan of %s skipped: a newer push was scanned", repo["full_name"])
            return
        with self._workspace() as tmp:
            root = Path(tmp) / "repo"
            sha = self.github.clone(repo["installation_id"], repo["full_name"],
                                    payload.get("ref") or repo["default_branch"], root)  # fmt: skip
            settings = repo_settings(root, self.server)
            started = time.monotonic()
            graph = build_graph(settings)
            found, tools = collect(settings, graph)
            db.record_scan(self.conn, repo["id"], sha, found, len(graph.files),
                           round(time.monotonic() - started, 2), {"builtin", *tools.ran},
                           payload.get("scan_type", "push"))  # fmt: skip
        if payload.get("event_at"):
            self.conn.execute(
                "UPDATE repositories SET last_event_at = GREATEST(COALESCE(last_event_at, ''), %s)"
                " WHERE id = %s",
                (payload["event_at"], repo["id"]),
            )

    # ------------------------------------------------------------ ReleaseGuard

    def pr_risk(self, payload: dict) -> None:
        repo = self._repo(payload["repository_id"])
        number, base = payload["number"], payload["base_ref"]
        seen = self.conn.execute(
            "SELECT event_at FROM pull_requests WHERE repository_id = %s AND number = %s",
            (repo["id"], number),
        ).fetchone()
        if self._older_than_seen(payload, seen and seen["event_at"]):
            log.info("PR #%s report skipped: a newer push was reported", number)
            return
        with self._workspace() as tmp:
            root = Path(tmp) / "repo"
            sha = self.github.clone(repo["installation_id"], repo["full_name"],
                                    payload["head_sha"], root)  # fmt: skip
            self.github.fetch(repo["installation_id"], repo["full_name"], base, root)
            settings = repo_settings(root, self.server)
            try:
                report = assess(settings, f"origin/{base}")
            except GuardError as exc:
                raise JobError(str(exc)) from exc
        data = report.to_dict()
        data["markdown"] = to_markdown(report)
        old = db.save_pr(self.conn, repo["id"], number, sha, data, payload.get("event_at"))
        link = f"{self.server.public_url}/#/repos/{repo['id']}"
        body = data["markdown"] + f"\n[Tokenwise panelinde aç]({link}) · commit `{sha[:12]}`\n"
        comment_id = self.github.upsert_comment(
            repo["installation_id"], repo["full_name"], number, body,
            old["comment_id"] if old else None,
        )  # fmt: skip
        db.set_pr_comment(self.conn, repo["id"], number, comment_id)

    # ------------------------------------------------------------ Agent

    def fix_propose(self, payload: dict) -> None:
        fix = db.fix(self.conn, payload["fix_id"])
        if fix is None or fix["status"] != "queued":
            raise JobError("fix is not waiting for a proposal")
        repo = self._repo(fix["repository_id"])
        org = self._org(repo["org_id"])
        finding = db.finding(self.conn, repo["id"], fix["finding_id"])
        if finding is None:
            raise JobError("finding is gone")
        task = task_for(finding)
        with self._workspace() as tmp:
            root = Path(tmp) / "repo"
            sha = self.github.clone(repo["installation_id"], repo["full_name"],
                                    repo["default_branch"], root)  # fmt: skip
            settings = repo_settings(root, self.server, org)
            llm = self.model_factory(settings, self.server.decrypt(org["model_key"]))
            result = run_agent(task["request"], settings, llm, files=task["files"])
        data = result.to_dict()
        db.log_usage(self.conn, org["id"], repo["id"], fix["requested_by"], "fix", data["calls"])
        summary = {
            "request": task["request"],
            "model": result.model,
            "check": data["check"],
            "lines": data["lines"],
            "cost_usd": data["cost_usd"],
        }
        if not result.ok:
            db.update_fix(self.conn, fix["id"], status="failed", summary=summary, base_sha=sha,
                          error=mask(result.error or "no change"))  # fmt: skip
            return
        changes = [{"path": c["path"], "old": c["old"], "new": c["new"], "diff": c["diff"]}
                   for c in data["changes"]]  # fmt: skip
        if db.move_fix(self.conn, fix["id"], "queued", "proposed", summary=summary,
                       base_sha=sha, changes=changes) is None:  # fmt: skip
            raise JobError("the fix changed while the proposal was being made")

    def fix_push(self, payload: dict) -> None:
        fix = db.fix(self.conn, payload["fix_id"])
        if fix is None or fix["status"] != "approved":
            raise JobError("fix is not approved")
        repo = self._repo(fix["repository_id"])
        finding = db.finding(self.conn, repo["id"], fix["finding_id"])
        branch = f"tokenwise/fix-{fix['id']}"
        changes = [Change(c["path"], c["old"], c["new"]) for c in fix["changes"] or []]
        if not changes:
            raise JobError("the fix has no changes")
        with self._workspace() as tmp:
            root = Path(tmp) / "repo"
            # the default branch as it is now, not as it was when the model saw it: if a file
            # changed since, the approved diff would no longer be what was reviewed
            head = self.github.clone(repo["installation_id"], repo["full_name"],
                                     repo["default_branch"], root)  # fmt: skip
            for change in changes:
                path = (root / change.path).resolve()
                if not path.is_relative_to(root.resolve()):
                    raise JobError(f"path outside the repository: {change.path}")
                current = path.read_text("utf-8") if path.exists() else None
                expected = change.old
                if (current.replace("\r\n", "\n") if current is not None else None) != expected:
                    raise JobError(
                        f"{change.path} öneri hazırlandıktan sonra {repo['default_branch']} "
                        f"dalında değişti ({(fix['base_sha'] or '')[:12]} → {head[:12]}). "
                        "Yeni bir öneri isteyin.",
                        fix_status="stale",
                    )
            apply_changes(replace(repo_settings(root, self.server), root=root), changes)
            title = f"tokenwise: {finding['title'] if finding else 'fix'}"
            message = (
                f"{title}\n\n{(fix['summary'] or {}).get('request', '')}\n\n"
                f"Approved in Tokenwise (fix #{fix['id']}).\n\n"
                f"Co-Authored-By: {BOT_NAME} <{BOT_EMAIL}>\n"
            )
            _commit(root, [c.path for c in changes], message)
            self.github.push_branch(repo["installation_id"], repo["full_name"], root, branch)
        summary = fix["summary"] or {}
        check = summary.get("check") or {}
        state = {True: "geçti", False: "geçmedi"}.get(check.get("passed"), "çalıştırılmadı")
        body = (
            f"Bu taslak PR, Tokenwise'ta onaylanan bir düzeltmedir (fix #{fix['id']}).\n\n"
            f"**İstek:** {summary.get('request', '')}\n\n"
            f"**Model:** `{summary.get('model', '')}` · **Kontrol:** {state} · "
            f"+{(summary.get('lines') or {}).get('added', 0)} "
            f"−{(summary.get('lines') or {}).get('removed', 0)} satır\n\n"
            "Merge kararı sizde: Tokenwise ana dala hiçbir zaman yazmaz."
        )
        try:
            url = self.github.create_draft_pr(repo["installation_id"], repo["full_name"], branch,
                                              repo["default_branch"], title, body)  # fmt: skip
        except GitHubError as exc:
            raise JobError(str(exc)) from exc
        db.update_fix(self.conn, fix["id"], status="pushed", branch=branch, pr_url=url, error=None)


def _commit(root: Path, paths: list[str], message: str) -> None:
    base = ["git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]  # fmt: skip
    for args in (["add", "--", *paths], ["commit", "-q", "-m", message]):
        done = subprocess.run(base + args, cwd=root, capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise JobError(f"git {args[0]} failed: {done.stderr.strip()[-400:]}")


def main() -> None:  # pragma: no cover - started by `tokenwise server worker`
    from .app import make_github

    logging.basicConfig(level=logging.INFO)
    server = ServerConfig.from_env()
    if server.work_dir:  # check copies too: the Docker host must see them at the same path
        Path(server.work_dir).mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = server.work_dir
    conn = db.connect(server.database_url)
    db.migrate(conn)
    Worker(conn, make_github(server), server).run_forever()
