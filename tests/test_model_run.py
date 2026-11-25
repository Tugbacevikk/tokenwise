"""The model benchmark workflow's helper (benchmark/model_run.py): inputs, config, summary."""

from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "model_run", Path(__file__).parent.parent / "benchmark" / "model_run.py"
)
model_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model_run)


def _env(monkeypatch, **values):
    for key in ("REPEAT", "MODELS", "MAX_USD", "ONLY"):
        monkeypatch.setenv(key, values.get(key, ""))


def test_defaults_use_the_openrouter_ladder(monkeypatch):
    from tokenwise.openrouter import MODELS

    _env(monkeypatch)
    assert model_run.inputs() == {
        "repeat": 3, "models": list(MODELS), "max_usd": 0.05, "only": ""
    }  # fmt: skip


@pytest.mark.parametrize(
    "values",
    [
        {"REPEAT": "0"},
        {"REPEAT": "50"},
        {"MAX_USD": "5"},  # a typo must not spend $5 a run
        {"MAX_USD": "0"},
        {"MODELS": 'qwen", "x'},  # would break out of the TOML string
        {"ONLY": "a b"},
    ],
)
def test_bad_inputs_are_refused(monkeypatch, values):
    _env(monkeypatch, **values)
    with pytest.raises(ValueError):
        model_run.inputs()


def test_config_is_valid_toml_with_the_ceiling(monkeypatch):
    text = model_run.config("simple", ["~deepseek/deepseek-flash-latest", "qwen/qwen-2.5:7b"], 0.05)
    data = tomllib.loads(text)["tokenwise"]
    assert data["provider"] == "openrouter" and data["router"] == "simple"
    assert data["models"] == ["~deepseek/deepseek-flash-latest", "qwen/qwen-2.5:7b"]
    assert data["max_usd_per_request"] == 0.05


def test_summary_compares_both_routers(tmp_path, monkeypatch):
    _env(monkeypatch, REPEAT="1")
    monkeypatch.setattr(model_run, "OUT", tmp_path)
    run = {"passed": True, "regression": False, "escalated": True, "cost": 0.01, "level": "easy"}
    (tmp_path / "smart.json").write_text(json.dumps({"runs": [run, {**run, "passed": False}]}))
    (tmp_path / "smart.md").write_text("| table |")
    text = model_run.summary()
    assert "| smart | 1/2 | 0 | 2/2 | easy | 0.0200 |" in text
    assert "| simple | – | – | – | did not finish | – |" in text
    assert "Total cost: $0.0200" in text and "| table |" in text
