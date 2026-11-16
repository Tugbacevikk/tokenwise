"""Local web interface behind `tokenwise serve`. It uses the same pipeline as the CLI.

The server can change files in the projects it serves, so by default it listens on 127.0.0.1
only. Another address needs a token, which every /api request must send in a header.

Endpoints (pid is a project id from GET /api/projects):
  GET  /                                  the page
  GET  /api/projects                      the projects served
  GET  /api/projects/{pid}                files, map size, models, budget, git or not
  POST /api/projects/{pid}/estimate       rough input tokens and cost before running
  POST /api/projects/{pid}/ask            one change request (also used to run a thread)
  POST /api/projects/{pid}/apply          write the change proposed by an earlier ask
  POST /api/projects/{pid}/undo           undo the last applied change
  POST /api/projects/{pid}/commit         git commit of an applied change
  POST /api/projects/{pid}/plan           split a goal into work threads, saved to .tokenwise/plans
  GET  /api/projects/{pid}/plans          saved plans, newest first
  GET  /api/projects/{pid}/plans/{id}     one plan
  PUT  /api/projects/{pid}/plans/{id}/threads/{n}   set a thread's status
  GET  /api/projects/{pid}/plans/{id}/export        the plan as Markdown
"""

import json
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .config import Settings, load_settings
from .editor import apply_changes, resolve, undo_last
from .ledger import Ledger, ledger_path
from .llm import ConfigError, ContextTooLongError, LLMError, Usage, make_llm
from .pipeline import Result, run
from .pricing import format_usd, price_for, reply_cost, usage_cost
from .prompts import PLAN_SYSTEM
from .repo_map import build_map, estimate_tokens, list_source_files
from .selector import normalize_path
from .textio import read_source

STATIC_PAGE = Path(__file__).parent / "static" / "index.html"
MAX_THREADS = 8
LEVELS = ("easy", "medium", "hard")
THREAD_STATUSES = ("pending", "done", "skipped")
PLAN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
MODEL_ERRORS = (ConfigError, LLMError, ContextTooLongError)
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


class StatusBody(BaseModel):
    status: str


@dataclass
class Project:
    id: str
    root: Path
    lock: threading.Lock = field(default_factory=threading.Lock)  # one request at a time
    pending: dict[int, Result] = field(default_factory=dict)  # run id -> proposal to apply
    applied: dict[int, tuple[str, list[str]]] = field(default_factory=dict)  # for commit


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


def parse_plan(text: str, files: list[str]) -> list[dict]:
    """Work threads from the planner's reply. Unknown files are dropped; bad items are skipped."""
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    known = set(files)
    threads: list[dict] = []
    for item in items:
        if len(threads) >= MAX_THREADS:
            break
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not title or not prompt:
            continue
        paths: list[str] = []
        for raw in item.get("files") or []:
            path = normalize_path(str(raw))
            if path in known and path not in paths:
                paths.append(path)
        level = item.get("level") if item.get("level") in LEVELS else "medium"
        deps = [
            d
            for d in item.get("depends_on") or []
            if isinstance(d, int) and not isinstance(d, bool) and 0 <= d < len(threads)
        ]
        threads.append(
            {
                "index": len(threads),
                "title": title,
                "prompt": prompt,
                "files": paths[:3],
                "check": str(item.get("check") or "").strip(),
                "level": level,
                "depends_on": sorted(set(deps)),
                "status": "pending",
            }
        )
    return threads


def plan_dir(settings: Settings) -> Path:
    return settings.state_dir / "plans"


def save_plan(settings: Settings, plan: dict) -> None:
    directory = plan_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{plan['id']}.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), "utf-8"
    )


def load_plan(settings: Settings, plan_id: str) -> dict:
    path = plan_dir(settings) / f"{plan_id}.json"
    if not PLAN_ID.match(plan_id) or not path.exists():
        raise HTTPException(404, "Bu plan bulunamadı.")
    return json.loads(path.read_text("utf-8"))


