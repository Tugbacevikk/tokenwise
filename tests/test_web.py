import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tokenwise.config import Settings  # noqa: E402
from tokenwise.ledger import Ledger, ledger_path  # noqa: E402
from tokenwise.llm import Reply, Usage  # noqa: E402
from tokenwise.pipeline import Result  # noqa: E402
from tokenwise.web import create_app, parse_plan, project_ids  # noqa: E402

EDIT = (
    "app/auth.py\n"
    "<<<<<<< SEARCH\n"
    "    return True\n"
    "=======\n"
    "    return len(password) >= 8\n"
    ">>>>>>> REPLACE\n"
)
EDIT_UTIL = (
    "app/util.py\n<<<<<<< SEARCH\n        return 0\n=======\n        return 1\n>>>>>>> REPLACE\n"
)
SELECTED = '["app/auth.py"]'
PLAN = """Here is the plan:
[
  {"title": "Şifre kontrolü", "prompt": "app/auth.py içindeki check fonksiyonuna şifre uzunluk kontrolü ekle.",
   "files": ["app/auth.py", "uydurma.py"], "check": "", "level": "easy", "depends_on": []},
  {"title": "Test", "prompt": "Kontrol için test ekle.", "files": ["app/auth.py"],
   "check": "{python} -m pytest -q", "level": "medium", "depends_on": [0]}
]"""


