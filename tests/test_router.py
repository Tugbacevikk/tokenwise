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
