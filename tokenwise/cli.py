"""Command line: tokenwise init | map | ask | apply | undo | report | cache | benchmark |
scan | findings | fix | risk | serve."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from . import llm as llm_module
from .config import CONFIG_FILE, STATE_DIR, Settings, config_template, load_settings
from .editor import Change, EditError, apply_changes, resolve, undo_last
from .ledger import Ledger, ledger_path
from .llm import LLM, CachedLLM, ConfigError, ContextTooLongError, LLMError, cache_path
from .pipeline import Result, run
from .pricing import format_usd, reply_cost
from .repo_map import estimate_tokens, fit_map, list_source_files, source_tokens
from .textio import read_source

app = typer.Typer(
    help="tokenwise: get AI-written code with the fewest tokens and the fewest lines.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

RootOption = typer.Option(Path("."), "--root", "-r", help="Project folder.")
PENDING_FILE = "pending.json"


def make_llm(settings: Settings) -> LLM:
    """Replaced in tests with a fake model."""
    return llm_module.make_llm(settings)


def _fail(message: str, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": message}))
    else:
        console.print(f"[red]{message}[/]")
    raise typer.Exit(1)


def _version(value: bool) -> None:
    if value:
        console.print(f"tokenwise {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True),
) -> None:
    pass


@app.command()
def init(
    root: Path = RootOption,
    local: bool = typer.Option(
        False, "--local", help="Use a local model server (Ollama) instead of the Claude API."
    ),
) -> None:
    """Create tokenwise.toml and .env, and keep secrets out of git."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = root / CONFIG_FILE
    if not config.exists():
        config.write_text(config_template("ollama" if local else "anthropic"), "utf-8")
        console.print(f"Created {CONFIG_FILE}" + (" for a local model" if local else ""))

    if not local:
        env = root / ".env"
        has_key = env.exists() and "ANTHROPIC_API_KEY=" in env.read_text("utf-8").replace(" ", "")
        if not has_key and not os.getenv("ANTHROPIC_API_KEY"):
            key = typer.prompt("Anthropic API key", hide_input=True, default="", show_default=False)
            if key:
                with env.open("a", encoding="utf-8") as f:
                    f.write(f"ANTHROPIC_API_KEY={key.strip()}\n")
                console.print("Saved the key to .env")

    gitignore = root / ".gitignore"
    existing = gitignore.read_text("utf-8").splitlines() if gitignore.exists() else []
    missing = [line for line in (".env", f"{STATE_DIR}/") if line not in existing]
    if missing:
        with gitignore.open("a", encoding="utf-8") as f:
            if existing and existing[-1].strip():
                f.write("\n")
            f.write("\n".join(missing) + "\n")
        console.print(f"Added {', '.join(missing)} to .gitignore")
    if local:
        console.print("Start Ollama and pull a model, e.g.: ollama pull qwen2.5-coder:7b")
    console.print("[green]Ready.[/] Try: tokenwise map")


@app.command("map")
def map_cmd(
    root: Path = RootOption,
    show: bool = typer.Option(True, "--show/--quiet", help="Print the map itself."),
) -> None:
    """Show the project map and how much smaller it is than the full source."""
    settings = load_settings(root)
    files = list_source_files(settings)
    repo_map, level = fit_map(settings, files)
    if show:
        console.print(repo_map, markup=False, highlight=False)
    full, small = source_tokens(settings, files), estimate_tokens(repo_map)
    ratio = f" ({100 * small / full:.1f}%)" if full else ""
    shortened = f" · shortened to fit map_max_tokens: {level}" if level != "full" else ""
    console.print(
        f"\n[bold]{len(files)}[/] files · full source ≈ {full:,} tokens · "
        f"map ≈ {small:,} tokens{ratio}{shortened}"
    )


def _usage_table(result: Result) -> Table:
    table = Table(title="Token usage", show_edge=False)
    for column in ("step", "model", "input", "cache read", "output", "cost"):
        table.add_column(column, justify="left" if column in ("step", "model") else "right")
    for reply in result.replies:
        u = reply.usage
        table.add_row(
            reply.role + (" [dim](cached)[/]" if reply.cached else ""),
            reply.model,
            f"{u.input_tokens:,}",
            f"{u.cache_read_tokens:,}",
            f"{u.output_tokens:,}",
            format_usd(reply_cost(reply, result.prices)),
        )
    total = result.usage
    table.add_row(
        "[bold]total[/]",
        "",
        f"[bold]{total.input_tokens:,}[/]",
        f"[bold]{total.cache_read_tokens:,}[/]",
        f"[bold]{total.output_tokens:,}[/]",
        f"[bold]{format_usd(result.cost)}[/]",
    )
    return table


