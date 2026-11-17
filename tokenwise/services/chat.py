"""One turn of a conversation: decide what the message asks for, then answer it with a
question, an explanation, a plan or a change proposal. The session is changed in place; the
caller saves it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..repo_map import build_map, list_source_files
from . import ServiceError
from .assistant import CONTEXT_CHARS, INTENTS, MODEL_ERRORS, Assistant, read_context
from .records import history_text, message

# propose(request, model, files) -> (run id, Result, the proposal's diff)
Propose = Callable[[str, str, list[str]], tuple]


@dataclass
class Decision:
    text: str  # what is acted on: the message, or an earlier question plus this answer
    intent: str
    ready: bool = True
    questions: list[str] = field(default_factory=list)


def decide(assistant: Assistant, session: dict, user_text: str, intent: str,
           files: list[str]) -> Decision:  # fmt: skip
    """Picks the intent: the user's choice, an answer to our earlier questions, or the router's
    guess. Clears any waiting question."""
    pending = session.get("pending")
    has_proposal = session.get("last_proposal") is not None
    session["pending"] = None
    if pending and intent == "auto":  # this message answers the questions
        return Decision(f"{pending['text']}\nEk bilgi: {user_text}", pending["intent"])
    if intent == "auto":
        history = history_text(session["messages"])
        found, ready, questions = assistant.classify(user_text, history, files, has_proposal)
        if not ready:
            session["pending"] = {"text": user_text, "intent": found}
        return Decision(user_text, found, ready, questions)
    if intent == "refine" and not has_proposal:
        intent = "change"
    return Decision(user_text, intent)


def take_turn(
    assistant: Assistant,
    session: dict,
    user_text: str,
    intent: str = "auto",
    files: list[str] | None = None,
    model: str = "",
    propose: Propose | None = None,
) -> tuple[list[dict], str]:
    """Returns the turn's new messages (the user's first) and the intent acted on."""
    if intent not in ("auto", *INTENTS):
        raise ServiceError(400, "Niyet auto, change, refine, explain ya da plan olmalı.")
    settings = assistant.settings
    files = files or []
    all_files = list_source_files(settings)
    user_text = user_text.strip()
    history = history_text(session["messages"])
    new = [message("user", "text", user_text)]
    acted = intent
    try:
        decision = decide(assistant, session, user_text, intent, all_files)
        acted = decision.intent
        if not decision.ready:
            new.append(message("assistant", "question", "Önce şunları netleştirelim:",
                               questions=decision.questions))  # fmt: skip
        elif decision.intent == "explain":
            new.append(_explain(assistant, session, decision.text, history, files, all_files))
        elif decision.intent == "plan":
            record = assistant.plan(decision.text, model)
            new.append(message("assistant", "plan",
                               f"{len(record['threads'])} iş parçacığına ayırdım.",
                               plan_id=record["id"]))  # fmt: skip
        else:
            new.append(_change(session, decision, files, model, propose))
    except MODEL_ERRORS as exc:
        new.append(message("assistant", "error", str(exc)))
    session["messages"] += new
    return new, acted


def _explain(assistant: Assistant, session: dict, text: str, history: str, files: list[str],
             all_files: list[str]) -> dict:  # fmt: skip
    last = session.get("last_proposal")
    shown = files or (last["files"] if last else [])
    settings = assistant.settings
    if shown:
        context = read_context(settings.root, shown)
    else:
        context = build_map(settings, all_files)
    answer = assistant.explain(text, history, context[: CONTEXT_CHARS * 2])
    return message("assistant", "explain", answer)


def _change(session: dict, decision: Decision, files: list[str], model: str,
            propose: Propose | None) -> dict:  # fmt: skip
    if propose is None:
        raise ServiceError(400, "Bu oturumda değişiklik önerilemiyor.")
    last = session.get("last_proposal")
    request = decision.text
    if decision.intent == "refine" and last is not None:
        request = (
            f"{last['request']}\n\nÖnceki öneri şu değişikliği yaptı:\n"
            f"{last['diff']}\n\nKullanıcının düzeltme isteği: {decision.text}\n"
            "Bu düzeltmeyi uygula; öneriden doğru kalan kısımları koru."
        )
        files = last["files"]
    run_id, result, diff = propose(request, model, files)
    if result.changes:
        session["last_proposal"] = {"run_id": run_id, "request": request,
                                    "files": result.files, "diff": diff[:8000]}  # fmt: skip
        summary = f"{len(result.changes)} dosyada değişiklik önerildi."
    else:
        summary = result.error or "Değişiklik çıkmadı."
    return message("assistant", "proposal", summary, run_id=run_id,
                   data={"run_id": run_id, **result.to_dict()})  # fmt: skip
