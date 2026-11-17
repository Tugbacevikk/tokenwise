"""The hosted web app: GitHub webhooks in, the dashboard and its JSON API out.

Every /api route checks that the signed-in user is a member of the organization the record
belongs to; anything else answers 404, so another account's records cannot even be confirmed to
exist. Approving a fix, rejecting it and changing the model key need the owner role. Requests
that change something must carry the `X-Tokenwise: 1` header, which a cross-site form cannot.

  POST /webhooks/github                        installation, installation_repositories, push,
                                               pull_request (drafts included)
  GET  /auth/login, /auth/callback             sign in with GitHub (a session lasts 24 hours;
                                               membership is read again at every sign-in)
  POST /auth/logout, /auth/logout-all          end this session / every session
  GET  /api/me                                 the user and their organizations
  PUT  /api/orgs/{id}/model                    provider, models and key (owner)
  GET  /api/orgs/{id}/repos | /usage
  GET  /api/repos/{id}                         repository and health history
  GET  /api/repos/{id}/findings?status=        findings by priority
  PUT  /api/repos/{id}/findings/{fid}          {"status": "open" | "ignored"}
  POST /api/repos/{id}/findings/{fid}/fix      ask the Agent for a fix (needs a model key)
  POST /api/repos/{id}/scan                    scan the default branch now
  GET  /api/repos/{id}/fixes | /pulls
  GET  /api/fixes/{id};  POST /api/fixes/{id}/approve | /reject   (owner)
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from ..health import SEVERITY_WEIGHT
from . import db, queue
from .config import ServerConfig
from .github import AppConfig, GitHub, GitHubApp, GitHubError, verify_signature

PAGE = Path(__file__).parent / "static" / "dashboard.html"
SESSION_COOKIE = "tw_session"
STATE_COOKIE = "tw_state"
# Membership is read from GitHub at sign-in, so a session lasts one day: someone removed
# from a GitHub organization loses access here within a day, without storing their token.
SESSION_HOURS = 24
PR_ACTIONS = ("opened", "synchronize", "reopened", "ready_for_review", "edited")
# Requests per client address per minute. Webhooks are signed and not limited here.
RATE_LIMITS = (("/auth/", 20), ("/api/", 240))


class RateLimiter:
    """A sliding one-minute window per (client, path group), kept in this process's memory.
    With several web processes each one counts its own share, which is enough to stop one
    client from flooding the API or the sign-in."""

    def __init__(self, limits=RATE_LIMITS, clock=time.monotonic) -> None:
        self.limits, self.clock = limits, clock
        self.hits: dict[tuple[str, str], deque] = {}
        self.lock = threading.Lock()

    def check(self, client: str, path: str) -> int:
        """0 if allowed, else seconds until the next request would be."""
        group = next(((prefix, n) for prefix, n in self.limits if path.startswith(prefix)), None)
        if group is None:
            return 0
        prefix, limit = group
        now = self.clock()
        with self.lock:
            window = self.hits.setdefault((client, prefix), deque())
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= limit:
                return int(60 - (now - window[0])) + 1
            window.append(now)
            if len(self.hits) > 50_000:  # forget idle clients
                for key in [k for k, w in self.hits.items() if not w or w[-1] <= now - 60]:
                    del self.hits[key]
            return 0


class ModelBody(BaseModel):
    provider: str = "anthropic"
    base_url: str = ""
    models: list[str] = []
    api_key: str = ""


class StatusBody(BaseModel):
    status: str


# What a person can do about the errors a scan most often ends with.
_FRIENDLY = (
    ("repository is gone", "Depo bu kurulumdan kaldırılmış."),
    (
        "Repository not found",
        "GitHub depoya erişim vermedi: App'in bu depoya kurulu olduğundan emin olun.",
    ),
    ("couldn't find remote ref", "Varsayılan dal bulunamadı: depoda en az bir commit olmalı."),
    ("rate limit", "GitHub istek sınırına ulaşıldı; tarama kendiliğinden yeniden denenecek."),
    ("timed out", "İşlem zaman aşımına uğradı; depo çok büyük olabilir."),
)


def friendly(error: str | None) -> str | None:
    if not error:
        return None
    for needle, text in _FRIENDLY:
        if needle.lower() in error.lower():
            return text
    return "Tarama tamamlanamadı. Ayrıntı: " + error[:200]


def make_github(server: ServerConfig) -> GitHub:
    return GitHubApp(AppConfig(**server.github))


def create_app(
    server: ServerConfig,
    github: GitHub,
    connect: Callable | None = None,
) -> FastAPI:
    app = FastAPI(title="Tokenwise", docs_url=None, redoc_url=None)
    limiter = RateLimiter()

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        client = request.client.host if request.client else "?"
        wait = limiter.check(client, request.url.path)
        if wait:
            return JSONResponse({"detail": "Çok fazla istek; biraz sonra tekrar deneyin."},
                                status_code=429, headers={"Retry-After": str(wait)})  # fmt: skip
        return await call_next(request)

    connect = connect or (lambda: db.connect(server.database_url))
    secure = server.public_url.startswith("https://")
    # over https the cookie gets the __Host- prefix: browsers then refuse it unless it is
    # Secure, for this host only and for the whole site, so a subdomain cannot plant one
    session_cookie = f"__Host-{SESSION_COOKIE}" if secure else SESSION_COOKIE

    def get_conn():
        conn = connect()
        try:
            yield conn
        finally:
            conn.close()

    def current_user(request: Request, conn=Depends(get_conn)) -> dict:
        token = request.cookies.get(session_cookie, "")
        row = None
        if token:
            row = conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = %s AND s.expires_at > now()",
                (db.token_hash(token),),
            ).fetchone()
        if row is None:
            raise HTTPException(401, "Giriş yapın.")
        if request.method not in ("GET", "HEAD") and request.headers.get("x-tokenwise") != "1":
            raise HTTPException(403, "X-Tokenwise başlığı eksik.")
        return row

    def need_org(conn, user: dict, org_id: int, owner: bool = False) -> str:
        role = db.role_in(conn, user["id"], org_id)
        if role is None:
            raise HTTPException(404, "Bulunamadı.")
        if owner and role != "owner":
            raise HTTPException(403, "Bu işlem için organization sahibi olmalısınız.")
        return role

    def need_repo(conn, user: dict, repo_id: int, owner: bool = False) -> dict:
        repo = db.repo(conn, repo_id)
        if repo is None:
            raise HTTPException(404, "Bulunamadı.")
        need_org(conn, user, repo["org_id"], owner)
        return repo

    def need_fix(conn, user: dict, fix_id: int, owner: bool = False) -> tuple[dict, dict]:
        fix = db.fix(conn, fix_id)
        if fix is None:
            raise HTTPException(404, "Bulunamadı.")
        return fix, need_repo(conn, user, fix["repository_id"], owner)

    # ------------------------------------------------------------ pages

    @app.get("/healthz")
    def healthz(conn=Depends(get_conn)) -> dict:
        conn.execute("SELECT 1")
        return {"ok": True, "jobs": queue.counts(conn)}

    @app.get("/metrics")
    def metrics(request: Request, conn=Depends(get_conn)) -> Response:
        """Prometheus metrics, only with TOKENWISE_METRICS_TOKEN set and sent as a bearer."""
        expected = f"Bearer {server.metrics_token}"
        given = request.headers.get("authorization", "")
        if not server.metrics_token or not secrets.compare_digest(given, expected):
            raise HTTPException(404, "Not Found")
        return PlainTextResponse(db.metrics(conn), media_type="text/plain; version=0.0.4")

    @app.get("/")
    def page() -> FileResponse:
        return FileResponse(PAGE, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ webhooks

    @app.post("/webhooks/github")
    async def webhook(request: Request, conn=Depends(get_conn)) -> dict:
        body = await request.body()
        secret = server.github.get("webhook_secret", "")
        if not verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
            raise HTTPException(401, "bad signature")
        event = request.headers.get("x-github-event", "")
        delivery = request.headers.get("x-github-delivery", "")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(400, "bad json") from exc
        # The delivery is recorded in the same transaction as its effects: a redelivery of an
        # event already handled does nothing, and one that failed half way runs again.
        with conn.transaction():
            if delivery and not db.first_delivery(conn, delivery, event):
                return {"duplicate": delivery}
            result = handle_event(conn, event, payload)
        if secrets.randbelow(100) == 0:
            db.prune_deliveries(conn)
        return result

    # ------------------------------------------------------------ sign-in

    @app.get("/auth/login")
    def login() -> RedirectResponse:
        state = secrets.token_urlsafe(24)
        response = RedirectResponse(github.login_url(state))
        response.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, secure=secure,
                            samesite="lax")  # fmt: skip
        return response

    @app.get("/auth/callback")
    def callback(request: Request, code: str = "", state: str = "",
                 conn=Depends(get_conn)) -> RedirectResponse:  # fmt: skip
        expected = request.cookies.get(STATE_COOKIE, "")
        if not code or not state or not secrets.compare_digest(state, expected):
            raise HTTPException(400, "Oturum açma isteği geçersiz; tekrar deneyin.")
        try:
            token = github.exchange_code(code)
            gh_user = github.user(token)
            installations = github.user_installations(token)
        except GitHubError as exc:
            raise HTTPException(502, str(exc)) from exc
        with db.tx(conn):
            db.upsert_user(conn, gh_user["id"], gh_user["login"])
            reachable = []
            for inst in installations:
                org = db.org_by_installation(conn, inst["id"])
                if org is None:
                    continue
                reachable.append(org["id"])
                personal = inst.get("account", {}).get("id") == gh_user["id"]
                db.set_member(conn, org["id"], gh_user["id"], "owner" if personal else "member")
            # GitHub no longer lets this person reach these installations: no longer a member
            conn.execute("DELETE FROM members WHERE user_id = %s AND NOT (org_id = ANY(%s))",
                         (gh_user["id"], reachable))  # fmt: skip
            conn.execute("DELETE FROM sessions WHERE expires_at < now()")
            session = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                (db.token_hash(session), gh_user["id"], db.now() + timedelta(hours=SESSION_HOURS)),
            )
        response = RedirectResponse("/")
        response.set_cookie(session_cookie, session, max_age=SESSION_HOURS * 3600, path="/",
                            httponly=True, secure=secure, samesite="lax")  # fmt: skip
        response.delete_cookie(STATE_COOKIE)
        return response

    @app.post("/auth/logout")
    def logout(request: Request, conn=Depends(get_conn)) -> Response:
        if request.headers.get("x-tokenwise") != "1":
            raise HTTPException(403, "X-Tokenwise başlığı eksik.")
        token = request.cookies.get(session_cookie, "")
        if token:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (db.token_hash(token),))
        response = JSONResponse({"ok": True})
        response.delete_cookie(session_cookie, path="/", secure=secure, httponly=True)
        return response

    @app.post("/auth/logout-all")
    def logout_all(user=Depends(current_user), conn=Depends(get_conn)) -> Response:
        """Ends every session of this person, on every device."""
        ended = conn.execute("DELETE FROM sessions WHERE user_id = %s", (user["id"],)).rowcount
        response = JSONResponse({"ended": ended})
        response.delete_cookie(session_cookie, path="/", secure=secure, httponly=True)
        return response

    # ------------------------------------------------------------ API

    @app.get("/api/me")
    def me(user=Depends(current_user), conn=Depends(get_conn)) -> dict:
        return {"user": {"id": user["id"], "login": user["login"]},
                "orgs": db.orgs_of(conn, user["id"])}  # fmt: skip

    @app.put("/api/orgs/{org_id}/model")
    def set_model(org_id: int, body: ModelBody, user=Depends(current_user),
                  conn=Depends(get_conn)) -> dict:  # fmt: skip
        need_org(conn, user, org_id, owner=True)
        if body.provider not in ("anthropic", "openai"):
            raise HTTPException(400, "Sağlayıcı anthropic ya da openai olmalı.")
        if body.provider == "openai" and body.base_url not in server.allowed_model_urls:
            raise HTTPException(400, "Bu adrese bu sunucuda izin verilmiyor.")
        models = [m.strip() for m in body.models if m.strip()][:5]
        conn.execute(
            "UPDATE organizations SET model_provider = %s, model_base_url = %s, models = %s"
            " WHERE id = %s",
            (body.provider, body.base_url if body.provider == "openai" else "", models, org_id),
        )
        if body.api_key:
            conn.execute("UPDATE organizations SET model_key = %s WHERE id = %s",
                         (server.encrypt(body.api_key), org_id))  # fmt: skip
        return {"ok": True}

    @app.get("/api/orgs/{org_id}/repos")
    def repos(org_id: int, user=Depends(current_user), conn=Depends(get_conn)) -> list[dict]:
        need_org(conn, user, org_id)
        return conn.execute(
            "SELECT r.id, r.full_name, r.default_branch, r.overall, r.last_scan_at,"
            " (SELECT COUNT(*) FROM findings f WHERE f.repository_id = r.id"
            "  AND f.status = 'open') AS open_findings,"
            " s.status AS scan_state, s.error AS scan_error"
            " FROM repositories r LEFT JOIN LATERAL ("
            "  SELECT status, error FROM jobs j WHERE j.kind = 'scan'"
            "  AND j.payload->>'repository_id' = r.id::text ORDER BY j.id DESC LIMIT 1"
            " ) s ON true WHERE r.org_id = %s ORDER BY r.full_name",
            (org_id,),
        ).fetchall()

    @app.get("/api/orgs/{org_id}/usage")
    def org_usage(org_id: int, days: int = 30, user=Depends(current_user),
                  conn=Depends(get_conn)) -> dict:  # fmt: skip
        need_org(conn, user, org_id)
        return db.usage(conn, org_id, max(1, min(days, 365)))

    @app.get("/api/repos/{repo_id}")
    def repo_detail(repo_id: int, user=Depends(current_user), conn=Depends(get_conn)) -> dict:
        repo = need_repo(conn, user, repo_id)
        role = db.role_in(conn, user["id"], repo["org_id"])
        scan = conn.execute(
            "SELECT status, error FROM jobs WHERE kind = 'scan'"
            " AND payload->>'repository_id' = %s ORDER BY id DESC LIMIT 1",
            (str(repo_id),),
        ).fetchone()
        return {
            "scan": {"state": scan["status"], "error": friendly(scan["error"])} if scan else None,
            "repository": {
                k: repo[k]
                for k in ("id", "full_name", "default_branch", "overall", "last_scan_at", "org_id")
            },  # fmt: skip
            "role": role,
            "snapshots": db.snapshots(conn, repo_id),
        }

    @app.get("/api/repos/{repo_id}/findings")
    def repo_findings(repo_id: int, status: str = "open", user=Depends(current_user),
                      conn=Depends(get_conn)) -> list[dict]:  # fmt: skip
        need_repo(conn, user, repo_id)
        rows = db.findings(conn, repo_id, None if status == "all" else status)
        return sorted(rows, key=lambda f: (-SEVERITY_WEIGHT[f["severity"]], f["id"]))

    @app.put("/api/repos/{repo_id}/findings/{finding_id}")
    def finding_status(repo_id: int, finding_id: int, body: StatusBody,
                       user=Depends(current_user), conn=Depends(get_conn)) -> dict:  # fmt: skip
        need_repo(conn, user, repo_id)
        if body.status not in ("open", "ignored"):
            raise HTTPException(400, "Durum open ya da ignored olmalı.")
        row = db.set_finding_status(conn, repo_id, finding_id, body.status)
        if row is None:
            raise HTTPException(404, "Bulunamadı.")
        return row

    @app.post("/api/repos/{repo_id}/findings/{finding_id}/fix")
    def request_fix(repo_id: int, finding_id: int, user=Depends(current_user),
                    conn=Depends(get_conn)) -> dict:  # fmt: skip
        repo = need_repo(conn, user, repo_id)
        finding = db.finding(conn, repo_id, finding_id)
        if finding is None or finding["status"] != "open":
            raise HTTPException(404, "Açık bir bulgu bulunamadı.")
        org = conn.execute("SELECT model_key FROM organizations WHERE id = %s",
                           (repo["org_id"],)).fetchone()  # fmt: skip
        if not org or org["model_key"] is None:
            raise HTTPException(400, "Önce organization ayarlarından bir model anahtarı girin.")
        with db.tx(conn):
            fix, created = db.create_fix(conn, repo_id, finding_id, user["id"])
            if created:
                queue.enqueue(conn, "fix_propose", {"fix_id": fix["id"]}, repo["org_id"],
                              dedupe_key=f"fix:{fix['id']}")  # fmt: skip
        return fix

    @app.post("/api/repos/{repo_id}/scan")
    def scan_now(repo_id: int, user=Depends(current_user), conn=Depends(get_conn)) -> dict:
        repo = need_repo(conn, user, repo_id)
        job = queue.enqueue(conn, "scan", {"repository_id": repo_id, "scan_type": "manual"},
                            repo["org_id"], dedupe_key=f"scan:{repo_id}")  # fmt: skip
        return {"job": job}

    @app.get("/api/repos/{repo_id}/fixes")
    def repo_fixes(repo_id: int, user=Depends(current_user), conn=Depends(get_conn)) -> list:
        need_repo(conn, user, repo_id)
        return db.fixes(conn, repo_id)

    @app.get("/api/repos/{repo_id}/pulls")
    def repo_pulls(repo_id: int, user=Depends(current_user), conn=Depends(get_conn)) -> list:
        need_repo(conn, user, repo_id)
        return db.pull_requests(conn, repo_id)

    @app.get("/api/fixes/{fix_id}")
    def get_fix(fix_id: int, user=Depends(current_user), conn=Depends(get_conn)) -> dict:
        fix, _ = need_fix(conn, user, fix_id)
        return fix

    @app.post("/api/fixes/{fix_id}/approve")
    def approve(fix_id: int, user=Depends(current_user), conn=Depends(get_conn)) -> dict:
        fix, repo = need_fix(conn, user, fix_id, owner=True)
        with db.tx(conn):
            moved = db.move_fix(conn, fix_id, "proposed", "approved", approved_by=user["id"])
            if moved is None:
                raise HTTPException(409, f"Bu düzeltme onaylanamaz (durum: {fix['status']}).")
            queue.enqueue(conn, "fix_push", {"fix_id": fix_id}, repo["org_id"],
                          dedupe_key=f"fix:{fix_id}")  # fmt: skip
        return moved

    @app.post("/api/fixes/{fix_id}/reject")
    def reject(fix_id: int, user=Depends(current_user), conn=Depends(get_conn)) -> dict:
        fix, _ = need_fix(conn, user, fix_id, owner=True)
        moved = db.move_fix(conn, fix_id, "proposed", "rejected")
        if moved is None:
            raise HTTPException(409, f"Bu düzeltme reddedilemez (durum: {fix['status']}).")
        return moved

    return app


# ---------------------------------------------------------------- webhook events


def handle_event(conn, event: str, payload: dict) -> dict:
    installation = (payload.get("installation") or {}).get("id")
    action = payload.get("action", "")
    if event == "ping":
        return {"ok": True}
    if event == "installation":
        if action == "deleted":
            with db.tx(conn):
                gone = db.delete_org(conn, installation)
            return {"deleted": gone}
        if action in ("created", "new_permissions_accepted", "unsuspend"):
            return _install(conn, payload)
        return {"ignored": action}
    org = db.org_by_installation(conn, installation) if installation else None
    if org is None:
        return {"ignored": "unknown installation"}
    if event == "installation_repositories":
        with db.tx(conn):
            for item in payload.get("repositories_removed", []):
                db.remove_repo(conn, item["id"])
            added = []
            for item in payload.get("repositories_added", []):
                added.append(db.upsert_repo(conn, org["id"], item["id"], item["full_name"]))
            if added:
                queue.enqueue(conn, "sync", {"org_id": org["id"]}, org["id"],
                              dedupe_key=f"sync:{org['id']}")  # fmt: skip
        return {"added": len(added)}
    repository = payload.get("repository") or {}
    if not repository:
        return {"ignored": event}
    branch = repository.get("default_branch") or "main"
    if event == "push":
        if payload.get("deleted") or payload.get("ref") != f"refs/heads/{branch}":
            return {"ignored": "not the default branch"}
        with db.tx(conn):
            repo_id = db.upsert_repo(conn, org["id"], repository["id"], repository["full_name"],
                                     branch)  # fmt: skip
            job = queue.enqueue(conn, "scan", {"repository_id": repo_id, "ref": payload["after"],
                                               "event_at": event_time(repository.get("pushed_at"))},
                                org["id"], dedupe_key=f"scan:{repo_id}")  # fmt: skip
        return {"job": job}
    if event == "pull_request":
        if action not in PR_ACTIONS:
            return {"ignored": action}
        pr = payload["pull_request"]
        if action == "edited" and "base" not in (payload.get("changes") or {}):
            return {"ignored": "edited without a new base"}
        if pr.get("state") != "open":
            return {"ignored": "closed"}
        with db.tx(conn):
            repo_id = db.upsert_repo(conn, org["id"], repository["id"], repository["full_name"],
                                     branch)  # fmt: skip
            job = queue.enqueue(
                conn, "pr_risk",
                {"repository_id": repo_id, "number": pr["number"], "head_sha": pr["head"]["sha"],
                 "base_ref": pr["base"]["ref"], "event_at": event_time(pr.get("updated_at"))},
                org["id"], dedupe_key=f"pr:{repo_id}:{pr['number']}",
            )  # fmt: skip
        return {"job": job}
    return {"ignored": event}


def event_time(value) -> str:
    """GitHub sends times as ISO text or as Unix seconds; one sortable UTC form for both."""
    from datetime import UTC, datetime

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
        return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


def _install(conn, payload: dict) -> dict:
    inst = payload["installation"]
    if db.installation_removed(conn, inst["id"]):
        return {"ignored": "installation was removed"}
    account = inst["account"]
    sender = payload.get("sender") or {}
    with db.tx(conn):
        org_id = db.upsert_org(conn, inst["id"], account["login"], account["id"],
                               account.get("type", "User"))  # fmt: skip
        if sender.get("id"):  # whoever installed the app owns the organization here
            db.upsert_user(conn, sender["id"], sender["login"])
            db.set_member(conn, org_id, sender["id"], "owner")
        for item in payload.get("repositories", []):
            db.upsert_repo(conn, org_id, item["id"], item["full_name"])
        queue.enqueue(conn, "sync", {"org_id": org_id}, org_id, dedupe_key=f"sync:{org_id}")
    return {"org": org_id}


def main() -> None:  # pragma: no cover - started by `tokenwise server web`
    import uvicorn

    server = ServerConfig.from_env()
    conn = db.connect(server.database_url)
    db.migrate(conn)
    conn.close()
    app = create_app(server, make_github(server))
    uvicorn.run(app, host="0.0.0.0", port=8000, proxy_headers=True)  # noqa: S104