def _save_pending(settings: Settings, run_id: int, result: Result) -> None:
    settings.state_dir.mkdir(exist_ok=True)
    data = {
        "run_id": run_id,
        "changes": [{"path": c.path, "old": c.old, "new": c.new} for c in result.changes],
    }
    (settings.state_dir / PENDING_FILE).write_text(json.dumps(data, ensure_ascii=False), "utf-8")


def _apply(settings: Settings, ledger: Ledger, run_id: int, changes: list[Change]) -> None:
    apply_changes(settings, changes)
    ledger.mark_applied(run_id)
    (settings.state_dir / PENDING_FILE).unlink(missing_ok=True)


@app.command()
def ask(
    request: str = typer.Argument(..., help="What to change, in plain words."),
    root: Path = RootOption,
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without asking."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only show the change."),
    model: str = typer.Option("", "--model", "-m", help="Force one editor model, no climbing."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Always ask the model."),
    as_json: bool = typer.Option(
        False, "--json", help="Print the proposal as JSON and keep it for `tokenwise apply`."
    ),
    check: str = typer.Option(
        "", "--check", help='Command that must pass first, e.g. "pytest -q". Overrides the config.'
    ),
    no_check: bool = typer.Option(False, "--no-check", help="Skip the configured check."),
    over_budget: bool = typer.Option(
        False, "--over-budget", help="Run even if the weekly budget is used up."
    ),
) -> None:
    """Make a code change with the least context and the fewest lines."""
    settings = load_settings(root)
    ledger = Ledger(ledger_path(settings))
    spent = ledger.summary(7)["cost_usd"]
    budget = settings.budget_usd_week
    if budget and spent >= budget and not over_budget:
        _fail(
            f"Weekly budget used up: {format_usd(spent)} of {format_usd(budget)} "
            "in the last 7 days. Raise budget_usd_week in tokenwise.toml or pass --over-budget.",
            as_json,
        )
    if no_cache:
        settings.cache = False
    if check:
        settings.check = check
    if no_check:
        settings.check = ""
    try:
        llm = make_llm(settings)
    except ConfigError as exc:
        _fail(str(exc), as_json)

    try:
        if as_json:
            result = run(request, settings, llm, model)
        else:
            with console.status("Choosing files and writing the change..."):
                result = run(request, settings, llm, model)
    except LLMError as exc:
        _fail(str(exc), as_json)
    except Exception as exc:  # the editor shows this to the user, so never crash silently
        if not as_json:
            raise
        _fail(f"{type(exc).__name__}: {exc}", as_json=True)

    run_id = ledger.record(result)
    budget_note = ""
    if budget:
        spent += result.cost
        budget_note = f"{format_usd(spent)} of {format_usd(budget)} weekly budget used"

    if as_json:
        if result.ok:
            _save_pending(settings, run_id, result)
        if result.changes and result.check_passed is False:
            _save_pending(settings, run_id, result)  # the user may still choose to apply it
        budget_info = {"budget_usd_week": budget, "spent_7d_usd": round(spent, 6)}
        print(json.dumps({"run_id": run_id, **result.to_dict(), **budget_info}))
        raise typer.Exit(0 if result.ok else 1)

    if result.files:
        console.print(f"Files: [cyan]{', '.join(result.files)}[/]")
    if result.models:
        console.print(f"Difficulty: [cyan]{result.level}[/] → {' → '.join(result.models)}")
    if result.changes and result.check_passed is False:
        for change in result.changes:
            console.print(
                Syntax(change.diff, "diff", theme="ansi_dark", background_color="default")
            )
        console.print(f"[red]Check failed:[/] {result.check}")
        console.print(result.check_output, markup=False, highlight=False)
        console.print(_usage_table(result))
        _save_pending(settings, run_id, result)
        console.print("Not applied. Apply anyway with: tokenwise apply")
        raise typer.Exit(1)
    if not result.ok:
        console.print(f"[red]No change produced:[/] {result.error}")
        console.print(_usage_table(result))
        raise typer.Exit(1)

    for change in result.changes:
        console.print(Syntax(change.diff, "diff", theme="ansi_dark", background_color="default"))
    if result.check_passed:
        console.print(f"[green]Check passed:[/] {result.check}")
    console.print(_usage_table(result))
    added, removed = result.lines
    console.print(
        f"{len(result.changes)} file(s) · [green]+{added}[/] [red]-{removed}[/] lines · "
        f"by {result.model}"
    )
    if budget_note:
        color = "yellow" if spent >= 0.8 * budget else "dim"
        console.print(f"[{color}]{budget_note}[/]")

    if dry_run:
        _save_pending(settings, run_id, result)
        console.print("Not applied. Apply later with: tokenwise apply")
        return
    if yes or Confirm.ask("Apply this change?", default=True):
        _apply(settings, ledger, run_id, result.changes)
        console.print("[green]Applied.[/] Undo with: tokenwise undo")
    else:
        console.print("Nothing changed.")


