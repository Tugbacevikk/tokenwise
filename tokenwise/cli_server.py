"""Command line, the hosted version: tokenwise serve and tokenwise server (web, worker,
migrate, doctor, selftest). Registered on the app in cli.py."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.markup import escape
from rich.table import Table

from .cli import _fail, app, console


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


server_app = typer.Typer(help="The hosted version: GitHub App, dashboard and worker.")
app.add_typer(server_app, name="server")


@server_app.callback()
def _server_ready() -> None:
    """Fails with an install hint when the server extras are missing."""
    try:
        import cryptography  # noqa: F401
        import fastapi  # noqa: F401
        import psycopg  # noqa: F401
    except ImportError:
        _fail('The hosted version needs extra packages: pip install "tokenwise-coder[server]"')


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


@server_app.command("doctor")
def server_doctor() -> None:
    """Check a hosted setup: database, secrets, the GitHub App, Docker and the work folder."""
    from .server.config import ServerConfig
    from .server.doctor import ready, run_checks

    try:
        server = ServerConfig.from_env()
    except (KeyError, ValueError, OSError) as exc:
        _fail(f"Settings are incomplete: {str(exc).rstrip('.')}. See docs/hosting.md.")
    checks = run_checks(server)
    table = Table(show_header=False, box=None)
    for c in checks:
        mark = "[green]ok[/]" if c.ok else ("[red]HATA[/]" if c.required else "[yellow]uyarı[/]")
        table.add_row(mark, c.name, c.detail)
    console.print(table)
    if not ready(checks):
        raise typer.Exit(1)
    console.print("[green]Hazır.[/] GitHub App'i bir hesaba kurup ilk taramayı bekleyin.")


@server_app.command("selftest")
def server_selftest() -> None:
    """Check a running setup from outside: public address, webhook, the GitHub App's webhook
    URL, sign-in and a worker taking jobs. Run it after `docker compose up`."""
    from .server.config import ServerConfig
    from .server.selftest import run_selftest

    try:
        server = ServerConfig.from_env()
    except (KeyError, ValueError, OSError) as exc:
        _fail(f"Settings are incomplete: {str(exc).rstrip('.')}. See docs/hosting.md.")
    console.print(f"{server.public_url} deneniyor (worker için en çok 45 saniye)…")
    checks = run_selftest(server)
    table = Table(show_header=False, box=None)
    for c in checks:
        table.add_row("[green]ok[/]" if c.ok else "[red]HATA[/]", c.name, escape(c.detail))
    console.print(table)
    if not all(c.ok for c in checks):
        raise typer.Exit(1)
    console.print("[green]Dışarıdan her şey yolunda.[/] GitHub App'i deneme deposuna kurabilirsin.")


if __name__ == "__main__":
    app()
