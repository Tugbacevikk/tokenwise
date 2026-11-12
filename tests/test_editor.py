import pytest

from tokenwise.editor import (
    Edit,
    EditError,
    apply_changes,
    parse_edits,
    parse_need_files,
    plan_changes,
    propose,
    undo_last,
)

LENGTH_CHECK = """app/auth.py
<<<<<<< SEARCH
def login(user, password):
=======
def login(user, password):
    if len(password) < 8:
        raise ValueError("password too short")
>>>>>>> REPLACE
"""

ALL_FILES = ["app/auth.py", "app/util.py", "web/api.ts"]


def test_parse_edits_handles_fences_and_empty_search():
    text = (
        "```python\n"
        + LENGTH_CHECK
        + "```\nnew.py\n<<<<<<< SEARCH\n=======\nx = 1\n>>>>>>> REPLACE"
    )
    edits = parse_edits(text)
    assert [e.path for e in edits] == ["app/auth.py", "new.py"]
    assert edits[0].search == "def login(user, password):\n"
    assert edits[1] == Edit("new.py", "", "x = 1\n")


def test_parse_need_files():
    assert parse_need_files("NEED_FILES: app/util.py, ./web/api.ts") == [
        "app/util.py",
        "web/api.ts",
    ]
    assert parse_need_files(LENGTH_CHECK) == []


def test_plan_changes_edits_only_matching_lines(project):
    (change,) = plan_changes(project, parse_edits(LENGTH_CHECK))
    assert change.old.startswith("def login")
    assert 'raise ValueError("password too short")' in change.new
    assert change.stats == (2, 0)
    assert "+    if len(password) < 8:" in change.diff


def test_plan_changes_tolerates_blank_edges(project):
    edit = Edit("app/util.py", "\n        return 0\n\n", "\n        return 1\n\n")
    (change,) = plan_changes(project, [edit])
    assert "return 1" in change.new


def test_plan_changes_errors(project):
    with pytest.raises(EditError, match="not found"):
        plan_changes(project, [Edit("app/auth.py", "nope\n", "x\n")])
    with pytest.raises(EditError, match="matches 2"):
        plan_changes(project, [Edit("app/auth.py", "    return ", "    yield ")])
    with pytest.raises(EditError, match="not empty"):
        plan_changes(project, [Edit("app/auth.py", "", "x\n")])
    with pytest.raises(EditError, match="inside the project"):
        plan_changes(project, [Edit("../evil.py", "", "x\n")])


def test_new_file_apply_and_undo(settings):
    changes = plan_changes(settings.root, [Edit("app/new.py", "", "X = 1\n")])
    assert changes[0].old is None
    apply_changes(settings, changes)
    assert (settings.root / "app/new.py").read_text() == "X = 1\n"
    assert undo_last(settings) == ["app/new.py"]
    assert not (settings.root / "app/new.py").exists()
    assert undo_last(settings) == []


def test_apply_and_undo_restores_original(settings):
    original = (settings.root / "app/auth.py").read_text()
    apply_changes(settings, plan_changes(settings.root, parse_edits(LENGTH_CHECK)))
    assert "too short" in (settings.root / "app/auth.py").read_text()
    undo_last(settings)
    assert (settings.root / "app/auth.py").read_text() == original


def test_propose_sends_only_chosen_files(settings, fake):
    llm = fake(LENGTH_CHECK)
    proposal = propose("check length", ["app/auth.py"], settings, llm, ALL_FILES)
    assert proposal.error is None and len(proposal.changes) == 1
    prompt = llm.calls[0]["prompt"]
    assert "def check(user, password)" in prompt  # auth.py content
    assert "return 0" not in prompt  # util.py content is not sent
    assert "app/util.py" in prompt  # but its name is listed


def test_propose_fetches_needed_files(settings, fake):
    llm = fake("NEED_FILES: app/util.py", LENGTH_CHECK)
    proposal = propose("x", ["app/auth.py"], settings, llm, ALL_FILES)
    assert proposal.files_sent == ["app/auth.py", "app/util.py"]
    assert "return 0" in llm.calls[1]["prompt"]
    assert len(proposal.replies) == 2