@app.command()
def apply(
    root: Path = RootOption,
    as_json: bool = typer.Option(False, "--json", help="Print the outcome as JSON."),
) -> None:
    """Apply the last proposal made with --json or --dry-run."""
    settings = load_settings(root)
    pending = settings.state_dir / PENDING_FILE
    if not pending.exists():
        _fail("Nothing to apply.", as_json)
    data = json.loads(pending.read_text("utf-8"))
    changes = [Change(c["path"], c["old"], c["new"]) for c in data["changes"]]
    for change in changes:
        try:
            path = resolve(settings.root, change.path)
        except EditError as exc:
            _fail(str(exc), as_json)
        current = read_source(path, strict=False) if path.exists() else None
        if current != change.old:
            _fail(f"{change.path} changed since the proposal; ask again.", as_json)
    _apply(settings, Ledger(ledger_path(settings)), data["run_id"], changes)
    paths = [c.path for c in changes]
    if as_json:
        print(json.dumps({"ok": True, "applied": paths}))
    else:
        console.print(f"[green]Applied:[/] {', '.join(paths)}. Undo with: tokenwise undo")


@app.command()
def undo(
    root: Path = RootOption,
    as_json: bool = typer.Option(False, "--json", help="Print the outcome as JSON."),
) -> None:
    """Undo the last applied change."""
    restored = undo_last(load_settings(root))
    if as_json:
        print(json.dumps({"ok": bool(restored), "restored": restored}))
    elif restored:
        console.print(f"[green]Restored:[/] {', '.join(restored)}")
    else:
        console.print("Nothing to undo.")


def _summary_table(columns: list[tuple[str, dict]]) -> Table:
    table = Table(title="tokenwise report", show_edge=False)
    table.add_column("")
    for name, _ in columns:
        table.add_column(name, justify="right")
    rows = [
        ("requests", lambda s: f"{s['runs']:,}"),
        ("succeeded", lambda s: f"{s['ok']:,}"),
        ("applied", lambda s: f"{s['applied']:,}"),
        ("model calls", lambda s: f"{s['calls']:,}"),
        ("cache hit rate", lambda s: f"{100 * s['cache_hit_rate']:.0f}%"),
        ("input tokens", lambda s: f"{s['input_tokens']:,}"),
        ("cache read tokens", lambda s: f"{s['cache_read_tokens']:,}"),
        ("output tokens", lambda s: f"{s['output_tokens']:,}"),
        ("lines +/-", lambda s: f"+{s['lines_added']:,} / -{s['lines_removed']:,}"),
        ("total cost", lambda s: format_usd(s["cost_usd"])),
        ("avg cost per request", lambda s: format_usd(s["avg_cost_per_run"])),
    ]
    for label, fmt in rows:
        table.add_row(label, *(fmt(s) for _, s in columns))
    return table


