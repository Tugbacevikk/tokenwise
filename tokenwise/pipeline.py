"""One request from start to finish: map → choose files → route → edit. No printing, no I/O
beyond reading the project, so the CLI, the JSON mode and the benchmark all share it."""

from __future__ import annotations

from dataclasses import dataclass, field

from .checker import make_verifier
from .config import Settings
from .editor import Change, propose
from .llm import LLM, Reply, Usage
from .pricing import reply_cost
from .repo_map import build_map, list_source_files
from .router import NAMES, difficulty, ladder
from .selector import select_files


@dataclass
class Result:
    request: str
    files: list[str] = field(default_factory=list)
    level: str = ""
    models: list[str] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    model: str = ""
    error: str | None = None
    prices: dict = field(default_factory=dict)
    check: str = ""
    check_passed: bool | None = None
    check_output: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.changes)

    @property
    def usage(self) -> Usage:
        total = Usage()
        for reply in self.replies:
            total = total + reply.usage
        return total

    @property
    def cost(self) -> float:
        return sum(reply_cost(r, self.prices) for r in self.replies)

    @property
    def lines(self) -> tuple[int, int]:
        added = removed = 0
        for change in self.changes:
            a, r = change.stats
            added, removed = added + a, removed + r
        return added, removed

    def to_dict(self) -> dict:
        u = self.usage
        added, removed = self.lines
        return {
            "request": self.request,
            "ok": self.ok,
            "error": self.error,
            "files": self.files,
            "difficulty": self.level,
            "models": self.models,
            "model": self.model,
            "changes": [
                {
                    "path": c.path,
                    "old": c.old,
                    "new": c.new,
                    "diff": c.diff,
                    "added": c.stats[0],
                    "removed": c.stats[1],
                }
                for c in self.changes
            ],
            "lines": {"added": added, "removed": removed},
            "check": {
                "command": self.check,
                "passed": self.check_passed,
                "output": self.check_output,
            },
            "usage": {
                "input": u.input_tokens,
                "output": u.output_tokens,
                "cache_read": u.cache_read_tokens,
                "cache_write": u.cache_write_tokens,
            },
            "cost_usd": round(self.cost, 6),
            "calls": [
                {
                    "role": r.role,
                    "model": r.model,
                    "cached": r.cached,
                    "input": r.usage.input_tokens,
                    "output": r.usage.output_tokens,
                    "cost_usd": round(reply_cost(r, self.prices), 6),
                }
                for r in self.replies
            ],
        }


def run(request: str, settings: Settings, llm: LLM, model: str = "") -> Result:
    result = Result(request=request, prices=settings.prices, check=settings.check)
    all_files = list_source_files(settings)
    repo_map = build_map(settings, all_files)

    chosen, selector_reply = select_files(request, repo_map, settings, llm)
    result.replies.append(selector_reply)
    result.files = chosen
    if not chosen:
        result.error = "the selector found no relevant files"
        return result

    if model:
        result.level, result.models = "forced", [model]
    else:
        level = difficulty(request, settings.root, chosen)
        result.level, result.models = NAMES[level], ladder(settings.models, level)

    verify = make_verifier(settings, settings.check) if settings.check else None
    proposal = propose(request, chosen, settings, llm, all_files, result.models, verify)
    result.replies += proposal.replies
    result.check_passed, result.check_output = proposal.check_passed, proposal.check_output
    result.files = proposal.files_sent
    result.changes = proposal.changes
    result.model = proposal.model
    if proposal.error:
        result.error = proposal.error
    elif not proposal.changes:
        result.error = "the edit changed nothing"
    return result