def test_propose_retries_with_feedback_then_gives_up(settings, fake):
    bad = "app/auth.py\n<<<<<<< SEARCH\nmissing\n=======\nx\n>>>>>>> REPLACE\n"
    llm = fake(bad, "no blocks here")
    proposal = propose("x", ["app/auth.py"], settings, llm, ALL_FILES, models=["m1"])
    assert proposal.changes == [] and proposal.error == "no applicable edit from m1"
    assert "could not be applied" in llm.calls[1]["prompt"]
    assert len(llm.calls) == 2


def test_propose_climbs_the_ladder_on_failure(settings, fake):
    bad = "app/auth.py\n<<<<<<< SEARCH\nmissing\n=======\nx\n>>>>>>> REPLACE\n"
    llm = fake(bad, bad, LENGTH_CHECK)
    proposal = propose("x", ["app/auth.py"], settings, llm, ALL_FILES, models=["cheap", "strong"])
    assert proposal.model == "strong" and len(proposal.changes) == 1
    assert [c["model"] for c in llm.calls] == ["cheap", "cheap", "strong"]


def test_propose_uses_settings_ladder_by_default(settings, fake):
    llm = fake(LENGTH_CHECK)
    proposal = propose("x", ["app/auth.py"], settings, llm, ALL_FILES)
    assert proposal.model == settings.models[0] == llm.calls[0]["model"]


@pytest.mark.parametrize(
    "line",
    ["**app/auth.py**", "`app/auth.py`", "### app/auth.py", "File: app/auth.py", "app/auth.py:"],
)
def test_decorated_path_lines(line):
    text = f"{line}\n```python\n<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n```"
    assert parse_edits(text) == [Edit("app/auth.py", "x\n", "y\n")]


def test_loose_markers_and_missing_paths():
    text = (
        "Here is the change:\n"
        "```\n<<<<<SEARCH\na\n=====\nb\n>>>>> REPLACE\n```\n"
        "```\n<<<<<<< SEARCH\nc\n=======\nd\n>>>>>>> REPLACE\n```"
    )
    assert parse_edits(text) == []  # no path anywhere and no default
    assert parse_edits(text, default_path="one.py") == [
        Edit("one.py", "a\n", "b\n"),
        Edit("one.py", "c\n", "d\n"),
    ]
    later = "b.py\n<<<<<<< SEARCH\n1\n=======\n2\n>>>>>>> REPLACE\n" + text.split(":\n", 1)[1]
    assert {e.path for e in parse_edits(later)} == {"b.py"}  # follows the previous block


def test_clean_path_rejects_prose():
    from tokenwise.editor import clean_path

    assert clean_path("Here is the change:") is None
    assert clean_path("```python") is None
    assert clean_path("Makefile") is None
    assert clean_path("./src/app.ts") == "src/app.ts"


def test_wrong_indentation_is_fixed(project):
    # the model dropped the 4-space indent of the method body
    edit = Edit("app/util.py", "return 0\n", "if self:\n    return 1\nreturn 0\n")
    (change,) = plan_changes(project, [edit])
    assert change.new.endswith("        if self:\n            return 1\n        return 0\n")


def test_too_much_indentation_is_fixed(project):
    edit = Edit(
        "app/auth.py",
        "        def check(user, password):\n",
        "        def check(user, password=None):\n",
    )
    (change,) = plan_changes(project, [edit])
    assert "\ndef check(user, password=None):\n" in change.new


def test_ambiguous_loose_match_still_fails(project):
    (project / "app/dup.py").write_text("def a():\n    pass\n\ndef b():\n    pass\n")
    with pytest.raises(EditError, match="not found"):
        plan_changes(project, [Edit("app/dup.py", "\tpass\n", "\treturn\n")])


def test_propose_uses_the_only_file_when_path_is_missing(settings, fake):
    reply = "```\n<<<<<<< SEARCH\n    return True\n=======\n    return False\n>>>>>>> REPLACE\n```"
    proposal = propose("x", ["app/auth.py"], settings, fake(reply), ALL_FILES, models=["m"])
    assert proposal.changes and "return False" in proposal.changes[0].new