@app.command()
def report(
    root: Path = RootOption,
    days: int = typer.Option(7, "--days", "-d", help="Length of the recent window."),
    csv_path: Path | None = typer.Option(None, "--csv", help="Also write every request to CSV."),
    as_json: bool = typer.Option(False, "--json", help="Print the numbers as JSON."),
) -> None:
    """Show what tokenwise has spent: recent window and all time."""
    settings = load_settings(root)
    ledger = Ledger(ledger_path(settings))
    recent, total = ledger.summary(days), ledger.summary(None)
    week = recent if days == 7 else ledger.summary(7)
    budget = {"usd_week": settings.budget_usd_week, "spent_7d_usd": week["cost_usd"]}

    if csv_path:
        rows = ledger.rows()
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["id"])
            writer.writeheader()
            writer.writerows(rows)

    if as_json:
        print(json.dumps({"recent": recent, "all_time": total, "budget": budget}))
        return
    if not total["runs"]:
        console.print('No requests recorded yet. Try: tokenwise ask "..."')
        return

    console.print(_summary_table([(f"last {days} days", recent), ("all time", total)]))
    if settings.budget_usd_week:
        share = week["cost_usd"] / settings.budget_usd_week
        color = "red" if share >= 1 else "yellow" if share >= 0.8 else "green"
        console.print(
            f"Weekly budget: [{color}]{format_usd(week['cost_usd'])} of "
            f"{format_usd(settings.budget_usd_week)} ({100 * share:.0f}%)[/]"
        )
    models = Table(title="By model (all time)", show_edge=False)
    for column in ("model", "calls", "cached", "input", "output", "cost"):
        models.add_column(column, justify="left" if column == "model" else "right")
    for m in total["by_model"]:
        models.add_row(
            m["model"],
            f"{m['calls']:,}",
            f"{m['cached']:,}",
            f"{m['input']:,}",
            f"{m['output']:,}",
            format_usd(m["cost"]),
        )
    console.print(models)
    if csv_path:
        console.print(f"Wrote {csv_path}")


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
) -> None:
    """Run every task twice, the usual way and the tokenwise way, and compare cost and results."""
    from .benchmark import METHODS, Outcome, load_tasks, markdown, run_task, totals

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
        llm = make_llm(settings)
    except ConfigError as exc:
        _fail(str(exc))
    ledger = Ledger(ledger_path(settings))
    checks_dir = (tasks_file.parent / "checks").resolve()

    outcomes = []
    for task in tasks:
        for method in chosen:
            with console.status(f"{task.name} · {method}..."):
                try:
                    outcome = run_task(task, method, settings, llm, checks_dir, model)
                except ContextTooLongError as exc:  # this task is too big; the others may fit
                    failed = Result(request=task.request, error=f"context too long: {exc}")
                    outcome = Outcome(task, method, failed, False if task.check else None, 0.0)
                except LLMError as exc:  # server down, unknown model: every task would fail
                    _fail(f"{task.name} · {method}: {exc}")
            ledger.record(outcome.result, method=method)
            skipped = f" [yellow]check skipped: {outcome.check_output}[/]" if task.check else ""
            mark = {None: skipped, True: " [green]check passed[/]", False: " [red]check failed[/]"}
            status = "ok" if outcome.result.ok else f"[red]{outcome.result.error}[/]"
            console.print(
                f"{task.name} · {method}: {status} · {format_usd(outcome.result.cost)}"
                f"{mark[outcome.passed]}"
            )
            if outcome.passed is False and outcome.result.ok:
                for change in outcome.result.changes:
                    console.print(change.diff, markup=False, highlight=False)
                tail = "\n".join(outcome.check_output.splitlines()[-15:])
                console.print(f"[dim]{tail}[/]" if tail else "[dim](check printed nothing)[/]")
            outcomes.append(outcome)

    report_md = markdown(outcomes)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report_md, "utf-8")
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
    console.print(table)
    console.print(f"Wrote {out}")


@app.command()
def doctor(root: Path = RootOption) -> None:
    """Check the setup: config, model server, and that the configured models exist."""
    settings = load_settings(root)
    config = settings.root / CONFIG_FILE
    console.print(f"Config:   {config if config.exists() else 'no tokenwise.toml, using defaults'}")
    console.print(
        f"Provider: {settings.provider}" + (f" ({settings.base_url})" if settings.base_url else "")
    )
    console.print(f"Models:   {' → '.join(settings.models)}")
    files = list_source_files(settings)
    repo_map, level = fit_map(settings, files)
    map_tokens = estimate_tokens(repo_map)
    console.print(f"Project:  {len(files)} source files, map ≈ {map_tokens:,} tokens ({level})")
    try:
        backend = llm_module.make_backend(settings)
    except ConfigError as exc:
        _fail(str(exc))
    if not hasattr(backend, "list_models"):
        console.print("[green]API key found.[/] (Models are checked on the first request.)")
        return
    try:
        available = backend.list_models()
    except LLMError as exc:
        _fail(str(exc))
    console.print(f"Server:   reachable, {len(available)} model(s): {', '.join(available) or '-'}")
    missing = [m for m in settings.models if m not in available and f"{m}:latest" not in available]
    for model in settings.models:
        mark = "[red]missing[/]" if model in missing else "[green]ok[/]"
        console.print(f"  {model}: {mark}")
    if hasattr(backend, "context_for"):
        try:
            backend.context_for("x" * (map_tokens * 4), "", 400)
        except LLMError as exc:
            console.print(f"[yellow]The selector prompt for this project is too big:[/] {exc}")
    if missing:
        hint = "ollama pull " + missing[0] if settings.provider == "ollama" else "fix `models`"
        _fail(f"{len(missing)} model(s) missing. Download them ({hint}) or change `models`.")
    console.print("[green]All good.[/]")


