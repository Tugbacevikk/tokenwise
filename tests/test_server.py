"""The hosted server end to end on a real PostgreSQL, with real git repositories standing in for
GitHub (a fake client clones from and pushes to local bare repositories). No test calls a real
model or GitHub.

Needs TOKENWISE_TEST_DATABASE_URL, e.g. postgresql://postgres@localhost:5432/tokenwise_test;
without it these tests are skipped. With TOKENWISE_DOCKER_IMAGE the fix's check really runs in a
container; without it the check is reported as skipped."""

import hashlib
import hmac
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tests.conftest import FakeLLM  # noqa: E402
from tokenwise.server import db, queue  # noqa: E402
from tokenwise.server.app import create_app, handle_event  # noqa: E402
from tokenwise.server.config import ServerConfig, repo_settings  # noqa: E402
from tokenwise.server.github import (  # noqa: E402
    COMMENT_MARKER,
    git_clone,
    git_fetch,
    git_push,
    mask,
    verify_signature,
)
from tokenwise.server.worker import Worker  # noqa: E402

DATABASE_URL = os.getenv("TOKENWISE_TEST_DATABASE_URL", "")
DOCKER_IMAGE = os.getenv("TOKENWISE_DOCKER_IMAGE", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="set TOKENWISE_TEST_DATABASE_URL")

