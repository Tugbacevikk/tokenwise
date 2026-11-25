"""Model benchmark on GitHub's machines: the smart and the simple router on the same tasks.

Used by .github/workflows/benchmark.yml; also runs on a laptop with OPENROUTER_API_KEY set:

    python benchmark/model_run.py check
    python benchmark/model_run.py run smart
    python benchmark/model_run.py run simple
    python benchmark/model_run.py summary

Inputs come from the environment: REPEAT, MODELS (comma-separated ladder, empty = the
OpenRouter default), MAX_USD (ceiling per task run), ONLY (task name filter)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "benchmark"
OUT = ROOT / ".model-benchmark"
ROUTERS = ("smart", "simple")
MAX_REPEAT = 10
MAX_USD = 1.0  # per task run; a typo like "5" must not spend $5 a run
_MODEL = re.compile(r"^~?[A-Za-z0-9._:/-]+$")


def inputs() -> dict:
    """The run's inputs, checked; raises ValueError with a readable reason."""
    from tokenwise.openrouter import MODELS

    repeat = int(os.getenv("REPEAT") or 3)
    if not 1 <= repeat <= MAX_REPEAT:
        raise ValueError(f"repeat must be 1-{MAX_REPEAT}")
    models = [m.strip() for m in (os.getenv("MODELS") or "").split(",") if m.strip()]
    models = models or list(MODELS)
    bad = [m for m in models if not _MODEL.match(m)]
    if bad:
        raise ValueError(f"not a model name: {', '.join(bad)}")
    max_usd = float(os.getenv("MAX_USD") or 0.05)
    if not 0 < max_usd <= MAX_USD:
        raise ValueError(f"max_usd_per_request must be above 0 and at most {MAX_USD}")
    only = os.getenv("ONLY") or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]*", only):
        raise ValueError("only: letters, digits, - and _")
    return {"repeat": repeat, "models": models, "max_usd": max_usd, "only": only}


def config(router: str, models: list[str], max_usd: float) -> str:
    listed = ", ".join(json.dumps(m) for m in models)
    return (
        "[tokenwise]\n"
        'provider = "openrouter"\n'
        f"models = [{listed}]\n"
        f'router = "{router}"\n'
        f"max_usd_per_request = {max_usd}\n"
        "cache = false\n"
    )


def run(router: str) -> int:
    args = inputs()
    (BENCH / "tokenwise.toml").write_text(config(router, args["models"], args["max_usd"]), "utf-8")
    # each router starts with no history: the smart router learns from the ledger
    shutil.rmtree(BENCH / ".tokenwise", ignore_errors=True)
    OUT.mkdir(exist_ok=True)
    command = [
        sys.executable, "-m", "tokenwise", "benchmark", str(BENCH / "tasks.toml"),
        "--methods", "tokenwise", "--repeat", str(args["repeat"]),
        "-o", str(OUT / f"{router}.md"), "--json-out", str(OUT / f"{router}.json"),
    ]  # fmt: skip
    if args["only"]:
        command += ["--only", args["only"]]
    if router != ROUTERS[0] and (OUT / f"{ROUTERS[0]}.json").exists():
        command += ["--compare", str(OUT / f"{ROUTERS[0]}.json")]
    return subprocess.run(command, cwd=ROOT).returncode


def _totals(path: Path) -> dict | None:
    if not path.exists():
        return None
    runs = json.loads(path.read_text("utf-8"))["runs"]
    checked = [r for r in runs if r["passed"] is not None]
    return {
        "runs": len(runs),
        "solved": sum(1 for r in checked if r["passed"]),
        "checked": len(checked),
        "regressions": sum(1 for r in runs if r["regression"]),
        "escalated": sum(1 for r in runs if r["escalated"]),
        "cost": sum(r["cost"] for r in runs),
        "starts": sorted({r["level"] for r in runs if r["level"]}),
    }


def summary() -> str:
    try:
        args = inputs()
    except ValueError as exc:
        return f"## Model benchmark\n\nInputs rejected: {exc}\n"
    lines = [
        "## Model benchmark: smart vs simple router",
        "",
        f"Models: {' → '.join(args['models'])} · repeat {args['repeat']} · "
        f"ceiling ${args['max_usd']}/task run",
        "",
        "| Router | Solved | Regressions | Escalations | Start levels | Cost ($) |",
        "| --- | ---: | ---: | ---: | --- | ---: |",
    ]
    total = 0.0
    for router in ROUTERS:
        t = _totals(OUT / f"{router}.json")
        if t is None:
            lines.append(f"| {router} | – | – | – | did not finish | – |")
            continue
        total += t["cost"]
        lines.append(
            f"| {router} | {t['solved']}/{t['checked']} | {t['regressions']} | "
            f"{t['escalated']}/{t['runs']} | {', '.join(t['starts']) or '–'} | {t['cost']:.4f} |"
        )
    lines += ["", f"Total cost: ${total:.4f}", ""]
    for router in ROUTERS:
        report = OUT / f"{router}.md"
        if report.exists():
            lines += [f"<details><summary>{router}: every run</summary>", "",
                      report.read_text("utf-8"), "", "</details>", ""]  # fmt: skip
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if argv[:1] == ["check"]:
        try:
            print(json.dumps(inputs()))
        except ValueError as exc:
            print(f"::error::{exc}")
            return 1
        return 0
    if len(argv) == 2 and argv[0] == "run" and argv[1] in ROUTERS:
        return run(argv[1])
    if argv[:1] == ["summary"]:
        print(summary())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
