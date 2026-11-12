"""Command line: tokenwise init | map | ask | undo."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .config import CONFIG_FILE, CONFIG_TEMPLATE, STATE_DIR, load_settings
from .editor import apply_changes, propose, undo_last
from .llm import LLM, AnthropicLLM, Reply, Usage
from .repo_map import build_map, estimate_tokens, list_source_files, source_tokens
from .selector import select_files

app = typer.Typer(
    help="tokenwise: get AI-written code with the fewest tokens and the fewest lines.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

RootOption = typer.Option(Path("."), "--root", "-r", help="Project folder.")


def make_llm() -> LLM:
    """Replaced in tests with a fake model."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        console.print("[red]ANTHROPIC_API_KEY is not set.[/] Run [bold]tokenwise init[/] first.")
        raise typer.Exit(1)
    return AnthropicLLM()


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
def init(root: Path = RootOption) -> None:
    """Create tokenwise.toml and .env, and keep secrets out of git."""
    root = root.resolve()
    config = root / CONFIG_FILE
    if not config.exists():
        config.write_text(CONFIG_TEMPLATE, "utf-8")
        console.print(f"Created {CONFIG_FILE}")

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


def _usage_table(replies: list[Reply]) -> Table:
    table = Table(title="Token usage", show_edge=False)
    for column in ("step", "model", "input", "cache read", "output"):
        table.add_column(column, justify="left" if column in ("step", "model") else "right")
    total = Usage()
    for reply in replies:
        u = reply.usage
        total = total + u
        table.add_row(
            reply.role,
            reply.model,
            f"{u.input_tokens:,}",
            f"{u.cache_read_tokens:,}",
            f"{u.output_tokens:,}",
        )
    table.add_row(
        "[bold]total[/]",
        "",
        f"[bold]{total.input_tokens:,}[/]",
        f"[bold]{total.cache_read_tokens:,}[/]",
        f"[bold]{total.output_tokens:,}[/]",
    )
    return table


@app.command()
def ask(
    request: str = typer.Argument(..., help="What to change, in plain words."),
    root: Path = RootOption,
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without asking."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only show the change."),
) -> None:
    """Make a code change with the least context and the fewest lines."""
    settings = load_settings(root)
    llm = make_llm()
    files = list_source_files(settings)
    repo_map = build_map(settings, files)

    with console.status("Choosing files..."):
        chosen, selector_reply = select_files(request, repo_map, settings, llm)
    if not chosen:
        console.print("[red]The selector found no relevant files.[/] Try a more specific request.")
        console.print(_usage_table([selector_reply]))
        raise typer.Exit(1)
    console.print(f"Files: [cyan]{', '.join(chosen)}[/]")

    with console.status("Writing the change..."):
        proposal = propose(request, chosen, settings, llm, files)
    replies = [selector_reply, *proposal.replies]

    if proposal.error or not proposal.changes:
        console.print(f"[red]No change produced:[/] {proposal.error or 'the edit changed nothing'}")
        console.print(_usage_table(replies))
        raise typer.Exit(1)

    added = removed = 0
    for change in proposal.changes:
        console.print(Syntax(change.diff, "diff", theme="ansi_dark", background_color="default"))
        a, r = change.stats
        added, removed = added + a, removed + r
    console.print(_usage_table(replies))
    console.print(f"{len(proposal.changes)} file(s) · [green]+{added}[/] [red]-{removed}[/] lines")

    if dry_run:
        return
    if yes or Confirm.ask("Apply this change?", default=True):
        apply_changes(settings, proposal.changes)
        console.print("[green]Applied.[/] Undo with: tokenwise undo")
    else:
        console.print("Nothing changed.")


@app.command()
def undo(root: Path = RootOption) -> None:
    """Undo the last applied change."""
    restored = undo_last(load_settings(root))
    if restored:
        console.print(f"[green]Restored:[/] {', '.join(restored)}")
    else:
        console.print("Nothing to undo.")


if __name__ == "__main__":
    app()
