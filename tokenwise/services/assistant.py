"""The model-backed steps beyond a single change: routing a chat message, explaining, planning,
editing a plan and reviewing a proposal. Every call is recorded in the cost ledger."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..config import Settings
from ..ledger import Ledger, ledger_path
from ..llm import LLM, ConfigError, ContextTooLongError, LLMError, Reply
from ..pipeline import Result, run
from ..pricing import reply_cost
from ..prompts import EXPLAIN_SYSTEM, PLAN_EDIT_SYSTEM, PLAN_SYSTEM, REVIEW_SYSTEM, ROUTE_SYSTEM
from ..repo_map import build_map, list_source_files
from ..textio import read_source
from . import ServiceError
from .records import MAX_THREADS, new_id, parse_json, parse_plan, save_plan

INTENTS = ("change", "refine", "explain", "plan")
MODEL_ERRORS = (ConfigError, LLMError, ContextTooLongError)
CONTEXT_CHARS = 8000  # per file, when files are shown to explain a question


def record_reply(settings: Settings, text: str, reply: Reply, method: str = "chat") -> None:
    ledger = Ledger(ledger_path(settings))
    try:
        ledger.record(Result(request=text, replies=[reply], prices=settings.prices), method)
    finally:
        ledger.close()


def read_context(root: Path, files: list[str]) -> str:
    return "\n\n".join(
        f"{f}:\n```\n{read_source(root / f, strict=False)[:CONTEXT_CHARS]}\n```" for f in files[:4]
    )


class Assistant:
    """One project's settings and model. `factory(settings)` builds the model client."""

    def __init__(self, settings: Settings, factory: Callable[[Settings], LLM]) -> None:
        self.settings = settings
        self.factory = factory

    @property
    def cheap_model(self) -> str:
        return self.settings.models[0]

    @property
    def middle_model(self) -> str:
        return self.settings.models[min(1, len(self.settings.models) - 1)]

    def _ask(self, model: str, system: str, prompt: str, max_tokens: int, role: str,
             method: str = "chat", label: str = "") -> Reply:  # fmt: skip
        reply = self.factory(self.settings).complete(
            model=model, system=system, prompt=prompt, max_tokens=max_tokens
        )
        reply.role = role
        record_reply(self.settings, label or prompt, reply, method)
        return reply

    # ------------------------------------------------------------ one change

    def propose(self, request: str, model: str = "", files: list[str] | None = None):
        """Runs the Agent once and records it; returns (run id, result)."""
        result = run(request, self.settings, self.factory(self.settings), model, files or None)
        ledger = Ledger(ledger_path(self.settings))
        try:
            run_id = ledger.record(result)
        finally:
            ledger.close()
        return run_id, result

    # ------------------------------------------------------------ chat steps

    def classify(self, text: str, history: str, files: list[str], has_proposal: bool):
        """What the user wants and whether anything essential is missing:
        (intent, ready, questions)."""
        system = ROUTE_SYSTEM.format(
            history=history or "(yok)", files=", ".join(files[:80]) or "(yok)"
        )
        reply = self._ask(self.cheap_model, system, text, 200, "router")
        data = parse_json(reply.text, "{") or {}
        intent = data.get("intent") if data.get("intent") in INTENTS else "change"
        if intent == "refine" and not has_proposal:
            intent = "change"
        questions = [str(q).strip() for q in data.get("questions") or [] if str(q).strip()][:3]
        ready = data.get("ready") is not False or not questions
        return intent, ready, questions

    def explain(self, text: str, history: str, context: str) -> str:
        system = EXPLAIN_SYSTEM.format(history=history or "(yok)", context=context)
        return self._ask(self.middle_model, system, text, 1200, "explainer").text.strip()

    def review(self, request: str, diff: str) -> dict:
        model = self.middle_model
        reply = self._ask(model, REVIEW_SYSTEM, f"İstek: {request}\n\nDiff:\n{diff[:12000]}",
                          600, "reviewer", label=request)  # fmt: skip
        return {
            "review": reply.text.strip(),
            "model": model,
            "cost_usd": reply_cost(reply, self.settings.prices),
        }

    # ------------------------------------------------------------ plans

    def plan(self, goal: str, model: str = "") -> dict:
        files = list_source_files(self.settings)
        model = model or self.middle_model
        # The planner reads the whole map, so it is the middle rung: smarter than the selector,
        # cheaper than the top model.
        system = PLAN_SYSTEM.format(
            max_threads=MAX_THREADS, repo_map=build_map(self.settings, files)
        )
        reply = self._ask(model, system, goal, 2000, "planner", "plan")
        threads = parse_plan(reply.text, files)
        if not threads:
            raise ServiceError(
                502, "Model geçerli bir iş listesi üretemedi. Hedefi biraz daha ayrıntılı yaz."
            )
        now = datetime.now()
        record = {
            "id": new_id(now),
            "goal": goal,
            "model": model,
            "created": now.isoformat(timespec="seconds"),
            "cost_usd": reply_cost(reply, self.settings.prices),
            "threads": threads,
        }
        save_plan(self.settings, record)
        return record

    def edit_plan(self, record: dict, instruction: str, model: str = "") -> dict:
        files = list_source_files(self.settings)
        current = [
            {k: t[k] for k in ("title", "prompt", "files", "check", "level", "depends_on")}
            for t in record["threads"]
        ]
        system = PLAN_EDIT_SYSTEM.format(
            current=json.dumps(current, ensure_ascii=False, indent=1),
            repo_map=build_map(self.settings, files),
            max_threads=MAX_THREADS,
        )
        reply = self._ask(model or self.middle_model, system, instruction, 2500, "planner")
        threads = parse_plan(reply.text, files)
        if not threads:
            raise ServiceError(502, "Plan düzenlenemedi. Talimatı daha açık yaz.")
        old_status = {t["title"]: t["status"] for t in record["threads"]}
        for t in threads:  # threads kept under the same title keep their status
            t["status"] = old_status.get(t["title"], "pending")
        record["threads"] = threads
        save_plan(self.settings, record)
        return record
