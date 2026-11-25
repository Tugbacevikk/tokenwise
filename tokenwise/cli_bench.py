"""Command line: tokenwise benchmark. Registered on the app in cli.py."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.table import Table

from . import cli as base
from .cli import _fail, app, console
from .config import load_settings
from .ledger import Ledger, ledger_path
from .llm import ConfigError, ContextTooLongError, LLMError
from .pipeline import Result
from .pricing import format_usd


def _print_outcome(label: str, task, outcome) -> None:
    skipped = f" [yellow]check skipped: {outcome.check_output}[/]" if task.check else ""
    mark = {None: skipped, True: " [green]check passed[/]", False: " [red]check failed[/]"}
    status = "ok" if outcome.result.ok else f"[red]{outcome.result.error}[/]"
    broke = " [red]suite broke (regression)[/]" if outcome.regression else ""
    console.print(
        f"{label}: {status} · {format_usd(outcome.result.cost)}{mark[outcome.passed]}{broke}"
    )
    if outcome.passed is False and outcome.result.ok:
        for change in outcome.result.changes:
            console.print(change.diff, markup=False, highlight=False)
        tail = "\n".join(outcome.check_output.splitlines()[-15:])
        console.print(f"[dim]{tail}[/]" if tail else "[dim](check printed nothing)[/]")


@app.command()
def benchmark(
    tasks_file: Path = typer.Argument(
        Path("benchmark/tasks.toml"), help="TOML file with [[task]] entries."
    ),
    methods: str = typer.Option(
        "baseline,tokenwise", "--methods", help="Comma-separated: baseline, tokenwise."
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Markdown file for the results (default: results.md next to the tasks).",
    ),
    model: str = typer.Option("", "--model", "-m", help="Force one model for both methods."),
    only: str = typer.Option("", "--only", help="Run only tasks whose name contains this."),
    repeat: int = typer.Option(
        1, "--repeat", min=1, help="Run each task this many times, to see the run-to-run spread."
    ),
    json_out: Path | None = typer.Option(
        None, "--json-out", help="Also save every run and the totals as JSON (for --compare)."
    ),
    compare_with: Path | None = typer.Option(
        None, "--compare", help="A JSON from an earlier --json-out: show what moved since."
    ),
) -> None:
    """Run every task twice, the usual way and the tokenwise way, and compare cost and results."""
    from .benchmark import (
        METHODS,
        Outcome,
        compare,
        load_json,
        load_tasks,
        markdown,
        run_task,
        to_json,
        totals,
    )

    chosen = [m.strip() for m in methods.split(",") if m.strip()]
    if not chosen or any(m not in METHODS for m in chosen):
        _fail(f"--methods must be a comma-separated list of: {', '.join(METHODS)}")
    try:
        tasks = load_tasks(tasks_file)
    except (OSError, ValueError) as exc:
        _fail(str(exc))
    tasks = [t for t in tasks if only in t.name]
    out = out or tasks_file.parent / "results.md"

    settings = load_settings(tasks_file.parent)
    settings.cache = False
    try:
        llm = base.make_llm(settings)
    except ConfigError as exc:
        _fail(str(exc))
    ledger = Ledger(ledger_path(settings))
    checks_dir = (tasks_file.parent / "checks").resolve()

    outcomes = []
    runs = [(n, task, method) for n in range(1, repeat + 1) for task in tasks for method in chosen]
    for number, task, method in runs:
        label = f"{task.name} · {method}" + (f" · run {number}" if repeat > 1 else "")
        with console.status(f"{label}..."):
            try:
                outcome = run_task(task, method, settings, llm, checks_dir, model, number)
            except ContextTooLongError as exc:  # this task is too big; the others may fit
                failed = Result(request=task.request, error=f"context too long: {exc}")
                outcome = Outcome(task, method, failed, False if task.check else None, 0.0,
                                  run=number)  # fmt: skip
            except LLMError as exc:  # server down, unknown model: every task would fail
                _fail(f"{task.name} · {method}: {exc}")
        ledger.record(outcome.result, method=method)
        _print_outcome(label, task, outcome)
        outcomes.append(outcome)

    report_md = markdown(outcomes)
    data = to_json(outcomes)
    if compare_with is not None:
        report_md += "\n## Compared with " + compare_with.name + "\n\n" + compare(
            load_json(compare_with), data)  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report_md, "utf-8")
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    console.print()
    table = Table(title="Benchmark", show_edge=False)
    table.add_column("")
    for method in chosen:
        table.add_column(method, justify="right")
    stats = [totals(outcomes, m) for m in chosen]
    table.add_row("cost", *(format_usd(s["cost"]) for s in stats))
    table.add_row("input tokens", *(f"{s['input']:,}" for s in stats))
    table.add_row("output tokens", *(f"{s['output']:,}" for s in stats))
    table.add_row("lines changed", *(f"{s['lines']:,}" for s in stats))
    table.add_row("checks passed", *(f"{s['passed']}/{s['checked']}" for s in stats))
    table.add_row("regressions", *(f"{s['regressions']}/{s['suite_runs']}" for s in stats))
    table.add_row("median time (s)", *(f"{s['median_seconds']:.1f}" for s in stats))
    console.print(table)
    console.print(f"Wrote {out}")
