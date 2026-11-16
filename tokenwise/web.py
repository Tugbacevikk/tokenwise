"""Local web interface behind `tokenwise serve`. It uses the same pipeline as the CLI.

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

from .checker import check_in_copy
from .config import Settings, load_settings
from .editor import apply_changes, resolve, undo_last
from .health import HealthStore, health_path, ranked, task_for, verify_fixed
from .health import scan as run_scan
from .history import load_history
from .ledger import Ledger, ledger_path
from .llm import ConfigError, ContextTooLongError, LLMError, Reply, Usage, make_llm
from .pipeline import Result, run
from .pricing import format_usd, price_for, reply_cost, usage_cost
from .prompts import (
    EXPLAIN_SYSTEM,
    PLAN_EDIT_SYSTEM,
    PLAN_SYSTEM,
    REVIEW_SYSTEM,
    ROUTE_SYSTEM,
)
from .release import GuardError, assess
from .repo_map import build_map, estimate_tokens, list_source_files
from .selector import normalize_path
from .textio import read_source

STATIC_PAGE = Path(__file__).parent / "static" / "index.html"
MAX_THREADS = 8
LEVELS = ("easy", "medium", "hard")
THREAD_STATUSES = ("pending", "done", "skipped")
INTENTS = ("change", "refine", "explain", "plan")
ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
MODEL_ERRORS = (ConfigError, LLMError, ContextTooLongError)
INSTRUCTION_TOKENS = 300  # rough size of the editor's instructions, for the estimate
HISTORY_MESSAGES = 6  # how many earlier messages each turn sees
CONTEXT_CHARS = 8000  # per file, when files are shown to explain a question
SPEND_METHODS = ("tokenwise", "plan", "chat")  # ledger methods that count toward the budget


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


def new_id(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def parse_json(text: str, opening: str):
    """The first JSON object ("{") or array ("[") in a reply, or None."""
    pattern = r"\{.*\}" if opening == "{" else r"\[.*\]"
    match = re.search(pattern, text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def parse_plan(text: str, files: list[str]) -> list[dict]:
    """Work threads from the planner's reply. Unknown files are dropped; bad items are skipped."""
    items = parse_json(text, "[")
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
    if not ID_PATTERN.match(plan_id) or not path.exists():
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


def sessions_dir(settings: Settings) -> Path:
    return settings.state_dir / "sessions"


def new_session(title: str) -> dict:
    now = datetime.now()
    return {
        "id": new_id(now),
        "title": (title.strip() or "Yeni oturum")[:60],
        "created": now.isoformat(timespec="seconds"),
        "messages": [],
        "pending": None,  # a question waiting for the user's answer
        "last_proposal": None,  # what "düzelt" refers to
    }


def save_session(settings: Settings, session: dict) -> None:
    directory = sessions_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{session['id']}.json").write_text(
        json.dumps(session, ensure_ascii=False, indent=2), "utf-8"
    )


def load_session(settings: Settings, session_id: str) -> dict:
    path = sessions_dir(settings) / f"{session_id}.json"
    if not ID_PATTERN.match(session_id) or not path.exists():
        raise HTTPException(404, "Bu oturum bulunamadı.")
    return json.loads(path.read_text("utf-8"))


def session_summaries(settings: Settings) -> list[dict]:
    if not sessions_dir(settings).exists():
        return []
    sessions = [json.loads(p.read_text("utf-8")) for p in sessions_dir(settings).glob("*.json")]
    sessions.sort(key=lambda s: s["id"], reverse=True)
    return [
        {"id": s["id"], "title": s["title"], "created": s["created"], "count": len(s["messages"])}
        for s in sessions
    ]


def message(role: str, kind: str, content: str, **extra) -> dict:
    return {
        "role": role,
        "kind": kind,
        "content": content,
        "created": datetime.now().isoformat(timespec="seconds"),
        **extra,
    }


def history_text(messages: list[dict]) -> str:
    recent = messages[-HISTORY_MESSAGES:]
    return "\n".join(f"{m['role']} ({m['kind']}): {m['content'][:300]}" for m in recent)


def read_context(root: Path, files: list[str]) -> str:
    return "\n\n".join(
        f"{f}:\n```\n{read_source(root / f, strict=False)[:CONTEXT_CHARS]}\n```" for f in files[:4]
    )


