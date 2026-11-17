"""Local web interface behind `tokenwise serve`. Routes only: the work is in `services`,
shared with the CLI and the hosted server.

The server can change files in the projects it serves, so by default it listens on 127.0.0.1
only. Another address needs a token, which every /api request must send in a header.

Endpoints (pid is a project id from GET /api/projects):
  GET  /                                        the page
  GET  /api/projects                            the projects served
  GET  /api/projects/{pid}                      files, map size, models, budget, git or not
  POST /api/projects/{pid}/estimate             rough input tokens and cost before running
  POST /api/projects/{pid}/ask                  one change request (also used to run a thread)
  POST /api/projects/{pid}/apply                write an earlier proposal; runs the check after
  POST /api/projects/{pid}/undo                 undo the last applied change
  POST /api/projects/{pid}/commit               git commit of an applied change
  POST /api/projects/{pid}/runs/{run}/review    a second model reviews a proposal
  POST /api/projects/{pid}/plan                 split a goal into work threads
  GET  /api/projects/{pid}/plans                saved plans, newest first
  GET  /api/projects/{pid}/plans/{id}           one plan
  POST /api/projects/{pid}/plans/{id}/chat      change a plan with an instruction
  PUT  /api/projects/{pid}/plans/{id}/threads/{n}   set a thread's status
  GET  /api/projects/{pid}/plans/{id}/export    the plan as Markdown
  GET  /api/projects/{pid}/sessions             conversations, newest first
  POST /api/projects/{pid}/sessions             start a conversation
  GET  /api/projects/{pid}/sessions/{id}        one conversation
  POST /api/projects/{pid}/sessions/{id}/messages   one turn: the assistant answers
"""

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .config import Settings, load_settings
from .editor import undo_last
from .health import HealthStore, health_path, ranked, task_for, verify_fixed
from .health import scan as run_scan
from .history import load_history
from .ledger import Ledger, ledger_path
from .llm import Usage, make_llm
from .pipeline import Result
from .pricing import price_for, usage_cost
from .release import GuardError, assess
from .repo_map import build_map, estimate_tokens, list_source_files
from .services import ServiceError
from .services.assistant import MODEL_ERRORS, Assistant
from .services.changes import (
    SPEND_METHODS,
    apply_proposal,
    budget,
    commit_paths,
    is_git_repo,
    require_budget,
)
from .services.chat import take_turn
from .services.records import (  # noqa: F401 - parse_plan is part of this module's API
    load_plan,
    load_session,
    new_session,
    parse_plan,
    plan_markdown,
    plan_summaries,
    save_plan,
    save_session,
    session_summaries,
    set_thread_status,
)
from .textio import read_source

STATIC_PAGE = Path(__file__).parent / "static" / "index.html"
INSTRUCTION_TOKENS = 300  # rough size of the editor's instructions, for the estimate


class AskBody(BaseModel):
    request: str
    model: str = ""
    check: str = ""
    files: list[str] = []
    over_budget: bool = False


class PlanBody(BaseModel):
    goal: str
    model: str = ""


class ApplyBody(BaseModel):
    run_id: int


class CommitBody(BaseModel):
    run_id: int


class EstimateBody(BaseModel):
    request: str = ""
    files: list[str] = []


class RiskBody(BaseModel):
    base: str = ""


class StatusBody(BaseModel):
    status: str


class ChatBody(BaseModel):
    text: str
    model: str = ""


class SessionBody(BaseModel):
    title: str = ""


class TurnBody(BaseModel):
    text: str
    intent: str = "auto"  # auto | change | refine | explain | plan
    model: str = ""
    files: list[str] = []
    over_budget: bool = False


@dataclass
class Project:
    id: str
    root: Path
    lock: threading.Lock = field(default_factory=threading.Lock)  # one request at a time
    pending: dict[int, Result] = field(default_factory=dict)  # run id -> proposal to apply
    applied: dict[int, tuple[str, list[str]]] = field(default_factory=dict)  # for commit
    diffs: dict[int, tuple[str, str]] = field(default_factory=dict)  # run id -> request, diff