SECRET = "webhook-secret"
INSTALLATION = 4242
ALICE = {"id": 1, "login": "alice"}  # installed the app: owner
BOB = {"id": 2, "login": "bob"}  # member of the GitHub organization
CAROL = {"id": 3, "login": "carol"}  # has nothing to do with it
ACCOUNT = {"id": 100, "login": "acme", "type": "Organization"}
SHELL_CODE = "import subprocess\n\n\ndef run(cmd):\n    return subprocess.call(cmd, shell=True)\n"
FIX_REPLY = (
    "app/tasks.py\n<<<<<<< SEARCH\n    return subprocess.call(cmd, shell=True)\n=======\n"
    "    return subprocess.call(cmd.split())\n>>>>>>> REPLACE\n"
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


class FakeGitHub:
    """GitHub, as far as the server can tell: local bare repositories and recorded calls."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repos: dict[str, dict] = {}
        self.comments: dict[int, dict] = {}
        self.prs: list[dict] = []
        self.users: dict[str, dict] = {}  # code -> user
        self.installations: dict[int, list[dict]] = {}  # user id -> installations

    def add_repo(self, github_id: int, full_name: str, files: dict[str, str]) -> Path:
        bare = self.root / f"{full_name}.git"
        work = self.root / "work" / full_name
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
        work.mkdir(parents=True)
        git(work, "init", "-q", "-b", "main")
        for name, text in files.items():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(text, "utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "start")
        git(work, "push", "-q", str(bare), "main")
        self.repos[full_name] = {"id": github_id, "full_name": full_name, "bare": bare,
                                 "work": work, "default_branch": "main"}  # fmt: skip
        return work

    def url(self, full_name: str) -> str:
        return self.repos[full_name]["bare"].as_uri()

    def clone(self, installation_id, full_name, ref, target):
        return git_clone(self.url(full_name), ref, target)

    def fetch(self, installation_id, full_name, ref, repo):
        git_fetch(self.url(full_name), ref, repo)

    def push_branch(self, installation_id, full_name, repo, branch):
        git_push(self.url(full_name), repo, branch)

    def upsert_comment(self, installation_id, full_name, number, body, comment_id):
        if comment_id in self.comments:
            self.comments[comment_id]["body"] = COMMENT_MARKER + "\n" + body
            self.comments[comment_id]["edits"] += 1
            return comment_id
        new_id = 9000 + len(self.comments)
        self.comments[new_id] = {"number": number, "body": COMMENT_MARKER + "\n" + body,
                                 "edits": 0}  # fmt: skip
        return new_id

    def create_draft_pr(self, installation_id, full_name, head, base, title, body):
        self.prs.append({"head": head, "base": base, "title": title, "body": body, "draft": True})
        return f"https://github.com/{full_name}/pull/{100 + len(self.prs)}"

    def installation_repos(self, installation_id):
        return [{"id": r["id"], "full_name": r["full_name"],
                 "default_branch": r["default_branch"]} for r in self.repos.values()]  # fmt: skip

    def login_url(self, state):
        return f"https://github.example/login?state={state}"

    def exchange_code(self, code):
        return "token-" + code

    def user(self, token):
        return self.users[token.removeprefix("token-")]

    def user_installations(self, token):
        return self.installations.get(self.user(token)["id"], [])


@pytest.fixture
def conn():
    connection = db.connect(DATABASE_URL)
    connection.execute("DROP SCHEMA public CASCADE")
    connection.execute("CREATE SCHEMA public")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def server(tmp_path) -> ServerConfig:
    return ServerConfig(
        database_url=DATABASE_URL,
        secret_key="s" * 40,
        public_url="https://tokenwise.example",
        sandbox_image=DOCKER_IMAGE or "tokenwise-test-missing-image",
        scan_tools=(),
        github={"webhook_secret": SECRET},
        work_dir=str(tmp_path / "jobs"),
    )


@pytest.fixture
def gh(tmp_path) -> FakeGitHub:
    fake = FakeGitHub(tmp_path / "github")
    fake.add_repo(555, "acme/app", {
        "app/__init__.py": "",
        "app/tasks.py": SHELL_CODE,
        "app/api.py": "from app.tasks import run\n\n\ndef handle(x):\n    return run(x)\n",
        "tests/test_api.py": "def test_ok():\n    assert True\n",
        # untrusted: only `check` (and file choices) may be taken from here
        "tokenwise.toml": '[tokenwise]\nprovider = "openai"\nbase_url = "http://evil.example"\n'
                          'sandbox = "local"\ncheck = "{python} -c \\"import app.tasks\\""\n',
    })  # fmt: skip
    for user in (ALICE, BOB, CAROL):
        fake.users[user["login"]] = user
    installation = {"id": INSTALLATION, "account": ACCOUNT}
    fake.installations = {ALICE["id"]: [installation], BOB["id"]: [installation]}
    return fake


@pytest.fixture
def setup(conn, server, gh, tmp_path):
    (tmp_path / "jobs").mkdir()
    app = create_app(server, gh, connect=lambda: db.connect(DATABASE_URL))
    model = {"llm": FakeLLM(FIX_REPLY), "keys": []}

    def factory(settings, key):
        model["keys"].append((settings.provider, settings.base_url, key))
        return model["llm"]

    worker = Worker(conn, gh, server, "w1", model_factory=factory)
    return app, worker, model


def signed(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    return body, "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def send(client: TestClient, event: str, payload: dict):
    body, signature = signed(payload)
    return client.post("/webhooks/github", content=body,
                       headers={"X-GitHub-Event": event, "X-Hub-Signature-256": signature,
                                "Content-Type": "application/json"})  # fmt: skip


def drain(worker: Worker) -> list[dict]:
    done = []
    while (job := worker.run_once()) is not None:
        done.append(job)
    return done


def login(app, user: dict) -> TestClient:
    client = TestClient(app, base_url="https://tokenwise.example")
    start = client.get("/auth/login", follow_redirects=False)
    state = start.headers["location"].split("state=")[1]
    done = client.get(f"/auth/callback?code={user['login']}&state={state}", follow_redirects=False)
    assert done.status_code in (302, 307), done.text
    client.headers["X-Tokenwise"] = "1"
    return client


def install(client: TestClient) -> None:
    payload = {"action": "created", "installation": {"id": INSTALLATION, "account": ACCOUNT},
               "sender": ALICE, "repositories": [{"id": 555, "full_name": "acme/app"}]}  # fmt: skip
    assert send(client, "installation", payload).status_code == 200


# ---------------------------------------------------------------- pieces


def test_migrate_twice_is_harmless(conn):
    db.migrate(conn)
    assert conn.execute("SELECT version FROM schema_version").fetchall() == [{"version": 1}]


def test_signature_and_masking():
    body = b'{"a": 1}'
    good = "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert verify_signature("k", body, good)
    assert not verify_signature("k", body + b" ", good)
    assert not verify_signature("", body, good)
    text = "fatal: https://x-access-token:ghs_abcdefghijklmnopqrstuvwxyz0123@github.com/a/b"
    assert "ghs_" not in mask(text) and "***" in mask(text)
    assert "c2VjcmV0" not in mask("-c http.extraheader=AUTHORIZATION: basic c2VjcmV0")


def test_key_encryption(server):
    sealed = server.encrypt("sk-ant-secret")
    assert b"sk-ant" not in sealed and server.decrypt(sealed) == "sk-ant-secret"
    other = ServerConfig(database_url="", secret_key="x" * 40)
    assert other.decrypt(sealed) == ""


def test_repository_config_cannot_change_the_server(tmp_path, server):
    (tmp_path / "tokenwise.toml").write_text(
        '[tokenwise]\nprovider = "ollama"\nbase_url = "http://169.254.169.254"\nsandbox = "local"\n'
        'sandbox_image = "evil"\nscan_tools = []\ncheck = "pytest"\ncheck_timeout = 99999\n'
        'extensions = [".py"]\n', "utf-8")  # fmt: skip
    settings = repo_settings(tmp_path, server, {"model_provider": "anthropic", "models": ["m1"]})
    assert settings.provider == "anthropic" and settings.base_url == ""
    assert settings.sandbox == "docker" and settings.sandbox_image == server.sandbox_image
    assert settings.scan_tools == server.scan_tools and settings.models == ("m1",)
    assert settings.check == "pytest" and settings.check_timeout == 900
    assert settings.extensions == (".py",) and settings.cache is False


def test_queue_two_workers_never_take_the_same_job(conn):
    ids = [queue.enqueue(conn, "noop", {"n": n}) for n in range(40)]
    taken: list[int] = []
    lock = threading.Lock()

    def work(name: str) -> None:
        mine = db.connect(DATABASE_URL)
        while (job := queue.claim(mine, name)) is not None:
            with lock:
                taken.append(job["id"])
            queue.finish(mine, job["id"])
        mine.close()

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(taken) == sorted(ids)


def test_queue_dedupe_retry_and_stale_jobs(conn):
    first = queue.enqueue(conn, "scan", {"ref": "a"}, dedupe_key="scan:1")
    assert queue.enqueue(conn, "scan", {"ref": "b"}, dedupe_key="scan:1") == first
    job = queue.claim(conn, "w")
    assert job["id"] == first and job["payload"] == {"ref": "b"}  # the newest input
    newer = queue.enqueue(conn, "scan", {"ref": "c"}, dedupe_key="scan:1")
    assert newer != first  # new work may queue while the old one runs
    assert queue.fail(conn, job, "boom") == "superseded"  # newer covers it; no retry

    job = queue.claim(conn, "w")
    assert job["id"] == newer
    assert queue.fail(conn, job, "boom") == "queued"
    row = conn.execute("SELECT * FROM jobs WHERE id = %s", (newer,)).fetchone()
    assert row["run_after"] > db.now() and row["error"] == "boom"
    assert queue.claim(conn, "w") is None  # not before its delay
    assert queue.fail(conn, {**job, "attempts": queue.MAX_ATTEMPTS}, "boom") == "failed"

    stuck = queue.enqueue(conn, "x", {})
    conn.execute("UPDATE jobs SET status = 'running', locked_at = now() - interval '2 hours'"
                 " WHERE id = %s", (stuck,))  # fmt: skip
    assert queue.claim(conn, "w2")["id"] == stuck


def test_bad_signature_and_unknown_installation(setup):
    app, _, _ = setup
    client = TestClient(app)
    body = b'{"action": "created"}'
    bad = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "installation", "X-Hub-Signature-256": "sha256=0"},
    )
    assert bad.status_code == 401
    out = send(client, "push", {"installation": {"id": 1}, "repository": {"id": 1}})
    assert out.json() == {"ignored": "unknown installation"}


# ---------------------------------------------------------------- the whole flow


def test_hosted_flow(setup, gh, conn):
    app, worker, model = setup
    hooks = TestClient(app)
    install(hooks)

    # sync learns the default branch and queues the first scan
    jobs = drain(worker)
    assert [j["kind"] for j in jobs] == ["sync", "scan"]
    assert all(j["status"] == "done" for j in jobs), [j.get("error") for j in jobs]

    alice, bob, carol = login(app, ALICE), login(app, BOB), login(app, CAROL)
    orgs = alice.get("/api/me").json()["orgs"]
    assert [(o["account_login"], o["role"]) for o in orgs] == [("acme", "owner")]
    assert bob.get("/api/me").json()["orgs"][0]["role"] == "member"
    assert carol.get("/api/me").json()["orgs"] == []
    org_id = orgs[0]["id"]
    (repo,) = alice.get(f"/api/orgs/{org_id}/repos").json()
    assert repo["full_name"] == "acme/app" and repo["overall"] is not None

    # another account's records do not exist for carol
    assert carol.get(f"/api/orgs/{org_id}/repos").status_code == 404
    assert carol.get(f"/api/repos/{repo['id']}").status_code == 404
    assert carol.get(f"/api/repos/{repo['id']}/findings").status_code == 404
    assert TestClient(app).get(f"/api/repos/{repo['id']}").status_code == 401

    findings = bob.get(f"/api/repos/{repo['id']}/findings").json()
    shell = next(f for f in findings if f["rule"] == "shell-command")
    assert shell["file"] == "app/tasks.py"

    # a push to the default branch scans again; a push elsewhere does not
    work = gh.repos["acme/app"]["work"]
    (work / "app" / "extra.py").write_text("eval('1')\n", "utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "extra")
    git(work, "push", "-q", str(gh.repos["acme/app"]["bare"]), "main")
    repository = {"id": 555, "full_name": "acme/app", "default_branch": "main"}
    inst = {"id": INSTALLATION}
    head = git(work, "rev-parse", "HEAD")
    side = send(hooks, "push", {"ref": "refs/heads/dev", "after": head, "repository": repository,
                                "installation": inst})  # fmt: skip
    assert "ignored" in side.json()
    send(hooks, "push", {"ref": "refs/heads/main", "after": head, "repository": repository,
                         "installation": inst})  # fmt: skip
    assert [j["kind"] for j in drain(worker)] == ["scan"]
    rules = {f["rule"] for f in bob.get(f"/api/repos/{repo['id']}/findings").json()}
    assert "dynamic-code" in rules
    assert len(alice.get(f"/api/repos/{repo['id']}").json()["snapshots"]) == 2

    # a draft PR gets one risk comment, updated (not duplicated) on the next push
    git(work, "checkout", "-q", "-b", "feature")
    (work / "app" / "tasks.py").write_text(SHELL_CODE + "\n\ndef more():\n    return 1\n", "utf-8")
    git(work, "commit", "-qam", "more")
    git(work, "push", "-q", str(gh.repos["acme/app"]["bare"]), "feature")

    def pr_event(action: str) -> dict:
        sha = git(work, "rev-parse", "HEAD")
        return {"action": action, "installation": inst, "repository": repository,
                "pull_request": {"number": 7, "state": "open", "draft": True,
                                 "head": {"sha": sha}, "base": {"ref": "main"}}}  # fmt: skip

    send(hooks, "pull_request", pr_event("opened"))
    jobs = drain(worker)
    assert [j["status"] for j in jobs] == ["done"], [j.get("error") for j in jobs]
    (comment,) = gh.comments.values()
    assert comment["number"] == 7 and "ReleaseGuard risk raporu" in comment["body"]
    assert "app/tasks.py" in comment["body"]
    (work / "app" / "tasks.py").write_text(SHELL_CODE + "\n\ndef more():\n    return 2\n", "utf-8")
    git(work, "commit", "-qam", "more 2")
    git(work, "push", "-q", str(gh.repos["acme/app"]["bare"]), "feature")
    send(hooks, "pull_request", pr_event("synchronize"))
    drain(worker)
    assert len(gh.comments) == 1 and next(iter(gh.comments.values()))["edits"] == 1
    pulls = bob.get(f"/api/repos/{repo['id']}/pulls").json()
    assert pulls[0]["number"] == 7 and pulls[0]["head_sha"] == git(work, "rev-parse", "HEAD")

    # fixes need a model key; only the owner may set it, and only with the header
    fix_url = f"/api/repos/{repo['id']}/findings/{shell['id']}/fix"
    assert bob.post(fix_url).status_code == 400
    body = {"provider": "anthropic", "models": ["cheap-model"], "api_key": "sk-ant-org-key"}
    assert bob.put(f"/api/orgs/{org_id}/model", json=body).status_code == 403
    assert alice.put(f"/api/orgs/{org_id}/model", json=body,
                     headers={"X-Tokenwise": ""}).status_code == 403  # fmt: skip
    assert alice.put(f"/api/orgs/{org_id}/model", json=body).status_code == 200
    evil = {"provider": "openai", "base_url": "http://169.254.169.254", "models": ["m"]}
    assert alice.put(f"/api/orgs/{org_id}/model", json=evil).status_code == 400
    stored = conn.execute("SELECT model_key FROM organizations").fetchone()["model_key"]
    assert b"sk-ant-org-key" not in bytes(stored)

    fix = bob.post(fix_url).json()
    assert fix["status"] == "queued"
    jobs = drain(worker)
    assert [j["status"] for j in jobs] == ["done"], [j.get("error") for j in jobs]
    assert model["keys"] == [("anthropic", "", "sk-ant-org-key")]
    proposed = bob.get(f"/api/fixes/{fix['id']}").json()
    assert proposed["status"] == "proposed", proposed
    assert "cmd.split()" in proposed["changes"][0]["diff"]
    check = proposed["summary"]["check"]
    if DOCKER_IMAGE:
        assert check["passed"] is True
    else:
        assert check["passed"] is None and "docker" in check["output"]
    assert conn.execute("SELECT COUNT(*) AS n FROM model_usage").fetchone()["n"] == 1

    # only the owner approves; the default branch is never written
    main_before = git(gh.repos["acme/app"]["bare"], "rev-parse", "main")
    assert bob.post(f"/api/fixes/{fix['id']}/approve").status_code == 403
    assert carol.post(f"/api/fixes/{fix['id']}/approve").status_code == 404
    assert alice.post(f"/api/fixes/{fix['id']}/approve").status_code == 200
    assert alice.post(f"/api/fixes/{fix['id']}/approve").status_code == 409
    jobs = drain(worker)
    assert [j["status"] for j in jobs] == ["done"], [j.get("error") for j in jobs]
    pushed = alice.get(f"/api/fixes/{fix['id']}").json()
    assert pushed["status"] == "pushed" and pushed["branch"] == f"tokenwise/fix-{fix['id']}"
    assert gh.prs == [dict(gh.prs[0], head=pushed["branch"], base="main", draft=True)]
    bare = gh.repos["acme/app"]["bare"]
    assert git(bare, "rev-parse", "main") == main_before
    fixed = git(bare, "show", f"{pushed['branch']}:app/tasks.py")
    assert "cmd.split()" in fixed and "shell=True" not in fixed
    message = git(bare, "log", "-1", "--format=%B", pushed["branch"])
    assert "Co-Authored-By: tokenwise" in message

    # ignoring is a person's decision and survives the next scan
    assert bob.put(f"/api/repos/{repo['id']}/findings/{shell['id']}",
                   json={"status": "ignored"}).json()["status"] == "ignored"  # fmt: skip
    alice.post(f"/api/repos/{repo['id']}/scan")
    drain(worker)
    ignored = bob.get(f"/api/repos/{repo['id']}/findings?status=ignored").json()
    assert [f["id"] for f in ignored] == [shell["id"]]

    # removing the app removes everything of this organization
    send(
        hooks,
        "installation",
        {"action": "deleted", "installation": {"id": INSTALLATION, "account": ACCOUNT}},
    )
    for table in ("organizations", "members", "repositories", "findings", "scans", "snapshots",
                  "pull_requests", "fixes", "model_usage", "jobs"):  # fmt: skip
        assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0, table
    assert alice.get("/api/me").json()["orgs"] == []


def test_fix_push_refuses_a_proposal_for_other_content(setup, gh, conn):
    app, worker, model = setup
    install(TestClient(app))
    drain(worker)
    repo = conn.execute("SELECT * FROM repositories").fetchone()
    finding = conn.execute("SELECT * FROM findings WHERE rule = 'shell-command'").fetchone()
    fix = db.create_fix(conn, repo["id"], finding["id"], ALICE["id"])
    head = git(gh.repos["acme/app"]["bare"], "rev-parse", "main")
    db.update_fix(conn, fix["id"], status="approved", base_sha=head,
                  changes=[{"path": "app/tasks.py", "old": "not this", "new": "x"}])  # fmt: skip
    queue.enqueue(conn, "fix_push", {"fix_id": fix["id"]})
    (job,) = drain(worker)
    assert job["status"] == "failed"
    row = db.fix(conn, fix["id"])
    assert row["status"] == "failed" and "not what the proposal" in row["error"]
    assert gh.prs == []
    db.update_fix(conn, fix["id"], status="approved",
                  changes=[{"path": "../outside.py", "old": None, "new": "x"}])  # fmt: skip
    queue.enqueue(conn, "fix_push", {"fix_id": fix["id"]})
    (job,) = drain(worker)
    assert "outside the repository" in db.fix(conn, fix["id"])["error"]


def test_handle_event_ignores_closed_prs_and_edits_without_new_base(conn):
    db.upsert_org(conn, INSTALLATION, "acme", 100, "Organization")
    base = {"installation": {"id": INSTALLATION},
            "repository": {"id": 1, "full_name": "acme/x", "default_branch": "main"},
            "pull_request": {"number": 1, "state": "closed", "head": {"sha": "a"},
                             "base": {"ref": "main"}}}  # fmt: skip
    assert handle_event(conn, "pull_request", {**base, "action": "closed"})["ignored"] == "closed"
    assert "ignored" in handle_event(conn, "pull_request", {**base, "action": "edited"})
    open_pr = {**base, "action": "opened"}
    open_pr["pull_request"] = {**base["pull_request"], "state": "open"}
    assert "job" in handle_event(conn, "pull_request", open_pr)
