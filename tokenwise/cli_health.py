"""Command line, RepoDoctor and ReleaseGuard: doctor, scan, findings, fix, risk,
calibrate. Registered on the app in cli.py."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.markup import escape
from rich.table import Table

from . import cli as base
from . import llm as llm_module
from .cli import RootOption, _fail, app, console
from .config import CONFIG_FILE, load_settings
from .llm import ConfigError, LLMError
from .repo_map import estimate_tokens, fit_map, list_source_files


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
    shown = ", ".join(available) if len(available) <= 20 else f"{len(available)} listed"
    console.print(f"Server:   reachable, {len(available)} model(s): {shown or '-'}")
    if getattr(backend, "priced", True) is False:
        console.print("[yellow]Prices could not be read from OpenRouter:[/] costs will show 0 "
                      "and max_usd_per_request cannot hold until they can.")  # fmt: skip
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
        hint = {"ollama": "ollama pull " + missing[0],
                "openrouter": "names as on https://openrouter.ai/models"}.get(
            settings.provider, "fix `models`")  # fmt: skip
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
        base.ask(request=task["request"], root=root, yes=yes, dry_run=False, model=model,
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
        _print_findings(report)
        for item in report.checklist:
            console.print(f"[ ] {item}")
        for suggestion in report.suggestions:
            console.print(f'[dim]Agent:[/] tokenwise ask "{suggestion["request"]}"')
    if fail_above and report.score > fail_above:
        raise typer.Exit(1)


def _print_findings(report) -> None:
    """RepoDoctor findings in the changed files: new and worse ones, else those already there."""
    from .release import finding_mark

    for f in (report.new_findings + report.worse_findings or report.findings)[:8]:
        where = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
        mark = f"[bold]{escape(finding_mark(f))}[/] " if finding_mark(f) else ""
        console.print(f"RepoDoctor: {mark}{f['severity']} {escape(f['title'])} ({where})")


@app.command()
def calibrate(
    root: Path = RootOption,
    sample: int = typer.Option(200, "--sample", help="How many past commits to score."),
    window: int = typer.Option(
        20, "--window", help="A fix within this many later commits marks a commit as a regression."
    ),
    save: bool = typer.Option(False, "--save", help="Use the suggested weights for this project."),
    as_json: bool = typer.Option(False, "--json"),
    samples: bool = typer.Option(
        False, "--samples", help="With --json: every scored commit, its label and the fix."
    ),
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
        print(json.dumps(result.to_dict(samples=samples), ensure_ascii=False))
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