def record_reply(settings: Settings, text: str, reply: Reply, method: str = "chat") -> None:
    ledger = Ledger(ledger_path(settings))
    try:
        ledger.record(Result(request=text, replies=[reply], prices=settings.prices), method)
    finally:
        ledger.close()


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
            return sum(ledger.summary(7, method=m)["cost_usd"] for m in SPEND_METHODS)
        finally:
            ledger.close()

    def over_budget(settings: Settings) -> tuple[float, float] | None:
        spent = spent_last_week(settings)
        limit = settings.budget_usd_week
        return (spent, limit) if limit and spent >= limit else None

    def cheap_model(settings: Settings) -> str:
        return settings.models[0]

    def middle_model(settings: Settings) -> str:
        return settings.models[min(1, len(settings.models) - 1)]

    # --- the steps a turn can take -------------------------------------------------------

    def propose(proj: Project, settings: Settings, request: str, model: str, files: list[str]):
        """One change proposal. Kept in memory so it can be applied, committed or reviewed."""
        result = run(request, settings, factory(settings), model, files or None)
        ledger = Ledger(ledger_path(settings))
        try:
            run_id = ledger.record(result)
        finally:
            ledger.close()
        if result.changes:
            proj.pending[run_id] = result
            proj.diffs[run_id] = (request, "\n".join(c.diff for c in result.changes))
        return run_id, result

    def classify(settings: Settings, text: str, history: str, files: list[str], has_proposal):
        """Asks the cheapest model what the user wants and whether anything essential is missing."""
        reply = factory(settings).complete(
            model=cheap_model(settings),
            system=ROUTE_SYSTEM.format(
                history=history or "(yok)", files=", ".join(files[:80]) or "(yok)"
            ),
            prompt=text,
            max_tokens=200,
        )
        reply.role = "router"
        record_reply(settings, text, reply)
        data = parse_json(reply.text, "{") or {}
        intent = data.get("intent") if data.get("intent") in INTENTS else "change"
        if intent == "refine" and not has_proposal:
            intent = "change"
        questions = [str(q).strip() for q in data.get("questions") or [] if str(q).strip()][:3]
        ready = data.get("ready") is not False or not questions
        return intent, ready, questions

    def explain(settings: Settings, text: str, history: str, context: str) -> str:
        reply = factory(settings).complete(
            model=middle_model(settings),
            system=EXPLAIN_SYSTEM.format(history=history or "(yok)", context=context),
            prompt=text,
            max_tokens=1200,
        )
        reply.role = "explainer"
        record_reply(settings, text, reply)
        return reply.text.strip()

    def plan_core(settings: Settings, goal: str, model: str) -> dict:
        files = list_source_files(settings)
        model = model or middle_model(settings)
        # The planner reads the whole map, so it is the middle rung: smarter than the selector,
        # cheaper than the top model.
        system = PLAN_SYSTEM.format(max_threads=MAX_THREADS, repo_map=build_map(settings, files))
        reply = factory(settings).complete(model=model, system=system, prompt=goal, max_tokens=2000)
        reply.role = "planner"
        record_reply(settings, goal, reply, "plan")
        threads = parse_plan(reply.text, files)
        if not threads:
            raise HTTPException(
                502, "Model geçerli bir iş listesi üretemedi. Hedefi biraz daha ayrıntılı yaz."
            )
        now = datetime.now()
        record = {
            "id": new_id(now),
            "goal": goal,
            "model": model,
            "created": now.isoformat(timespec="seconds"),
            "cost_usd": reply_cost(reply, settings.prices),
            "threads": threads,
        }
        save_plan(settings, record)
        return record

    def plan_edit_core(settings: Settings, record: dict, instruction: str, model: str) -> dict:
        files = list_source_files(settings)
        current = [
            {k: t[k] for k in ("title", "prompt", "files", "check", "level", "depends_on")}
            for t in record["threads"]
        ]
        reply = factory(settings).complete(
            model=model or middle_model(settings),
            system=PLAN_EDIT_SYSTEM.format(
                current=json.dumps(current, ensure_ascii=False, indent=1),
                repo_map=build_map(settings, files),
                max_threads=MAX_THREADS,
            ),
            prompt=instruction,
            max_tokens=2500,
        )
        reply.role = "planner"
        record_reply(settings, instruction, reply)
        threads = parse_plan(reply.text, files)
        if not threads:
            raise HTTPException(502, "Plan düzenlenemedi. Talimatı daha açık yaz.")
        old_status = {t["title"]: t["status"] for t in record["threads"]}
        for t in threads:  # threads kept under the same title keep their status
            t["status"] = old_status.get(t["title"], "pending")
        record["threads"] = threads
        save_plan(settings, record)
        return record

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
                "spent_usd": spent_last_week(settings),
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
        est = usage_cost(Usage(input_tokens, 0), price_for(cheap_model(settings), settings.prices))
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
                run_id, result = propose(proj, settings, body.request, body.model, body.files)
            except MODEL_ERRORS as exc:
                raise HTTPException(400, str(exc)) from None
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
            check = None
            if settings.check:  # the project's tests, now that the change is in place
                outcome = check_in_copy(settings, [], settings.check)
                check = {
                    "passed": outcome.passed,
                    "skipped": outcome.skipped,
                    "output": outcome.output[-2000:],
                }
            return {"applied": paths, "check": check}

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
            message_text = "tokenwise: " + " ".join(request.split())[:72]
            # Only the paths tokenwise changed go into the commit, nothing else staged.
            for args in (["add", "--", *paths], ["commit", "-m", message_text, "--", *paths]):
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
            return {"commit": sha, "message": message_text}

    @app.post("/api/projects/{pid}/runs/{run_id}/review")
    def review(pid: str, run_id: int) -> dict:
        proj = project(pid)
        with proj.lock:
            found = proj.diffs.get(run_id)
            if found is None:
                raise HTTPException(404, "Bu öneri artık bellekte değil. İsteği yeniden gönder.")
            settings = load_settings(proj.root)
            request, diff = found
            model = middle_model(settings)
            try:
                reply = factory(settings).complete(
                    model=model,
                    system=REVIEW_SYSTEM,
                    prompt=f"İstek: {request}\n\nDiff:\n{diff[:12000]}",
                    max_tokens=600,
                )
            except MODEL_ERRORS as exc:
                raise HTTPException(400, str(exc)) from None
            reply.role = "reviewer"
            record_reply(settings, request, reply)
            return {
                "review": reply.text.strip(),
                "model": model,
                "cost_usd": reply_cost(reply, settings.prices),
            }

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
            try:
                return plan_core(settings, body.goal, body.model)
            except MODEL_ERRORS as exc:
                raise HTTPException(400, str(exc)) from None

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
            try:
                return plan_edit_core(settings, record, body.text, body.model)
            except MODEL_ERRORS as exc:
                raise HTTPException(400, str(exc)) from None

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

    @app.get("/api/projects/{pid}/sessions")
    def sessions(pid: str) -> list[dict]:
        return session_summaries(load_settings(project(pid).root))

    @app.post("/api/projects/{pid}/sessions")
    def start_session(pid: str, body: SessionBody) -> dict:
        proj = project(pid)
        with proj.lock:
            settings = load_settings(proj.root)
            session = new_session(body.title)
            save_session(settings, session)
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
            blocked = over_budget(settings)
            if blocked and not body.over_budget:
                spent, limit = blocked
                raise HTTPException(
                    409, f"Haftalık bütçe doldu: {format_usd(spent)} / {format_usd(limit)}."
                )
            if body.intent not in ("auto", *INTENTS):
                raise HTTPException(400, "Niyet auto, change, refine, explain ya da plan olmalı.")

            all_files = list_source_files(settings)
            user_text = body.text.strip()
            new_messages = [message("user", "text", user_text)]
            history = history_text(session["messages"])
            last = session.get("last_proposal")
            pending = session.get("pending")
            try:
                if pending and body.intent == "auto":  # this message answers the questions
                    text, intent, ready, questions = (
                        f"{pending['text']}\nEk bilgi: {user_text}",
                        pending["intent"],
                        True,
                        [],
                    )
                    session["pending"] = None
                elif body.intent == "auto":
                    text = user_text
                    intent, ready, questions = classify(
                        settings, text, history, all_files, last is not None
                    )
                    session["pending"] = None
                else:
                    text, intent, ready, questions = user_text, body.intent, True, []
                    if intent == "refine" and last is None:
                        intent = "change"
                    session["pending"] = None

                if not ready:
                    session["pending"] = {
                        "text": pending["text"] if pending else user_text,
                        "intent": intent,
                    }
                    new_messages.append(
                        message(
                            "assistant",
                            "question",
                            "Önce şunları netleştirelim:",
                            questions=questions,
                        )
                    )
                elif intent == "explain":
                    files = body.files or (last["files"] if last else [])
                    context = (
                        read_context(proj.root, files) if files else build_map(settings, all_files)
                    )
                    answer = explain(settings, text, history, context[: CONTEXT_CHARS * 2])
                    new_messages.append(message("assistant", "explain", answer))
                elif intent == "plan":
                    record = plan_core(settings, text, body.model)
                    new_messages.append(
                        message(
                            "assistant",
                            "plan",
                            f"{len(record['threads'])} iş parçacığına ayırdım.",
                            plan_id=record["id"],
                        )
                    )
                else:
                    request, files = text, body.files
                    if intent == "refine" and last is not None:
                        request = (
                            f"{last['request']}\n\nÖnceki öneri şu değişikliği yaptı:\n"
                            f"{last['diff']}\n\nKullanıcının düzeltme isteği: {text}\n"
                            "Bu düzeltmeyi uygula; öneriden doğru kalan kısımları koru."
                        )
                        files = last["files"]
                    run_id, result = propose(proj, settings, request, body.model, files)
                    if result.changes:
                        session["last_proposal"] = {
                            "run_id": run_id,
                            "request": request,
                            "files": result.files,
                            "diff": proj.diffs[run_id][1][:8000],
                        }
                        summary = f"{len(result.changes)} dosyada değişiklik önerildi."
                    else:
                        summary = result.error or "Değişiklik çıkmadı."
                    new_messages.append(
                        message(
                            "assistant",
                            "proposal",
                            summary,
                            run_id=run_id,
                            data={"run_id": run_id, **result.to_dict()},
                        )
                    )
            except MODEL_ERRORS as exc:
                new_messages.append(message("assistant", "error", str(exc)))

            session["messages"] += new_messages
            save_session(settings, session)
            return {
                "messages": new_messages,
                "intent": intent if "intent" in dir() else body.intent,
            }

    return app
