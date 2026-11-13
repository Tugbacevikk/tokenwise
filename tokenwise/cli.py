"""Command line: tokenwise init | map | ask | apply | undo | report | cache | benchmark."""

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
from .llm import LLM, CachedLLM, ConfigError, LLMError, cache_path
from .pipeline import Result, run
from .pricing import format_usd, reply_cost
from .repo_map import build_map, estimate_tokens, list_source_files, source_tokens
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
    repo_map = build_map(settings, files)
    if show:
        console.print(repo_map, markup=False, highlight=False)
    full, small = source_tokens(settings, files), estimate_tokens(repo_map)
    ratio = f" ({100 * small / full:.1f}%)" if full else ""
    console.print(
        f"\n[bold]{len(files)}[/] files · full source ≈ {full:,} tokens · "
        f"map ≈ {small:,} tokens{ratio}"
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
    out: Path = typer.Option(
        Path("benchmark/results.md"), "--out", "-o", help="Markdown file for the results."
    ),
    model: str = typer.Option("", "--model", "-m", help="Force one model for both methods."),
    only: str = typer.Option("", "--only", help="Run only tasks whose name contains this."),
) -> None:
    """Run every task twice, the usual way and the tokenwise way, and compare cost and results."""
    from .benchmark import METHODS, load_tasks, markdown, run_task, totals

    chosen = [m.strip() for m in methods.split(",") if m.strip()]
    if not chosen or any(m not in METHODS for m in chosen):
        _fail(f"--methods must be a comma-separated list of: {', '.join(METHODS)}")
    try:
        tasks = load_tasks(tasks_file)
    except (OSError, ValueError) as exc:
        _fail(str(exc))
    tasks = [t for t in tasks if only in t.name]

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
                except LLMError as exc:
                    _fail(f"{task.name} · {method}: {exc}")
            ledger.record(outcome.result, method=method)
            mark = {None: "", True: " [green]check passed[/]", False: " [red]check failed[/]"}
            status = "ok" if outcome.result.ok else f"[red]{outcome.result.error}[/]"
            console.print(
                f"{task.name} · {method}: {status} · {format_usd(outcome.result.cost)}"
                f"{mark[outcome.passed]}"
            )
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
    map_tokens = estimate_tokens(build_map(settings, files))
    console.print(f"Project:  {len(files)} source files, map ≈ {map_tokens:,} tokens")
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


if __name__ == "__main__":
    app()