SEVERITY_STYLE = {"high": "red", "medium": "yellow", "low": "dim"}


@app.command()
def scan(
    root: Path = RootOption,
    tools: str = typer.Option(
        "", "--tools", help="Extra sources, comma separated: ruff, bandit, coverage."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """RepoDoctor: analyze the project, track findings over time and score its health."""
    from .health import scan as run_scan

    settings = load_settings(root)
    chosen = tuple(t.strip() for t in tools.split(",") if t.strip()) or None
    if as_json:
        result = run_scan(settings, tools=chosen)
        print(json.dumps(result.__dict__))
        return
    with console.status("Scanning..."):
        result = run_scan(settings, tools=chosen)
    table = Table(title=f"Health: {result.scores['overall']}/100")
    table.add_column("Category")
    table.add_column("Score", justify="right")
    for key, value in result.scores.items():
        if key != "overall":
            table.add_row(key, str(value))
    console.print(table)
    console.print(
        f"{result.files} files · {result.open} open · [red]{result.new} new[/] · "
        f"[green]{result.resolved} resolved[/] · {result.reopened} reopened · {result.seconds}s"
    )
    if result.tools:
        console.print(f"Tools: {', '.join(result.tools)}")
    for tool, why in result.skipped_tools.items():
        console.print(f"[yellow]Skipped {tool}:[/] {why}")
    console.print("See them with: tokenwise findings · weekly report: tokenwise findings --report")


@app.command()
def findings(
    root: Path = RootOption,
    report_md: bool = typer.Option(False, "--report", help="Print the weekly Markdown report."),
    days: int = typer.Option(7, "--days", help="Report period in days."),
    status: str = typer.Option("open", "--status", help="open, resolved, ignored or all."),
    ignore: int = typer.Option(0, "--ignore", help="Mark this finding as ignored."),
    reopen: int = typer.Option(0, "--reopen", help="Open an ignored finding again."),
    limit: int = typer.Option(30, "--limit", "-n"),
    stats: bool = typer.Option(
        False, "--stats", help="Per rule: findings resolved vs ignored (false alarm rate)."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """RepoDoctor: list findings by priority (severity x how often the file changes and breaks)."""
    from .health import HealthStore, health_path, ranked, weekly_report
    from .history import load_history

    settings = load_settings(root)
    if report_md:
        print(weekly_report(settings, days=days), end="")
        return
    if stats:
        with HealthStore(health_path(settings)) as store:
            rules = store.rule_stats()
        if as_json:
            print(json.dumps(rules, ensure_ascii=False))
            return
        if not rules:
            console.print("No findings yet. Run: tokenwise scan")
            return
        table = Table(title="Findings per rule")
        for column in ("Rule", "Category", "Total", "Open", "Resolved", "Ignored", "Reopened",
                       "False alarms"):  # fmt: skip
            table.add_column(column, justify="left" if column in ("Rule", "Category") else "right")
        for r in rules:
            rate = r["false_alarm_rate"]
            table.add_row(r["rule"], r["category"], str(r["total"]), str(r["open"]),
                          str(r["resolved"]), str(r["ignored"]), str(r["reopened"]),
                          "-" if rate is None else f"{rate:.0%}")  # fmt: skip
        console.print(table)
        console.print("[dim]False alarms = ignored / (ignored + resolved). Open ones are not "
                      "counted until a person or a scan decides.[/]")  # fmt: skip
        return
    with HealthStore(health_path(settings)) as store:
        for finding_id, new_status in ((ignore, "ignored"), (reopen, "open")):
            if finding_id:
                if store.set_status(finding_id, new_status) is None:
                    _fail(f"No finding #{finding_id}.")
                console.print(f"#{finding_id} → {new_status}")
                return
        rows = store.findings(None if status == "all" else status)
    rows = ranked(rows, load_history(settings.root))[:limit]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        console.print("No findings. Run: tokenwise scan")
        return
    table = Table()
    for column in ("#", "Severity", "Category", "File", "Finding", "Priority"):
        table.add_column(column)
    for f in rows:
        style = SEVERITY_STYLE[f["severity"]]
        where = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
        table.add_row(str(f["id"]), f"[{style}]{f['severity']}[/]", f["category"], where,
                      f["title"], str(f["priority"]))  # fmt: skip
    console.print(table)
    console.print("Fix one with: tokenwise fix <#>")


@app.command()
def fix(
    finding_id: int = typer.Argument(..., help="Finding number from `tokenwise findings`."),
    root: Path = RootOption,
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without asking."),
    model: str = typer.Option("", "--model", "-m"),
) -> None:
    """Agent: turn a finding into a change, apply it, then re-scan to verify it is gone."""
    from .health import HealthStore, health_path, task_for, verify_fixed

    settings = load_settings(root)
    with HealthStore(health_path(settings)) as store:
        finding = store.get(finding_id)
    if finding is None:
        _fail(f"No finding #{finding_id}. Run: tokenwise scan")
    if finding["status"] != "open":
        _fail(f"Finding #{finding_id} is {finding['status']}.")
    task = task_for(finding)
    console.print(f"[bold]#{finding_id}[/] {finding['title']} → {task['request']}")
    if task["intent"] == "plan":
        console.print("This needs several steps; plan it in the web page: tokenwise serve")
    try:
        ask(request=task["request"], root=root, yes=yes, dry_run=False, model=model,
            no_cache=False, as_json=False, check="", no_check=False, over_budget=False)  # fmt: skip
    except typer.Exit as exc:
        if exc.exit_code:
            raise
    with console.status("Verifying..."):
        gone = verify_fixed(settings, finding)
    if gone:
        console.print(f"[green]Verified:[/] finding #{finding_id} no longer appears.")
    else:
        console.print(
            f"[yellow]Finding #{finding_id} still appears[/] (or the change was not applied)."
        )


@app.command()
def risk(
    root: Path = RootOption,
    base: str = typer.Option("", "--base", "-b", help="Base branch; default origin/main or main."),
    fmt: str = typer.Option("table", "--format", "-f", help="table, markdown or json."),
    fail_above: int = typer.Option(
        0, "--fail-above", help="Exit 1 when the score is above this (0 = never fail)."
    ),
) -> None:
    """ReleaseGuard: how risky is this branch's change, with the evidence and a checklist."""
    from .release import LABELS, GuardError, assess, to_markdown

    settings = load_settings(root)
    try:
        report = assess(settings, base)
    except GuardError as exc:
        _fail(str(exc), fmt == "json")
    if fmt == "json":
        print(json.dumps(report.to_dict(), ensure_ascii=False))
    elif fmt == "markdown":
        print(to_markdown(report), end="")
    else:
        color = {"yüksek": "red", "orta": "yellow"}.get(report.level, "green")
        console.print(
            f"[bold {color}]Risk {report.score}/100 ({report.level})[/] · base {report.base} · "
            f"{len(report.changed)} files · +{report.lines_added} -{report.lines_removed}"
        )
        table = Table()
        for column in ("Signal", "Points", "Confidence", "Evidence"):
            table.add_column(column)
        for s in report.signals:
            table.add_row(LABELS[s.name], f"{s.points:g}", s.confidence, "\n".join(s.reasons))
        console.print(table)
        for item in report.checklist:
            console.print(f"[ ] {item}")
        for s in report.suggestions:
            console.print(f'[dim]Agent:[/] tokenwise ask "{s["request"]}"')
    if fail_above and report.score > fail_above:
        raise typer.Exit(1)


@app.command()
def calibrate(
    root: Path = RootOption,
    sample: int = typer.Option(200, "--sample", help="How many past commits to score."),
    window: int = typer.Option(
        20, "--window", help="A fix within this many later commits marks a commit as a regression."
    ),
    save: bool = typer.Option(False, "--save", help="Use the suggested weights for this project."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """ReleaseGuard: replay past commits as PRs and measure how well each risk signal predicted
    the ones that needed a fix later (AUC). Reads git only; no model is called."""
    from .calibrate import CalibrationError, save_weights
    from .calibrate import calibrate as run_calibration
    from .release import LABELS, WEIGHTS, load_weights

    settings = load_settings(root)
    try:
        if as_json:
            result = run_calibration(settings, sample=sample, window=window)
        else:
            with console.status("Replaying history..."):
                result = run_calibration(settings, sample=sample, window=window)
        path = save_weights(settings, result) if save else None
    except CalibrationError as exc:
        _fail(str(exc), as_json)
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
        return
    console.print(
        f"{len(result.samples)} commits scored · {result.positives} needed a fix within "
        f"{result.window} commits · {result.commits} commits read · {result.seconds}s"
    )
    current = load_weights(settings) if not save else WEIGHTS
    table = Table()
    for column in ("Signal", "AUC", "Weight now", "Suggested"):
        table.add_column(column, justify="left" if column == "Signal" else "right")
    for name in WEIGHTS:
        value = result.auc.get(name)
        suggested = result.suggested.get(name)
        table.add_row(
            LABELS[name],
            "-" if value is None else f"{value:.2f}",
            f"{current[name]:.2f}",
            "-" if suggested is None else f"{suggested:.2f}",
        )
    score_auc = result.auc.get("score")
    table.add_row("[bold]Risk score[/]", "-" if score_auc is None else f"{score_auc:.2f}", "", "")
    console.print(table)
    if not result.suggested:
        console.print(
            "[yellow]No weights suggested:[/] fewer than 10 regressions or no signal beat 0.5."
        )
    elif result.suggested_auc is not None:
        console.print(f"With the suggested weights the score's AUC is {result.suggested_auc:.2f}"
                      " (measured on the same commits, so optimistic).")  # fmt: skip
    if path:
        console.print(f"[green]Saved[/] to {path}; `tokenwise risk` now uses these weights.")
    elif result.suggested:
        console.print("Use them for this project with: tokenwise calibrate --save")


@app.command()
def cache(
    root: Path = RootOption,
    clear: bool = typer.Option(False, "--clear", help="Delete every stored reply."),
) -> None:
    """Show or clear the reply cache."""
    settings = load_settings(root)
    path = cache_path(settings)
    if not path.exists():
        console.print("Cache is empty.")
        return
    store = CachedLLM(inner=None, db_path=path)  # type: ignore[arg-type]
    if clear:
        console.print(f"Removed {store.clear()} cached replies.")
    else:
        console.print(f"{store.count()} cached replies in {path}")


@app.command()
def serve(
    roots: list[Path] = typer.Option(
        [Path(".")], "--root", "-r", help="Project folder. Repeat for several projects."
    ),
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Address to listen on. Other than 127.0.0.1 needs --token."
    ),
    port: int = typer.Option(8765, "--port", "-p", help="Port."),
    token: str = typer.Option(
        "", "--token", help="Password the page must send. Required when --host is not 127.0.0.1."
    ),
) -> None:
    """Open the web interface for one or more projects."""
    if host not in ("127.0.0.1", "localhost") and not token:
        _fail("Listening on another address needs --token, so that only you can change files.")
    try:
        import uvicorn

        from .web import create_app
    except ImportError:
        _fail('The web interface needs extra packages: pip install "tokenwise-coder[web]"')
    link = f"http://{host}:{port}/" + (f"?token={token}" if token else "")
    console.print(f"Arayüz: [bold]{link}[/]   (durdurmak için Ctrl+C)")
    uvicorn.run(create_app(roots, token=token), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()


server_app = typer.Typer(help="The hosted version: GitHub App, dashboard and worker.")
app.add_typer(server_app, name="server")


@server_app.command("web")
def server_web() -> None:
    """Run the web app (webhooks, dashboard, API). Settings come from environment variables;
    see docs/hosting.md."""
    from .server.app import main as web_main

    web_main()


@server_app.command("worker")
def server_worker() -> None:
    """Run a worker: scans, PR risk comments and approved fixes, one job at a time."""
    from .server.worker import main as worker_main

    worker_main()


@server_app.command("migrate")
def server_migrate() -> None:
    """Create or update the PostgreSQL tables."""
    from .server import db
    from .server.config import ServerConfig

    server = ServerConfig.from_env()
    with db.connect(server.database_url) as conn:
        db.migrate(conn)
    console.print("[green]Database ready.[/]")
