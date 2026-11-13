from tokenwise.editor import Change, propose
from tokenwise.guard import problems


def ch(path, old, new):
    return Change(path, old, new)


def test_invalid_python_is_reported(tmp_path):
    found = problems(
        tmp_path, [ch("a.py", "x = 1\n", "class A:\n    def f(self):\n    pass\n")], ["a.py"]
    )
    assert len(found) == 1 and "invalid Python at line 3" in found[0]


def test_removed_but_still_used_in_the_same_file(tmp_path):
    old = "def predict(s):\n    return s\n\npredict('a')\n"
    new = "if __name__ == '__main__':\n    predict('a')\n\npredict('a')\n"
    assert problems(tmp_path, [ch("p.py", old, new)], ["p.py"]) == [
        "p.py: you removed `predict`, but the file still uses it"
    ]


def test_removed_but_used_by_another_module(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "ml.py").write_text("from config.settings import CHUNK_SIZE\nprint(CHUNK_SIZE)\n")
    (tmp_path / "other.py").write_text("CHUNK_SIZE = 3  # its own, unrelated\n")
    old = "CHUNK_SIZE = 500\nTOP_K = 4\n"
    new = "RAG_CHUNK_SIZE = 500\nTOP_K = 4\n"
    files = ["config/settings.py", "ml.py", "other.py"]
    assert problems(tmp_path, [ch("config/settings.py", old, new)], files) == [
        "config/settings.py: you removed `CHUNK_SIZE`, but ml.py still uses it"
    ]


def test_fine_changes_pass(tmp_path):
    assert (
        problems(tmp_path, [ch("a.py", "def f():\n    pass\n", "def g():\n    pass\n")], ["a.py"])
        == []
    )
    assert problems(tmp_path, [ch("new.py", None, "x = 1\n")], ["new.py"]) == []
    assert (
        problems(tmp_path, [ch("broken.py", "def (:\n", "def (:\n  x\n")], []) == []
    )  # was already broken
    assert problems(tmp_path, [ch("a.js", "x", "{{{")], []) == []  # only Python is checked


def test_propose_sends_guard_problems_back(tmp_path, fake):
    from tokenwise.config import Settings

    (tmp_path / "log.py").write_text(
        'class Logger:\n    """Doc."""\n\n    def close(self):\n        pass\n'
    )
    broken = "log.py\n<<<<<<< SEARCH\nclass Logger:\n=======\nclass Logger:\n    def __enter__(self):\n>>>>>>> REPLACE"
    good = "log.py\n<<<<<<< SEARCH\n    def close(self):\n        pass\n=======\n    def close(self):\n        pass\n\n    def __enter__(self):\n        return self\n>>>>>>> REPLACE"
    llm = fake(broken, good)
    proposal = propose("x", ["log.py"], Settings(root=tmp_path), llm, ["log.py"], ["m"])
    assert proposal.changes and "__enter__" in proposal.changes[0].new
    assert (
        "breaks the code" in llm.calls[1]["prompt"] and "invalid Python" in llm.calls[1]["prompt"]
    )


def test_error_says_what_went_wrong_last(tmp_path, fake):
    from tokenwise.config import Settings

    (tmp_path / "a.py").write_text("x = 1\n")
    bad = "a.py\n<<<<<<< SEARCH\nnope\n=======\ny\n>>>>>>> REPLACE"
    proposal = propose("x", ["a.py"], Settings(root=tmp_path), fake(bad, bad), ["a.py"], ["m"])
    assert proposal.error == "no applicable edit from m; last problem: a.py: SEARCH text not found"