def plan_summaries(settings: Settings) -> list[dict]:
    if not plan_dir(settings).exists():
        return []
    plans = [json.loads(p.read_text("utf-8")) for p in plan_dir(settings).glob("*.json")]
    plans.sort(key=lambda p: p["id"], reverse=True)
    return [
        {
            "id": p["id"],
            "goal": p["goal"],
            "created": p["created"],
            "total": len(p["threads"]),
            "done": sum(t["status"] == "done" for t in p["threads"]),
        }
        for p in plans
    ]


def plan_markdown(plan: dict) -> str:
    lines = [f"# {plan['goal']}", "", f"{plan['created']} · model: {plan['model']}", ""]
    for t in plan["threads"]:
        mark = {"done": "x", "skipped": "-"}.get(t["status"], " ")
        lines.append(f"## [{mark}] {t['index'] + 1}. {t['title']} ({t['level']})")
        if t["depends_on"]:
            lines.append("Önce: " + ", ".join(str(i + 1) for i in t["depends_on"]))
        if t["files"]:
            lines.append("Dosyalar: " + ", ".join(t["files"]))
        lines += ["", t["prompt"], ""]
        if t["check"]:
            lines += [f"Kontrol: `{t['check']}`", ""]
    return "\n".join(lines)


def is_git_repo(root: Path) -> bool:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
    except OSError:  # git is not installed
        return False
    return done.returncode == 0


