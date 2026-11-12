"""Command line: tokenwise init | map | ask | undo | cache."""

from __future__ import annotations

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
from .editor import apply_changes, propose, undo_last
from .llm import LLM, CachedLLM, ConfigError, Reply, Usage, cache_path
from .repo_map import build_map, estimate_tokens, list_source_files, source_tokens
from .router import NAMES, difficulty, ladder
from .selector import select_files

app = typer.Typer(
    help="tokenwise: get AI-written code with the fewest tokens and the fewest lines.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

RootOption = typer.Option(Path("."), "--root", "-r", help="Project folder.")


def make_llm(settings: Settings) -> LLM:
    """Replaced in tests with a fake model."""
    try:
        return llm_module.make_llm(settings)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None


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
    config = root / CONFIG_FILE
    if not config.exists():
        config.write_text(config_template("openai" if local else "anthropic"), "utf-8")
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


def _usage_table(replies: list[Reply]) -> Table:
    table = Table(title="Token usage", show_edge=False)
    for column in ("step", "model", "input", "cache read", "output"):
        table.add_column(column, justify="left" if column in ("step", "model") else "right")
    total = Usage()
    for reply in replies:
        u = reply.usage
        total = total + u
        table.add_row(
            reply.role + (" [dim](cached)[/]" if reply.cached else ""),
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
    model: str = typer.Option("", "--model", "-m", help="Force one editor model, no climbing."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Always ask the model."),
) -> None:
    """Make a code change with the least context and the fewest lines."""
    settings = load_settings(root)
    if no_cache:
        settings.cache = False
    llm = make_llm(settings)
    files = list_source_files(settings)
    repo_map = build_map(settings, files)

    with console.status("Choosing files..."):
        chosen, selector_reply = select_files(request, repo_map, settings, llm)
    if not chosen:
        console.print("[red]The selector found no relevant files.[/] Try a more specific request.")
        console.print(_usage_table([selector_reply]))
        raise typer.Exit(1)
    console.print(f"Files: [cyan]{', '.join(chosen)}[/]")

    if model:
        models = [model]
        console.print(f"Model: [cyan]{model}[/] (forced)")
    else:
        level = difficulty(request, settings.root, chosen)
        models = ladder(settings.models, level)
        console.print(f"Difficulty: [cyan]{NAMES[level]}[/] → {' → '.join(models)}")

    with console.status("Writing the change..."):
        proposal = propose(request, chosen, settings, llm, files, models)
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
    console.print(
        f"{len(proposal.changes)} file(s) · [green]+{added}[/] [red]-{removed}[/] lines · "
        f"by {proposal.model}"
    )

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
