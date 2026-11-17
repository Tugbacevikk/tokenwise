"""Plans and chat sessions, kept as JSON files under `.tokenwise/` (one file each), and the
planner's reply parsed into work threads."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from ..config import Settings
from ..selector import normalize_path
from . import ServiceError

MAX_THREADS = 8
LEVELS = ("easy", "medium", "hard")
THREAD_STATUSES = ("pending", "done", "skipped")
ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
HISTORY_MESSAGES = 6  # how many earlier messages each turn sees


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
        raise ServiceError(404, "Bu plan bulunamadı.")
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
        raise ServiceError(404, "Bu oturum bulunamadı.")
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


def set_thread_status(settings: Settings, plan_id: str, index: int, status: str) -> dict:
    if status not in THREAD_STATUSES:
        raise ServiceError(400, "Durum pending, done ya da skipped olmalı.")
    record = load_plan(settings, plan_id)
    if not 0 <= index < len(record["threads"]):
        raise ServiceError(404, "Bu iş parçacığı yok.")
    record["threads"][index]["status"] = status
    save_plan(settings, record)
    return record["threads"][index]
