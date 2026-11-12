import json

from typer.testing import CliRunner

from tokenwise import cli
from tokenwise.ledger import Ledger, ledger_path
from tokenwise.llm import Reply, Usage
from tokenwise.pipeline import Result

from .test_editor import LENGTH_CHECK

runner = CliRunner()


def spend(settings, usd):
    """Records one request that cost about `usd` on Opus (input $4 per million)."""
    reply = Reply("x", "claude-opus-5-5", Usage(input_tokens=int(usd * 250_000)), role="editor")
    Ledger(ledger_path(settings)).record(Result(request="old", replies=[reply], error="x"))


def test_over_budget_stops_before_any_model_call(project, settings, monkeypatch, fake):
    (project / "tokenwise.toml").write_text("[tokenwise]\nbudget_usd_week = 1.0\n")
    spend(settings, 1.5)
    llm = fake('["app/auth.py"]', LENGTH_CHECK)
    monkeypatch.setattr(cli, "make_llm", lambda s: llm)

    result = runner.invoke(cli.app, ["ask", "x", "-r", str(project)])
    assert result.exit_code == 1 and "budget used up" in result.output and llm.calls == []
    data = json.loads(runner.invoke(cli.app, ["ask", "x", "-r", str(project), "--json"]).output)
    assert data["ok"] is False and "budget" in data["error"]

    result = runner.invoke(cli.app, ["ask", "x", "-r", str(project), "--dry-run", "--over-budget"])
    assert result.exit_code == 0 and len(llm.calls) == 2


def test_budget_shown_after_ask_and_in_report(project, settings, monkeypatch, fake):
    (project / "tokenwise.toml").write_text("[tokenwise]\nbudget_usd_week = 2.0\n")
    spend(settings, 1.8)
    monkeypatch.setattr(cli, "make_llm", lambda s: fake('["app/auth.py"]', LENGTH_CHECK))
    result = runner.invoke(cli.app, ["ask", "x", "-r", str(project), "--dry-run"])
    assert "weekly budget used" in result.output
    report = runner.invoke(cli.app, ["report", "-r", str(project)])
    assert "Weekly budget:" in report.output
    data = json.loads(runner.invoke(cli.app, ["report", "-r", str(project), "--json"]).output)
    assert data["budget"]["usd_week"] == 2.0 and data["budget"]["spent_7d_usd"] > 1.8


def test_no_budget_means_no_limit(project, settings, monkeypatch, fake):
    spend(settings, 500)
    monkeypatch.setattr(cli, "make_llm", lambda s: fake('["app/auth.py"]', LENGTH_CHECK))
    result = runner.invoke(cli.app, ["ask", "x", "-r", str(project), "--dry-run"])
    assert result.exit_code == 0 and "budget" not in result.output