class ScriptedLLM:
    """Returns the queued replies in order and records the models it was asked for."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[str] = []

    def complete(self, *, model, system, prompt, max_tokens):
        self.calls.append(model)
        text = self.texts.pop(0)
        return Reply(text=text, model=model, usage=Usage(1000, len(text) // 4))


@pytest.fixture
def client_for(project: Path):
    def make(*texts: str, token: str = ""):
        llm = ScriptedLLM(*texts)
        app = create_app(project, llm_factory=lambda settings: llm, token=token)
        client = TestClient(app)
        if token:
            client.headers["X-Tokenwise-Token"] = token
        pid = next(iter(project_ids([project])))
        client.api = lambda suffix, pid=pid: f"/api/projects/{pid}{suffix}"
        return client, llm

    return make


def ask(client, request: str, **extra) -> dict:
    return client.post(client.api("/ask"), json={"request": request, **extra}).json()


def test_index_serves_the_page(project):
    assert TestClient(create_app(project)).get("/").text.count("tokenwise") >= 1


def test_projects_list_and_info(client_for):
    client, _ = client_for()
    listed = client.get("/api/projects").json()
    assert client.api("") == f"/api/projects/{listed[0]['id']}"
    info = client.get(client.api("")).json()
    assert "app/auth.py" in info["files"]
    assert not any(f.startswith("node_modules") for f in info["files"])
    assert info["git"] is False


def test_unknown_project_is_404(client_for):
    client, _ = client_for()
    assert client.get("/api/projects/nope").status_code == 404


def test_several_projects_get_distinct_ids(tmp_path):
    a, b = tmp_path / "x" / "shop", tmp_path / "y" / "shop"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    ids = project_ids([a, b])
    assert list(ids) == ["shop", "shop-2"]
    assert ids["shop-2"] == b.resolve()


def test_ask_then_apply_then_undo(project, client_for):
    client, _ = client_for(SELECTED, EDIT)
    body = ask(client, "add a length check to login")
    assert body["ok"] is True
    assert body["changes"][0]["path"] == "app/auth.py"
    assert "return True" in (project / "app" / "auth.py").read_text("utf-8")  # not written yet

    assert client.post(client.api("/apply"), json={"run_id": body["run_id"]}).status_code == 200
    assert "len(password)" in (project / "app" / "auth.py").read_text("utf-8")

    assert client.post(client.api("/undo")).json()["undone"] == ["app/auth.py"]
    assert "return True" in (project / "app" / "auth.py").read_text("utf-8")


def test_manual_files_skip_the_selector(client_for):
    client, llm = client_for(EDIT_UTIL)
    body = ask(client, "return 1 instead of 0", files=["app/util.py"])
    assert body["ok"] is True
    assert body["files"] == ["app/util.py"]
    assert len(llm.calls) == 1  # only the editor, no selector call


def test_apply_refuses_when_the_file_changed_since_the_proposal(project, client_for):
    client, _ = client_for(SELECTED, EDIT)
    run_id = ask(client, "add a length check to login")["run_id"]
    (project / "app" / "auth.py").write_text("def check():\n    return False\n", "utf-8")
    assert client.post(client.api("/apply"), json={"run_id": run_id}).status_code == 409
    assert "return False" in (project / "app" / "auth.py").read_text("utf-8")


def test_apply_twice_is_refused(client_for):
    client, _ = client_for(SELECTED, EDIT)
    run_id = ask(client, "add a length check to login")["run_id"]
    assert client.post(client.api("/apply"), json={"run_id": run_id}).status_code == 200
    assert client.post(client.api("/apply"), json={"run_id": run_id}).status_code == 404


def test_undo_without_history_is_404(client_for):
    client, _ = client_for()
    assert client.post(client.api("/undo")).status_code == 404


def test_estimate_counts_map_or_picked_files(client_for):
    client, _ = client_for()
    with_map = client.post(client.api("/estimate"), json={"request": "x"}).json()
    assert with_map["map_tokens"] > 0
    picked = client.post(
        client.api("/estimate"), json={"request": "x", "files": ["app/util.py"]}
    ).json()
    assert picked["map_tokens"] == 0
    assert picked["files_tokens"] > 0
    assert picked["input_tokens"] < with_map["input_tokens"]


def test_plan_is_saved_and_can_be_reopened_and_exported(client_for):
    client, llm = client_for(PLAN)
    plan = client.post(client.api("/plan"), json={"goal": "login güvenliği"}).json()
    threads = plan["threads"]
    assert [t["title"] for t in threads] == ["Şifre kontrolü", "Test"]
    assert threads[0]["files"] == ["app/auth.py"]  # the invented uydurma.py is dropped
    assert threads[1]["depends_on"] == [0]
    assert threads[0]["status"] == "pending"
    assert llm.calls[0] == Settings(root=Path(".")).models[1]  # the planner's rung

    listed = client.get(client.api("/plans")).json()
    assert listed[0]["id"] == plan["id"] and listed[0]["total"] == 2

    assert client.get(client.api(f"/plans/{plan['id']}")).json()["goal"] == "login güvenliği"
    export = client.get(client.api(f"/plans/{plan['id']}/export"))
    assert export.status_code == 200
    assert "## [ ] 1. Şifre kontrolü" in export.text


def test_thread_status_is_saved(client_for):
    client, _ = client_for(PLAN)
    plan = client.post(client.api("/plan"), json={"goal": "x"}).json()
    response = client.put(client.api(f"/plans/{plan['id']}/threads/0"), json={"status": "done"})
    assert response.json()["status"] == "done"
    again = client.get(client.api(f"/plans/{plan['id']}")).json()
    assert again["threads"][0]["status"] == "done"
    assert client.get(client.api("/plans")).json()[0]["done"] == 1
    bad = client.put(client.api(f"/plans/{plan['id']}/threads/0"), json={"status": "maybe"})
    assert bad.status_code == 400


def test_bad_plan_ids_are_not_read(client_for):
    client, _ = client_for()
    assert client.get(client.api("/plans/..%2Fsecret")).status_code == 404


def test_plan_is_recorded_in_the_ledger_as_plan(project, client_for):
    client, _ = client_for(PLAN)
    client.post(client.api("/plan"), json={"goal": "login güvenliği"})
    ledger = Ledger(ledger_path(Settings(root=project)))
    try:
        assert ledger.summary(7, method="plan")["calls"] == 1
        assert ledger.summary(7)["runs"] == 0
    finally:
        ledger.close()


def test_plan_with_garbage_is_a_clear_error(client_for):
    client, _ = client_for("I cannot help with that.")
    assert client.post(client.api("/plan"), json={"goal": "x"}).status_code == 502


def test_budget_blocks_a_new_request(project, client_for):
    (project / "tokenwise.toml").write_text("[tokenwise]\nbudget_usd_week = 1.0\n", "utf-8")
    ledger = Ledger(ledger_path(Settings(root=project)))
    try:
        spent = Result(
            request="earlier",
            replies=[Reply("x", "claude-opus-5-5", Usage(1_000_000, 0))],
            prices=Settings(root=project).prices,
        )
        ledger.record(spent)  # $4 of Opus input in the last 7 days
    finally:
        ledger.close()
    client, _ = client_for(SELECTED, EDIT)
    response = client.post(client.api("/ask"), json={"request": "add a length check to login"})
    assert response.status_code == 409
    assert "bütçe" in response.json()["detail"]
    assert "Yine de çalıştır" in response.json()["detail"]  # the page's override button
    forced = client.post(client.api("/ask"),
                         json={"request": "add a length check to login", "over_budget": True})  # fmt: skip
    assert forced.status_code == 200 and forced.json()["changes"]


def test_commit_only_the_changed_files(project, client_for):
    git = ["git", "-C", str(project)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "config", "user.email", "t@example.com"], check=True)
    subprocess.run([*git, "config", "user.name", "tester"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    (project / "unrelated.txt").write_text("not mine\n", "utf-8")  # must stay out of the commit

    client, _ = client_for(SELECTED, EDIT)
    assert client.get(client.api("")).json()["git"] is True
    run_id = ask(client, "add a length check to login")["run_id"]
    client.post(client.api("/apply"), json={"run_id": run_id})
    commit = client.post(client.api("/commit"), json={"run_id": run_id})
    assert commit.status_code == 200
    assert commit.json()["message"].startswith("tokenwise: add a length check")
    files = subprocess.run(
        [*git, "show", "--name-only", "--format=", "HEAD"], capture_output=True, text=True
    ).stdout.split()
    assert files == ["app/auth.py"]
    status = subprocess.run([*git, "status", "--short"], capture_output=True, text=True).stdout
    assert "unrelated.txt" in status
    # undoing now would leave the commit and a modified file: point to git revert instead
    undo = client.post(client.api("/undo"))
    assert undo.status_code == 409
    assert f"git revert {commit.json()['commit']}" in undo.json()["detail"]
    assert (
        "app/auth.py"
        not in subprocess.run([*git, "status", "--short"], capture_output=True, text=True).stdout
    )
    # a later change can be undone again
    client2, _ = client_for(SELECTED, EDIT_UTIL.replace("app/util.py", "app/auth.py")
                            .replace("        return 0", "    return len(password) >= 8")
                            .replace("        return 1", "    return len(password) >= 10"))  # fmt: skip
    run2 = ask(client2, "make the length check stricter")["run_id"]
    assert client2.post(client2.api("/apply"), json={"run_id": run2}).status_code == 200
    assert client2.post(client2.api("/undo")).json()["undone"] == ["app/auth.py"]


def test_commit_outside_git_is_refused(client_for):
    client, _ = client_for(SELECTED, EDIT)
    run_id = ask(client, "add a length check to login")["run_id"]
    client.post(client.api("/apply"), json={"run_id": run_id})
    assert client.post(client.api("/commit"), json={"run_id": run_id}).status_code == 400


def test_token_is_required_for_api_calls(project, client_for):
    client, _ = client_for(token="s3cret")
    client.headers.pop("X-Tokenwise-Token")
    assert client.get("/api/projects").status_code == 401
    assert client.get("/").status_code == 200  # the page itself carries no secrets
    client.headers["X-Tokenwise-Token"] = "s3cret"
    assert client.get("/api/projects").status_code == 200


def test_parse_plan_handles_fences_and_skips_bad_items():
    files = ["app/auth.py"]
    assert len(parse_plan("```json\n" + PLAN + "\n```", files)) == 2
    assert parse_plan("no json here", files) == []
    assert parse_plan('[{"title": "", "prompt": "x"}, 5]', files) == []
    odd = parse_plan(
        '[{"title": "a", "prompt": "b", "level": "huge", "depends_on": [7, true]}]', files
    )
    assert odd[0]["level"] == "medium"
    assert odd[0]["depends_on"] == []


def test_pipeline_errors_reach_the_page_in_turkish(client_for):
    client, _ = client_for("[]")  # the selector finds nothing
    res = ask(client, "something unrelated")
    assert res["error"].startswith("Değişecek dosya bulunamadı")


def test_turkish_error_wording():
    from tokenwise.services.messages import turkish_error

    assert "proje kontrolü hâlâ geçmiyor (a → b denendi)" in turkish_error(
        "the check still fails after trying a → b"
    )
    text = turkish_error("no applicable edit from a; last problem: SEARCH did not match")
    assert "(a denendi)" in text and "Son sorun: SEARCH did not match." in text
    assert "Son sorun" not in turkish_error("no applicable edit from a")
    assert turkish_error("something else") == "something else" and turkish_error(None) is None


def test_reopened_session_marks_proposals_that_can_no_longer_be_applied(client_for):
    client, _ = client_for('{"intent": "change", "ready": true, "questions": []}', SELECTED, EDIT)
    sid = client.post(client.api("/sessions"), json={"title": "t"}).json()["id"]
    turn = client.post(client.api(f"/sessions/{sid}/messages"),
                       json={"text": "add a length check to login", "intent": "change"}).json()  # fmt: skip
    run_id = turn["messages"][-1]["data"]["run_id"]
    proposal = lambda: client.get(client.api(f"/sessions/{sid}")).json()["messages"][-1]["data"]  # noqa: E731
    assert proposal()["pending"] is True
    client.post(client.api("/apply"), json={"run_id": run_id})
    assert proposal()["pending"] is False


def test_health_counts_open_findings_after_an_ignore(project, client_for):
    (project / "app" / "util.py").write_text(
        "import subprocess\n\n\ndef run(cmd):\n    return subprocess.call(cmd, shell=True)\n",
        "utf-8")  # fmt: skip
    client, _ = client_for()
    client.post(client.api("/health/scan"))
    before = client.get(client.api("/health")).json()
    first = before["findings"][0]["id"]
    client.post(client.api(f"/findings/{first}/status"), json={"status": "ignored"})
    after = client.get(client.api("/health")).json()
    assert after["open"] == before["open"] - 1
    assert after["latest"]["open_findings"] == before["latest"]["open_findings"]  # snapshot as was
