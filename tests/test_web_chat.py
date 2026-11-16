from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tokenwise.llm import Reply, Usage  # noqa: E402
from tokenwise.web import create_app  # noqa: E402

ROUTE_CHANGE = '{"intent": "change", "ready": true, "questions": []}'
ROUTE_EXPLAIN = '{"intent": "explain", "ready": true, "questions": []}'
ROUTE_PLAN = '{"intent": "plan", "ready": true, "questions": []}'
ROUTE_REFINE = '{"intent": "refine", "ready": true, "questions": []}'
ROUTE_ASK = '{"intent": "change", "ready": false, "questions": ["Minimum uzunluk kaç olsun?"]}'
SELECTED = '["app/auth.py"]'
EDIT = (
    "app/auth.py\n<<<<<<< SEARCH\n    return True\n=======\n"
    "    return len(password) >= 8\n>>>>>>> REPLACE\n"
)
EDIT_STRICT = (
    "app/auth.py\n<<<<<<< SEARCH\n    return True\n=======\n"
    "    return len(password) >= 12\n>>>>>>> REPLACE\n"
)
PLAN = (
    '[{"title": "Şifre kontrolü", "prompt": "app/auth.py check fonksiyonuna kontrol ekle.", '
    '"files": ["app/auth.py"], "check": "", "level": "easy", "depends_on": []},'
    ' {"title": "Test", "prompt": "Test ekle.", "files": ["app/auth.py"], "check": "",'
    ' "level": "easy", "depends_on": [0]}]'
)
ONE_THREAD = (
    '[{"title": "Şifre kontrolü", "prompt": "check fonksiyonunu düzelt.", '
    '"files": ["app/auth.py"], "check": "", "level": "easy", "depends_on": []}]'
)


