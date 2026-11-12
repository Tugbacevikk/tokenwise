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
    with pytest.raises(EditError, match="matches 3"):
        plan_changes(project, [Edit("app/auth.py", "(user, password)", "()")])
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
    llm = fake(bad, "no blocks here", bad)
    proposal = propose("x", ["app/auth.py"], settings, llm, ALL_FILES)
    assert proposal.changes == [] and "3 attempts" in proposal.error
    assert "could not be applied" in llm.calls[1]["prompt"]
    assert "no valid edit blocks" in llm.calls[2]["prompt"]
