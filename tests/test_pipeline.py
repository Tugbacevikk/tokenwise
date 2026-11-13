from tokenwise.pipeline import run

from .test_editor import LENGTH_CHECK


def test_run_success_collects_everything(settings, fake):
    llm = fake('["app/auth.py"]', LENGTH_CHECK)
    result = run("check password length", settings, llm)
    assert result.ok and result.level == "medium"
    assert result.models == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert result.model == "claude-sonnet-5-5"
    assert result.lines == (2, 0)
    assert result.cost > 0
    data = result.to_dict()
    assert data["changes"][0]["path"] == "app/auth.py"
    assert [c["role"] for c in data["calls"]] == ["selector", "editor"]
    assert data["usage"]["input"] == result.usage.input_tokens


def test_run_forced_model(settings, fake):
    result = run("x", settings, fake('["app/auth.py"]', LENGTH_CHECK), model="local")
    assert result.level == "forced" and result.models == ["local"] and result.cost > 0


def test_run_without_files(settings, fake):
    result = run("x", settings, fake("[]"))
    assert not result.ok and "no relevant files" in result.error
    assert len(result.replies) == 1


def test_run_edit_failure(settings, fake):
    bad = "app/auth.py\n<<<<<<< SEARCH\nnope\n=======\nx\n>>>>>>> REPLACE\n"
    result = run("x", settings, fake('["app/auth.py"]', bad, bad), model="m")
    assert not result.ok and result.error.startswith("no applicable edit from m;")
