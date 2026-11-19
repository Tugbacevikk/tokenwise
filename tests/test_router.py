import pytest

from tokenwise.router import EASY, HARD, MEDIUM, difficulty, ladder


def test_keywords_set_level(project):
    assert difficulty("fix the typo in the error message", project, []) == EASY
    assert difficulty("hata mesajındaki yazım hatasını düzelt", project, []) == EASY
    assert difficulty("add a length check to login", project, ["app/auth.py"]) == MEDIUM
    assert difficulty("refactor auth into a class", project, ["app/auth.py"]) == HARD
    assert difficulty("kimlik doğrulamayı yeniden düzenle", project, []) == HARD


def test_size_bumps_level(project):
    many = ["a.py", "b.py", "c.py", "d.py"]
    assert difficulty("rename a variable", project, many) == MEDIUM
    big = project / "big.py"
    big.write_text("x = 1\n" * 700, "utf-8")
    assert difficulty("add a check", project, ["big.py"]) == HARD
    assert difficulty("refactor it", project, ["big.py"]) == HARD


def test_ladder_starts_at_level_and_climbs():
    models = ("cheap", "mid", "strong")
    assert ladder(models, EASY) == ["cheap", "mid", "strong"]
    assert ladder(models, MEDIUM) == ["mid", "strong"]
    assert ladder(models, HARD) == ["strong"]


def test_ladder_stretches_short_lists():
    assert ladder(("only",), HARD) == ["only"]
    assert ladder(("a", "b"), EASY) == ["a", "b"]
    assert ladder(("a", "b"), MEDIUM) == ["b"]
    with pytest.raises(ValueError):
        ladder((), EASY)


def test_focused_big_file_counts_only_what_is_sent(project):
    from tokenwise.router import MEDIUM, difficulty

    funcs = "".join(f"def f{i}(x):\n    return x + {i}\n\n\n" for i in range(200))
    (project / "huge.py").write_text("import os\n\n\n" + funcs)
    assert difficulty("add a check", project, ["huge.py"]) == HARD  # whole file: 800 lines
    assert difficulty("add a check", project, ["huge.py"], {"huge.py": ["f7"]}) == MEDIUM


def test_topic_nouns_do_not_make_a_request_hard(tmp_path):
    assert difficulty("cache komutuna --json seçeneği ekle", tmp_path, []) == MEDIUM
    assert difficulty("add a column to the database report", tmp_path, []) == MEDIUM
    assert difficulty("rename the schema field", tmp_path, []) == EASY


def _branchy(name: str, n: int) -> str:
    body = "".join(f"    if x == {i}:\n        return {i}\n" for i in range(n))
    return f"def {name}(x):\n{body}    return -1\n\n\n"


def test_tangled_target_code_starts_higher(tmp_path):
    from tokenwise.config import Settings
    from tokenwise.router import route, target_complexity

    (tmp_path / "rules.py").write_text(_branchy("pick", 16) + _branchy("small", 1), "utf-8")
    settings = Settings(root=tmp_path)
    assert target_complexity(tmp_path, ["rules.py"], None) == 17
    decision = route("add a check", settings, ["rules.py"], history=False)
    assert decision.level == HARD
    assert [(s.name, s.value, s.points) for s in decision.signals if s.points] == [
        ("complexity", "17", 1)
    ]
    # the selector focused on the simple function: that is what the editor changes
    assert route("add a check", settings, ["rules.py"], {"rules.py": ["small"]},
                 history=False).level == MEDIUM  # fmt: skip
    assert "complexity of the target code 17 (+1)" in decision.describe()


def test_code_many_files_import_starts_higher(tmp_path):
    from tokenwise.config import Settings
    from tokenwise.graph import Graph
    from tokenwise.router import route

    graph = Graph(files=["core.py"] + [f"u{i}.py" for i in range(5)])
    for i in range(5):
        graph.add(f"u{i}.py", "core.py")
    (tmp_path / "core.py").write_text("def f():\n    return 1\n", "utf-8")
    settings = Settings(root=tmp_path)
    decision = route("add a check", settings, ["core.py"], graph=graph, history=False)
    assert decision.level == HARD and ("reach", "5", 1) in [
        (s.name, s.value, s.points) for s in decision.signals
    ]
    graph.rdeps["core.py"] = {"u0.py"}
    assert route("add a check", settings, ["core.py"], graph=graph, history=False).level == MEDIUM


def test_this_project_s_escalations_move_the_start_up(tmp_path):
    from tokenwise.config import Settings
    from tokenwise.ledger import Ledger, ledger_path
    from tokenwise.pipeline import Result
    from tokenwise.router import route

    (tmp_path / "a.py").write_text("def f():\n    return 1\n", "utf-8")
    settings = Settings(root=tmp_path, models=("cheap", "mid", "strong"))
    ledger = Ledger(ledger_path(settings))

    def record(model: str) -> None:
        r = Result(request="x", level="medium", models=["mid", "strong"], model=model)
        r.changes = [type("C", (), {"stats": (1, 0)})()]
        ledger.record(r)

    for model in ("mid", "mid", "strong", "strong"):
        record(model)
    assert route("add a check", settings, ["a.py"]).level == MEDIUM  # 4 runs: too few yet
    record("strong")  # 3 of the last 5 medium requests had to climb to the strong model
    decision = route("add a check", settings, ["a.py"])
    assert decision.level == HARD and decision.signals[-1].value == "3/5"
    assert route("add a check", settings, ["a.py"], history=False).level == MEDIUM


def test_pipeline_reports_why(settings, fake):
    from tokenwise.pipeline import run

    from .test_editor import LENGTH_CHECK

    result = run("check password length", settings, fake('["app/auth.py"]', LENGTH_CHECK))
    signals = {s["signal"] for s in result.to_dict()["route"]}
    assert {"words", "files", "complexity", "reach"} <= signals


def test_old_escalations_are_forgotten(tmp_path):
    import sqlite3

    from tokenwise.config import Settings
    from tokenwise.ledger import Ledger, ledger_path
    from tokenwise.pipeline import Result
    from tokenwise.router import escalation_history

    settings = Settings(root=tmp_path, models=("cheap", "mid", "strong"))
    ledger = Ledger(ledger_path(settings))
    for _ in range(6):
        r = Result(request="x", level="medium", models=["mid", "strong"], model="strong")
        r.changes = [type("C", (), {"stats": (1, 0)})()]
        ledger.record(r)
    assert escalation_history(settings, 1) == (6, 6)
    with sqlite3.connect(ledger_path(settings)) as db:
        db.execute("UPDATE runs SET ts = '2020-01-01T00:00:00+00:00'")
    assert escalation_history(settings, 1) == (0, 0)


def test_simple_router_uses_wording_and_size_only(tmp_path):
    from tokenwise.config import Settings
    from tokenwise.router import route

    (tmp_path / "rules.py").write_text(_branchy("pick", 16), "utf-8")
    smart = route("add a check", Settings(root=tmp_path), ["rules.py"], history=False)
    simple = route("add a check", Settings(root=tmp_path, router="simple"), ["rules.py"])
    assert smart.level == HARD and simple.level == MEDIUM
    assert {s.name for s in simple.signals} == {"words", "files"}