class Scripted:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.prompts: list[str] = []

    def complete(self, *, model, system, prompt, max_tokens):
        self.prompts.append(prompt)
        # Retries (failed project checks) reuse the last scripted reply.
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return Reply(text=text, model=model, usage=Usage(500, len(text) // 4))


@pytest.fixture
def chat_for(project: Path):
    def make(*texts: str):
        llm = Scripted(*texts)
        client = TestClient(create_app(project, llm_factory=lambda s: llm))
        pid = client.get("/api/projects").json()[0]["id"]
        session = client.post(f"/api/projects/{pid}/sessions", json={"title": "test"}).json()
        base = f"/api/projects/{pid}/sessions/{session['id']}/messages"

        def send(text: str, **extra) -> dict:
            return client.post(base, json={"text": text, **extra}).json()

        return client, llm, pid, session["id"], send

    return make


def kinds(turn: dict) -> list[str]:
    return [m["kind"] for m in turn["messages"]]


def test_a_clear_request_becomes_a_proposal(project, chat_for):
    client, llm, pid, sid, send = chat_for(ROUTE_CHANGE, SELECTED, EDIT)
    turn = send("login'e şifre uzunluk kontrolü ekle")
    assert kinds(turn) == ["text", "proposal"]
    assert turn["messages"][1]["data"]["ok"] is True
    assert "return True" in (project / "app" / "auth.py").read_text("utf-8")  # not applied yet
    saved = client.get(f"/api/projects/{pid}/sessions/{sid}").json()
    assert [m["kind"] for m in saved["messages"]] == ["text", "proposal"]
    assert saved["last_proposal"]["files"] == ["app/auth.py"]


def test_a_vague_request_gets_questions_and_the_answer_continues_it(chat_for):
    client, llm, pid, sid, send = chat_for(ROUTE_ASK, SELECTED, EDIT)
    first = send("şifreyi kontrol et")
    assert kinds(first) == ["text", "question"]
    assert first["messages"][1]["questions"] == ["Minimum uzunluk kaç olsun?"]
    assert len(llm.prompts) == 1  # only the router so far

    second = send("8 karakter")
    assert kinds(second) == ["text", "proposal"]
    assert "şifreyi kontrol et" in llm.prompts[-1]  # the original request is kept
    assert "Ek bilgi: 8 karakter" in llm.prompts[-1]


def test_an_explain_question_edits_nothing(project, chat_for):
    client, llm, pid, sid, send = chat_for(ROUTE_EXPLAIN, "check True döndürüyor.")
    turn = send("check ne yapıyor?")
    assert kinds(turn) == ["text", "explain"]
    assert turn["messages"][1]["content"] == "check True döndürüyor."
    assert "return True" in (project / "app" / "auth.py").read_text("utf-8")


def test_a_big_goal_becomes_a_plan(chat_for):
    client, llm, pid, sid, send = chat_for(ROUTE_PLAN, PLAN)
    turn = send("login güvenliğini baştan ele al")
    assert kinds(turn) == ["text", "plan"]
    plan_id = turn["messages"][1]["plan_id"]
    assert client.get(f"/api/projects/{pid}/plans/{plan_id}").json()["goal"]


def test_refine_changes_the_previous_proposal(chat_for):
    client, llm, pid, sid, send = chat_for(ROUTE_CHANGE, SELECTED, EDIT, ROUTE_REFINE, EDIT_STRICT)
    send("şifre kontrolü ekle")
    turn = send("8 yerine 12 olsun")
    assert kinds(turn) == ["text", "proposal"]
    assert "Önceki öneri" in llm.prompts[-1]  # the editor sees what was proposed before
    assert "12 olsun" in llm.prompts[-1]
    assert turn["messages"][1]["data"]["changes"][0]["new"].count("12") == 1


def test_refine_without_a_proposal_is_a_change(chat_for):
    client, llm, pid, sid, send = chat_for(ROUTE_REFINE, SELECTED, EDIT)
    turn = send("bunu biraz değiştir")
    assert kinds(turn) == ["text", "proposal"]


def test_apply_runs_the_project_check(project, chat_for):
    (project / "tokenwise.toml").write_text(
        '[tokenwise]\ncheck = "{python} -c \\"import sys; sys.exit(1)\\""\n', "utf-8"
    )
    client, llm, pid, sid, send = chat_for(ROUTE_CHANGE, SELECTED, EDIT, EDIT)
    run_id = send("şifre kontrolü ekle")["messages"][1]["run_id"]
    applied = client.post(f"/api/projects/{pid}/apply", json={"run_id": run_id}).json()
    assert applied["applied"] == ["app/auth.py"]
    assert applied["check"]["passed"] is False
    assert "len(password)" in (project / "app" / "auth.py").read_text("utf-8")


def test_apply_without_check_reports_none(chat_for):
    client, llm, pid, sid, send = chat_for(ROUTE_CHANGE, SELECTED, EDIT)
    run_id = send("şifre kontrolü ekle")["messages"][1]["run_id"]
    assert (
        client.post(f"/api/projects/{pid}/apply", json={"run_id": run_id}).json()["check"] is None
    )


def test_review_reads_the_proposal(chat_for):
    client, llm, pid, sid, send = chat_for(
        ROUTE_CHANGE, SELECTED, EDIT, "- Şifre boşlukları da sayıyor."
    )
    run_id = send("şifre kontrolü ekle")["messages"][1]["run_id"]
    review = client.post(f"/api/projects/{pid}/runs/{run_id}/review").json()
    assert review["review"] == "- Şifre boşlukları da sayıyor."
    assert client.post(f"/api/projects/{pid}/runs/999/review").status_code == 404


def test_plan_can_be_changed_with_an_instruction_and_keeps_statuses(chat_for):
    client, llm, pid, sid, send = chat_for(PLAN, ONE_THREAD)
    plan = client.post(f"/api/projects/{pid}/plan", json={"goal": "güvenlik"}).json()
    client.put(f"/api/projects/{pid}/plans/{plan['id']}/threads/0", json={"status": "done"})
    changed = client.post(
        f"/api/projects/{pid}/plans/{plan['id']}/chat", json={"text": "testi çıkar"}
    ).json()
    assert len(changed["threads"]) == 1
    assert changed["threads"][0]["title"] == "Şifre kontrolü"
    assert changed["threads"][0]["status"] == "done"  # same title, status kept
    assert (
        client.get(f"/api/projects/{pid}/plans/{plan['id']}").json()["threads"][0]["status"]
        == "done"
    )


def test_sessions_are_listed_and_kept(chat_for):
    client, llm, pid, sid, send = chat_for(ROUTE_EXPLAIN, "cevap")
    send("soru")
    listed = client.get(f"/api/projects/{pid}/sessions").json()
    assert listed[0]["id"] == sid and listed[0]["count"] == 2
    assert client.get(f"/api/projects/{pid}/sessions/nope").status_code == 404


def test_unknown_intent_is_refused(chat_for):
    client, llm, pid, sid, send = chat_for()
    response = send("x", intent="uydur")
    assert response.get("detail")  # HTTP 400 with a message
    assert (
        client.post(
            f"/api/projects/{pid}/sessions/{sid}/messages", json={"text": "x", "intent": "uydur"}
        ).status_code
        == 400
    )


def test_a_model_error_becomes_an_error_message(project):
    class Broken:
        def complete(self, **kwargs):
            from tokenwise.llm import LLMError

            raise LLMError("server down")

    client = TestClient(create_app(project, llm_factory=lambda s: Broken()))
    pid = client.get("/api/projects").json()[0]["id"]
    sid = client.post(f"/api/projects/{pid}/sessions", json={}).json()["id"]
    turn = client.post(
        f"/api/projects/{pid}/sessions/{sid}/messages", json={"text": "x", "intent": "explain"}
    ).json()
    assert kinds(turn) == ["text", "error"]
    assert "server down" in turn["messages"][1]["content"]


def test_costs_count_the_runs_by_method_and_day(chat_for):
    client, llm, pid, sid, send = chat_for(ROUTE_CHANGE, SELECTED, EDIT)
    send("şifre kontrolü ekle")
    costs = client.get(f"/api/projects/{pid}/costs").json()
    assert costs["runs"] == 2  # the routing call (chat) and the change (tokenwise)
    assert costs["by_method"]["tokenwise"] > 0
    assert costs["by_method"]["chat"] > 0
    assert sum(d["cost_usd"] for d in costs["by_day"]) == pytest.approx(costs["total_usd"])
    assert client.get(f"/api/projects/{pid}/costs?days=0").json()["days"] == 1