def create_app(roots, llm_factory=None, token: str = "") -> FastAPI:
    """The app for one or more projects. `llm_factory(settings)` returns the model client;
    tests pass a fake one. A non-empty `token` must be sent as X-Tokenwise-Token on /api calls."""
    if isinstance(roots, (str, Path)):
        roots = [roots]
    factory = llm_factory or make_llm
    projects = {pid: Project(pid, root) for pid, root in project_ids(list(roots)).items()}
    app = FastAPI(title="tokenwise")

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

    def spent_last_week(settings: Settings) -> float:
        ledger = Ledger(ledger_path(settings))
        try:
            return ledger.summary(7)["cost_usd"] + ledger.summary(7, method="plan")["cost_usd"]
        finally:
            ledger.close()

    def over_budget(settings: Settings) -> tuple[float, float] | None:
        spent = spent_last_week(settings)
        limit = settings.budget_usd_week
        return (spent, limit) if limit and spent >= limit else None

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
                "spent_usd": spent_last_week(settings),
                "limit_usd": settings.budget_usd_week,
            },
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
            blocked = over_budget(settings)
            if blocked and not body.over_budget:
                spent, limit = blocked
                raise HTTPException(
                    409,
                    f"Haftalık bütçe doldu: {format_usd(spent)} / {format_usd(limit)}. "
                    "Yine de çalıştırmak için over_budget seçeneğini kullan.",
                )
            if body.check:
                settings.check = body.check
            try:
                result = run(
                    body.request, settings, factory(settings), body.model, body.files or None
                )
            except MODEL_ERRORS as exc:
                raise HTTPException(400, str(exc)) from None
            ledger = Ledger(ledger_path(settings))
            try:
                run_id = ledger.record(result)
            finally:
                ledger.close()
            if result.changes:
                proj.pending[run_id] = result
            return {"run_id": run_id, **result.to_dict()}

    @app.post("/api/projects/{pid}/apply")
    def apply(pid: str, body: ApplyBody) -> dict:
        proj = project(pid)
        with proj.lock:
            result = proj.pending.get(body.run_id)
            if result is None:
                raise HTTPException(404, "Bu öneri yok ya da zaten uygulandı.")
            settings = load_settings(proj.root)
            # The proposal was made from the files as they were then; refuse if they changed.
            for change in result.changes:
                path = resolve(proj.root, change.path)
                if change.old is None:
                    stale = path.exists()
                else:
                    stale = not path.exists() or read_source(path, strict=False) != change.old
                if stale:
                    raise HTTPException(409, f"{change.path} değişmiş. İsteği yeniden çalıştır.")
            apply_changes(settings, result.changes)
            paths = [c.path for c in result.changes]
            ledger = Ledger(ledger_path(settings))
            try:
                ledger.mark_applied(body.run_id)
            finally:
                ledger.close()
            del proj.pending[body.run_id]
            proj.applied[body.run_id] = (result.request, paths)
            return {"applied": paths}

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
            if not is_git_repo(proj.root):
                raise HTTPException(400, "Proje bir git deposu değil.")
            request, paths = proj.applied[body.run_id]
            message = "tokenwise: " + " ".join(request.split())[:72]
            # Only the paths tokenwise changed go into the commit, nothing else staged.
            for args in (["add", "--", *paths], ["commit", "-m", message, "--", *paths]):
                done = subprocess.run(
                    ["git", "-C", str(proj.root), *args], capture_output=True, text=True
                )
                if done.returncode:
                    raise HTTPException(400, (done.stderr or done.stdout).strip())
            sha = subprocess.run(
                ["git", "-C", str(proj.root), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            del proj.applied[body.run_id]
            return {"commit": sha, "message": message}

    @app.post("/api/projects/{pid}/plan")
    def plan(pid: str, body: PlanBody) -> dict:
        proj = project(pid)
        with proj.lock:
            settings = load_settings(proj.root)
            blocked = over_budget(settings)
            if blocked:
                spent, limit = blocked
                raise HTTPException(
                    409, f"Haftalık bütçe doldu: {format_usd(spent)} / {format_usd(limit)}."
                )
            files = list_source_files(settings)
            # The planner reads the whole map, so it is the middle rung: smarter than the
            # selector, cheaper than the top model. The page can choose another.
            model = body.model or settings.models[min(1, len(settings.models) - 1)]
            system = PLAN_SYSTEM.format(
                max_threads=MAX_THREADS, repo_map=build_map(settings, files)
            )
            try:
                reply = factory(settings).complete(
                    model=model, system=system, prompt=body.goal, max_tokens=2000
                )
            except MODEL_ERRORS as exc:
                raise HTTPException(400, str(exc)) from None
            reply.role = "planner"
            ledger = Ledger(ledger_path(settings))
            try:
                ledger.record(
                    Result(request=body.goal, replies=[reply], prices=settings.prices), "plan"
                )
            finally:
                ledger.close()
            threads = parse_plan(reply.text, files)
            if not threads:
                raise HTTPException(
                    502, "Model geçerli bir iş listesi üretemedi. Hedefi biraz daha ayrıntılı yaz."
                )
            now = datetime.now()
            record = {
                "id": f"{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
                "goal": body.goal,
                "model": model,
                "created": now.isoformat(timespec="seconds"),
                "cost_usd": reply_cost(reply, settings.prices),
                "threads": threads,
            }
            save_plan(settings, record)
            return record

    @app.get("/api/projects/{pid}/plans")
    def plans(pid: str) -> list[dict]:
        return plan_summaries(load_settings(project(pid).root))

    @app.get("/api/projects/{pid}/plans/{plan_id}")
    def get_plan(pid: str, plan_id: str) -> dict:
        return load_plan(load_settings(project(pid).root), plan_id)

    @app.put("/api/projects/{pid}/plans/{plan_id}/threads/{index}")
    def set_status(pid: str, plan_id: str, index: int, body: StatusBody) -> dict:
        proj = project(pid)
        with proj.lock:
            settings = load_settings(proj.root)
            if body.status not in THREAD_STATUSES:
                raise HTTPException(400, "Durum pending, done ya da skipped olmalı.")
            record = load_plan(settings, plan_id)
            if not 0 <= index < len(record["threads"]):
                raise HTTPException(404, "Bu iş parçacığı yok.")
            record["threads"][index]["status"] = body.status
            save_plan(settings, record)
            return record["threads"][index]

    @app.get("/api/projects/{pid}/plans/{plan_id}/export")
    def export(pid: str, plan_id: str) -> PlainTextResponse:
        record = load_plan(load_settings(project(pid).root), plan_id)
        return PlainTextResponse(plan_markdown(record), media_type="text/markdown")

    return app
