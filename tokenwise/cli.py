"""Command line: the app, its shared helpers and the Agent commands (init, map, ask, apply,
undo, report, cache). The other commands are in cli_bench (benchmark), cli_health (doctor,
scan, findings, fix, risk, calibrate) and cli_server (serve, server ...)."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from . import llm as llm_module
from .config import CONFIG_FILE, STATE_DIR, Settings, config_template, load_settings
from .editor import Change, EditError, apply_changes, committed_change, resolve, undo_last
from .ledger import Ledger, ledger_path
from .llm import LLM, CachedLLM, ConfigError, LLMError, cache_path
from .pipeline import Result, run
from .pricing import format_usd, reply_cost
from .repo_map import estimate_tokens, fit_map, list_source_files, source_tokens
from .services.changes import budget
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


def _fail(message: str, as_json: bool = False) -> NoReturn:
    if as_json:
        print(json.dumps({"ok": False, "error": message}))
    else:
        console.print(f"[red]{escape(message)}[/]")  # messages may hold [brackets]
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
    use_openrouter: bool = typer.Option(
        False, "--openrouter", help="Use OpenRouter: one key for DeepSeek, Qwen, Llama, Claude..."
    ),
) -> None:
    """Create tokenwise.toml and .env, and keep secrets out of git."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    provider = "ollama" if local else "openrouter" if use_openrouter else "anthropic"
    config = root / CONFIG_FILE
    if not config.exists():
        config.write_text(config_template(provider), "utf-8")
        console.print(f"Created {CONFIG_FILE}" + {"ollama": " for a local model",
                      "openrouter": " for OpenRouter"}.get(provider, ""))  # fmt: skip
    if provider != "ollama":
        _ask_key(root, *{"openrouter": ("OPENROUTER_API_KEY", "OpenRouter API key")}.get(
            provider, ("ANTHROPIC_API_KEY", "Anthropic API key")))  # fmt: skip

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


def _ask_key(root: Path, name: str, label: str) -> None:
    env = root / ".env"
    has_key = env.exists() and f"{name}=" in env.read_text("utf-8").replace(" ", "")
    if not has_key and not os.getenv(name):
        key = typer.prompt(label, hide_input=True, default="", show_default=False)
        if key:
            with env.open("a", encoding="utf-8") as f:
                f.write(f"{name}={key.strip()}\n")
            console.print("Saved the key to .env")


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
    state = budget(settings, methods=None)
    if state.used_up and not over_budget:
        _fail(
            f"Weekly budget used up: {format_usd(state.spent)} of {format_usd(state.limit)} "
            "in the last 7 days. Raise budget_usd_week in tokenwise.toml or pass --over-budget.",
            as_json,
        )
    settings.cache = settings.cache and not no_cache
    settings.check = "" if no_check else (check or settings.check)
    result = _run_request(request, settings, model, as_json)
    run_id = ledger.record(result)
    spent = state.spent + result.cost
    if as_json:
        _print_json(settings, run_id, result, spent)
    _show_result(result)
    if result.changes and result.check_passed is False:
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
    _show_summary(result, spent, state.limit)
    if dry_run:
        _save_pending(settings, run_id, result)
        console.print("Not applied. Apply later with: tokenwise apply")
        return
    if yes or Confirm.ask("Apply this change?", default=True):
        _apply(settings, ledger, run_id, result.changes)
        console.print("[green]Applied.[/] Undo with: tokenwise undo")
    else:
        console.print("Nothing changed.")


def _run_request(request: str, settings, model: str, as_json: bool) -> Result:
    try:
        llm = make_llm(settings)
    except ConfigError as exc:
        _fail(str(exc), as_json)
    try:
        if as_json:
            return run(request, settings, llm, model)
        with console.status("Choosing files and writing the change..."):
            return run(request, settings, llm, model)
    except LLMError as exc:
        _fail(str(exc), as_json)
    except Exception as exc:  # the editor shows this to the user, so never crash silently
        if not as_json:
            raise
        _fail(f"{type(exc).__name__}: {exc}", as_json=True)
    raise AssertionError("unreachable")  # _fail always exits


def _print_json(settings, run_id: int, result: Result, spent: float) -> None:
    # a failed check still leaves the proposal: the user may choose to apply it anyway
    if result.ok or (result.changes and result.check_passed is False):
        _save_pending(settings, run_id, result)
    budget_info = {"budget_usd_week": settings.budget_usd_week, "spent_7d_usd": round(spent, 6)}
    print(json.dumps({"run_id": run_id, **result.to_dict(), **budget_info}))
    raise typer.Exit(0 if result.ok else 1)


def _show_result(result: Result) -> None:
    if result.files:
        console.print(f"Files: [cyan]{', '.join(result.files)}[/]")
    if result.models:
        console.print(f"Difficulty: [cyan]{result.level}[/] → {' → '.join(result.models)}")
        why = [f"{s['signal']} {s['value']} ({s['points']:+d})" for s in result.route
               if s["points"]]  # fmt: skip
        if why:
            console.print(f"[dim]Why: {'; '.join(why)}[/]")
    for change in result.changes:
        console.print(Syntax(change.diff, "diff", theme="ansi_dark", background_color="default"))


def _show_summary(result: Result, spent: float, limit: float) -> None:
    if result.check_passed:
        console.print(f"[green]Check passed:[/] {result.check}")
    console.print(_usage_table(result))
    added, removed = result.lines
    console.print(
        f"{len(result.changes)} file(s) · [green]+{added}[/] [red]-{removed}[/] lines · "
        f"by {result.model}"
    )
    if limit:
        color = "yellow" if spent >= 0.8 * limit else "dim"
        console.print(f"[{color}]{format_usd(spent)} of {format_usd(limit)} weekly budget used[/]")


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
    settings = load_settings(root)
    restored = undo_last(settings)
    if as_json:
        print(json.dumps({"ok": bool(restored), "restored": restored}))
    elif restored:
        console.print(f"[green]Restored:[/] {', '.join(restored)}")
    elif committed_change(settings):
        sha = committed_change(settings)
        console.print(f"The last change was committed ({sha}); use: git revert {sha}")
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


# The other commands live in their own modules; importing them registers them on `app`.
from . import cli_bench, cli_health, cli_server  # noqa: E402, F401
