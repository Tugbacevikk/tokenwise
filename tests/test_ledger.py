from tokenwise.ledger import Ledger, ledger_path
from tokenwise.pipeline import run

from .test_editor import LENGTH_CHECK


def test_record_and_summarise(settings, fake):
    ledger = Ledger(ledger_path(settings))
    ok = run("check length", settings, fake('["app/auth.py"]', LENGTH_CHECK))
    failed = run("x", settings, fake("[]"))
    first = ledger.record(ok)
    ledger.record(failed)
    ledger.mark_applied(first)

    s = ledger.summary(7)
    assert (s["runs"], s["ok"], s["applied"], s["calls"]) == (2, 1, 1, 3)
    assert s["lines_added"] == 2
    assert s["cost_usd"] == ok.cost + failed.cost
    assert {m["model"] for m in s["by_model"]} == {"claude-haiku-5-5", "claude-sonnet-5-5"}
    rows = ledger.rows()
    assert [r["ok"] for r in rows] == [1, 0] and rows[0]["calls"] == 2


def test_window_excludes_old_runs(settings, fake):
    ledger = Ledger(ledger_path(settings))
    old = ledger.record(run("x", settings, fake("[]")))
    ledger.backdate(old, "2020-01-01T00:00:00+00:00")
    ledger.record(run("x", settings, fake("[]")))
    assert ledger.summary(7)["runs"] == 1
    assert ledger.summary(None)["runs"] == 2


def test_empty_summary(settings):
    s = Ledger(ledger_path(settings)).summary(7)
    assert s["runs"] == 0 and s["avg_cost_per_run"] == 0 and s["cache_hit_rate"] == 0