def project_ids(roots: list[Path | str]) -> dict[str, Path]:
    """A short unique id per folder: its name, with -2, -3... when two folders share a name."""
    ids: dict[str, Path] = {}
    for root in roots:
        root = Path(root).resolve()
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name) or "project"
        pid, n = base, 2
        while pid in ids:
            pid, n = f"{base}-{n}", n + 1
        ids[pid] = root
    return ids


def create_app(roots, llm_factory=None, token: str = "") -> FastAPI:
    """The app for one or more projects. `llm_factory(settings)` returns the model client;
    tests pass a fake one. A non-empty `token` must be sent as X-Tokenwise-Token on /api calls."""
    if isinstance(roots, (str, Path)):
        roots = [roots]
    factory = llm_factory or make_llm
    projects = {pid: Project(pid, root) for pid, root in project_ids(list(roots)).items()}
    app = FastAPI(title="tokenwise")

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    if token:

        @app.middleware("http")
        async def require_token(request, call_next):
            if (
                request.url.path.startswith("/api/")
                and request.headers.get("x-tokenwise-token") != token
            ):
                return JSONResponse({"detail": "Erişim anahtarı gerekli."}, status_code=401)
            return await call_next(request)

    def project(pid: str) -> Project:
        found = projects.get(pid)
        if found is None:
            raise HTTPException(404, "Proje bulunamadı.")
        return found

    def assistant(settings: Settings) -> Assistant:
        return Assistant(settings, factory)

    def proposer(proj: Project, helper: Assistant):
        """Runs one proposal and keeps it in memory, so it can be applied, committed or
        reviewed; returns (run id, result, diff)."""

        def propose(request: str, model: str, files: list[str]):
            run_id, result = helper.propose(request, model, files)
            diff = "\n".join(c.diff for c in result.changes)
            if result.changes:
                proj.pending[run_id] = result
                proj.diffs[run_id] = (request, diff)
            return run_id, result, diff

        return propose

    def model_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except MODEL_ERRORS as exc:
            raise HTTPException(400, str(exc)) from None

    # --- routes --------------------------------------------------------------------------

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_PAGE)

    @app.get("/api/projects")
    def list_projects() -> list[dict]:
        return [{"id": p.id, "name": p.root.name, "root": str(p.root)} for p in projects.values()]

    @app.get("/api/projects/{pid}")
    def project_info(pid: str) -> dict:
        proj = project(pid)
        settings = load_settings(proj.root)
        files = list_source_files(settings)
        return {
            "id": proj.id,
            "name": proj.root.name,
            "files": files,
            "map_tokens": estimate_tokens(build_map(settings, files)),
            "models": list(settings.models),
            "check": settings.check,
            "git": is_git_repo(proj.root),
            "budget": {
                "spent_usd": budget(settings).spent,
                "limit_usd": settings.budget_usd_week,
            },
        }

    # --- RepoDoctor and ReleaseGuard -----------------------------------------------------

    @app.post("/api/projects/{pid}/health/scan")
    def health_scan(pid: str) -> dict:
        proj = project(pid)
        with proj.lock:
            return run_scan(load_settings(proj.root)).__dict__

    @app.get("/api/projects/{pid}/health")
    def health_info(pid: str, limit: int = 50) -> dict:
        settings = load_settings(project(pid).root)
        with HealthStore(health_path(settings)) as store:
            snaps = store.snapshots(30)
            rows = store.open_findings()
        rows = ranked(rows, load_history(settings.root))[: max(1, min(limit, 500))]
        return {
            "latest": snaps[-1] if snaps else None,
            "trend": [{"ts": s["ts"], "overall": s["overall"]} for s in snaps],
            "findings": rows,
        }

    @app.post("/api/projects/{pid}/findings/{finding_id}/status")
    def finding_status(pid: str, finding_id: int, body: StatusBody) -> dict:
        settings = load_settings(project(pid).root)
        if body.status not in ("open", "ignored"):
            raise HTTPException(400, "Durum open ya da ignored olmalı.")
        with HealthStore(health_path(settings)) as store:
            row = store.set_status(finding_id, body.status)
        if row is None:
            raise HTTPException(404, "Bulgu yok.")
        return row

    @app.get("/api/projects/{pid}/findings/{finding_id}/task")
    def finding_task(pid: str, finding_id: int) -> dict:
        settings = load_settings(project(pid).root)
        with HealthStore(health_path(settings)) as store:
            row = store.get(finding_id)
        if row is None:
            raise HTTPException(404, "Bulgu yok.")
        return {**task_for(row), "finding": row}

    @app.post("/api/projects/{pid}/findings/{finding_id}/verify")
    def finding_verify(pid: str, finding_id: int) -> dict:
        proj = project(pid)
        settings = load_settings(proj.root)
        with HealthStore(health_path(settings)) as store:
            row = store.get(finding_id)
        if row is None:
            raise HTTPException(404, "Bulgu yok.")
        with proj.lock:
            gone = verify_fixed(settings, row)
        return {"finding_id": finding_id, "resolved": gone}

    @app.post("/api/projects/{pid}/risk")
    def risk(pid: str, body: RiskBody) -> dict:
        settings = load_settings(project(pid).root)
        try:
            return assess(settings, body.base.strip()).to_dict()
        except GuardError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{pid}/costs")
    def costs(pid: str, days: int = 7) -> dict:
        """Spending over the last `days` days: per method, per local day and per run."""
        proj = project(pid)
        settings = load_settings(proj.root)
        days = min(max(days, 1), 90)
        ledger = Ledger(ledger_path(settings))
        try:
            rows = ledger.rows(days)
            by_method = {m: ledger.summary(days, method=m)["cost_usd"] for m in SPEND_METHODS}
        finally:
            ledger.close()
        per_day: dict[str, float] = {}
        for row in rows:
            day = datetime.fromisoformat(row["ts"]).astimezone().date().isoformat()
            per_day[day] = per_day.get(day, 0.0) + row["cost"]
        return {
            "days": days,
            "total_usd": sum(row["cost"] for row in rows),
            "by_method": by_method,
            "by_day": [{"day": d, "cost_usd": c} for d, c in sorted(per_day.items())],
            "runs": len(rows),
        }

    @app.post("/api/projects/{pid}/estimate")
    def estimate(pid: str, body: EstimateBody) -> dict:
        proj = project(pid)
        settings = load_settings(proj.root)
        files = list_source_files(settings)
        # With files picked there is no map and no selector call, so only the files are sent.
        map_tokens = 0 if body.files else estimate_tokens(build_map(settings, files))
        files_tokens = sum(
            estimate_tokens(read_source(proj.root / f, strict=False))
            for f in body.files
            if f in files
        )
        input_tokens = (
            map_tokens + files_tokens + estimate_tokens(body.request) + INSTRUCTION_TOKENS
        )
        # Rough: input tokens at the cheapest model's price. Output is not counted.
        est = usage_cost(Usage(input_tokens, 0), price_for(settings.models[0], settings.prices))
        return {
            "input_tokens": input_tokens,
            "map_tokens": map_tokens,
            "files_tokens": files_tokens,
            "est_usd": round(est, 6),
        }

    @app.post("/api/projects/{pid}/ask")
    def ask(pid: str, body: AskBody) -> dict:
        proj = project(pid)
        with proj.lock:
            settings = load_settings(proj.root)
            require_budget(settings, body.over_budget,
                           "Yine de çalıştırmak için over_budget seçeneğini kullan.")  # fmt: skip
            if body.check:
                settings.check = body.check
            propose = proposer(proj, assistant(settings))
            run_id, result, _ = model_call(propose, body.request, body.model, body.files)
            return {"run_id": run_id, **result.to_dict()}

    @app.post("/api/projects/{pid}/apply")
    def apply(pid: str, body: ApplyBody) -> dict:
        proj = project(pid)
        with proj.lock:
            result = proj.pending.get(body.run_id)
            if result is None:
                raise HTTPException(404, "Bu öneri yok ya da zaten uygulandı.")
            done = apply_proposal(load_settings(proj.root), body.run_id, result.changes)
            del proj.pending[body.run_id]
            proj.applied[body.run_id] = (result.request, done["applied"])
            return done

    @app.post("/api/projects/{pid}/undo")
    def undo(pid: str) -> dict:
        proj = project(pid)
        with proj.lock:
            undone = undo_last(load_settings(proj.root))
            if not undone:
                raise HTTPException(404, "Geri alınacak değişiklik yok.")
            return {"undone": undone}

    @app.post("/api/projects/{pid}/commit")
    def commit(pid: str, body: CommitBody) -> dict:
        proj = project(pid)
        with proj.lock:
            if body.run_id not in proj.applied:
                raise HTTPException(404, "Bu değişiklik uygulanmamış ya da commit edilmiş.")
            request, paths = proj.applied[body.run_id]
            done = commit_paths(proj.root, request, paths)
            del proj.applied[body.run_id]
            return done

    @app.post("/api/projects/{pid}/runs/{run_id}/review")
    def review(pid: str, run_id: int) -> dict:
        proj = project(pid)
        with proj.lock:
            found = proj.diffs.get(run_id)
            if found is None:
                raise HTTPException(404, "Bu öneri artık bellekte değil. İsteği yeniden gönder.")
            request, diff = found
            return model_call(assistant(load_settings(proj.root)).review, request, diff)

    @app.post("/api/projects/{pid}/plan")
    def plan(pid: str, body: PlanBody) -> dict:
        proj = project(pid)
        with proj.lock:
            settings = load_settings(proj.root)
            require_budget(settings)
            return model_call(assistant(settings).plan, body.goal, body.model)

    @app.get("/api/projects/{pid}/plans")
    def plans(pid: str) -> list[dict]:
        return plan_summaries(load_settings(project(pid).root))

    @app.get("/api/projects/{pid}/plans/{plan_id}")
    def get_plan(pid: str, plan_id: str) -> dict:
        return load_plan(load_settings(project(pid).root), plan_id)

    @app.post("/api/projects/{pid}/plans/{plan_id}/chat")
    def plan_chat(pid: str, plan_id: str, body: ChatBody) -> dict:
        proj = project(pid)
        with proj.lock:
            settings = load_settings(proj.root)
            record = load_plan(settings, plan_id)
            return model_call(assistant(settings).edit_plan, record, body.text, body.model)

    @app.put("/api/projects/{pid}/plans/{plan_id}/threads/{index}")
    def set_status(pid: str, plan_id: str, index: int, body: StatusBody) -> dict:
        proj = project(pid)
        with proj.lock:
            return set_thread_status(load_settings(proj.root), plan_id, index, body.status)

    @app.get("/api/projects/{pid}/plans/{plan_id}/export")
    def export(pid: str, plan_id: str) -> PlainTextResponse:
        record = load_plan(load_settings(project(pid).root), plan_id)
        return PlainTextResponse(plan_markdown(record), media_type="text/markdown")

    @app.get("/api/projects/{pid}/sessions")
    def sessions(pid: str) -> list[dict]:
        return session_summaries(load_settings(project(pid).root))

    @app.post("/api/projects/{pid}/sessions")
    def start_session(pid: str, body: SessionBody) -> dict:
        proj = project(pid)
        with proj.lock:
            session = new_session(body.title)
            save_session(load_settings(proj.root), session)
            return session

    @app.get("/api/projects/{pid}/sessions/{session_id}")
    def get_session(pid: str, session_id: str) -> dict:
        return load_session(load_settings(project(pid).root), session_id)

    @app.post("/api/projects/{pid}/sessions/{session_id}/messages")
    def turn(pid: str, session_id: str, body: TurnBody) -> dict:
        """One turn of the conversation: the assistant decides what to do and answers."""
        proj = project(pid)
        with proj.lock:
            settings = load_settings(proj.root)
            session = load_session(settings, session_id)
            require_budget(settings, body.over_budget)
            helper = assistant(settings)
            messages, intent = take_turn(helper, session, body.text, body.intent, body.files,
                                         body.model, proposer(proj, helper))  # fmt: skip
            save_session(settings, session)
            return {"messages": messages, "intent": intent}

    return app
