"""`tokenwise server selftest`: checks a running hosted setup from the outside.

`server doctor` looks at the parts (database, secrets, Docker). This goes the way GitHub and
a user's browser go: through the public address (a tunnel or a TLS proxy), so a wrong or
changed address, a webhook URL left pointing at an old tunnel, or a worker that is not
running shows up before the first real installation. It changes nothing except one `ping`
job that is removed again."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from . import db, queue
from .config import ServerConfig
from .doctor import Check

WEBHOOK_PATH = "/webhooks/github"


def check_address(server: ServerConfig, http) -> Check:
    url = f"{server.public_url}/healthz"
    try:
        response = http.get(url, timeout=15)
    except Exception as exc:  # noqa: BLE001 - any network error is the answer
        return Check("Dış adres", False, f"{url} açılmadı: {exc}. Tünel ya da proxy çalışıyor "
                                         "mu, TOKENWISE_PUBLIC_URL güncel mi?")  # fmt: skip
    if response.status_code != 200:
        detail = f"{url} → HTTP {response.status_code}: bu adreste başka bir şey yanıt veriyor."
        return Check("Dış adres", False, detail)
    return Check("Dış adres", True, f"{server.public_url} Tokenwise'a ulaşıyor")


def check_webhook(server: ServerConfig, http) -> Check:
    """Sends GitHub's `ping` event, signed like GitHub signs it, through the public address."""
    body = json.dumps({"zen": "tokenwise selftest", "hook_id": 0}).encode()
    secret = server.github.get("webhook_secret", "").encode()
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-GitHub-Event": "ping",
               "X-GitHub-Delivery": f"tokenwise-selftest-{uuid.uuid4()}",
               "X-Hub-Signature-256": signature}  # fmt: skip
    url = server.public_url + WEBHOOK_PATH
    try:
        response = http.post(url, content=body, headers=headers, timeout=15)
    except Exception as exc:  # noqa: BLE001
        return Check("Webhook", False, f"{url} açılmadı: {exc}")
    if response.status_code == 401:
        return Check("Webhook", False, "imza reddedildi: web sürecinin GITHUB_WEBHOOK_SECRET'i bu "
                                       "makinedekiyle aynı değil")  # fmt: skip
    if response.status_code != 200:
        return Check("Webhook", False, f"HTTP {response.status_code}: {response.text[:200]}")
    return Check("Webhook", True, "imzalı ping kabul edildi")


def check_app_webhook(server: ServerConfig, client) -> Check:
    """The address GitHub sends events to must be this server's, or nothing ever arrives."""
    want = server.public_url + WEBHOOK_PATH
    try:
        config = client.webhook_config()
    except Exception as exc:  # noqa: BLE001
        return Check("App webhook adresi", False, f"GitHub'dan okunamadı: {exc}")
    have = (config.get("url") or "").rstrip("/")
    if have != want:
        return Check("App webhook adresi", False,
                     f"GitHub App'te {have or '(boş)'} yazıyor; {want} olmalı. App ayarlarında "
                     "Webhook URL'i düzeltin (tünel adresi her açılışta değişir).")  # fmt: skip
    if config.get("content_type") != "json":
        return Check("App webhook adresi", False, "içerik türü 'json' olmalı (App ayarlarında "
                                                  "Content type: application/json)")  # fmt: skip
    return Check("App webhook adresi", True, want)


def check_login(server: ServerConfig, http) -> Check:
    url = f"{server.public_url}/auth/login"
    try:
        response = http.get(url, timeout=15, follow_redirects=False)
    except Exception as exc:  # noqa: BLE001
        return Check("Giriş", False, f"{url} açılmadı: {exc}")
    target = urlparse(response.headers.get("location", ""))
    client_id = parse_qs(target.query).get("client_id", [""])[0]
    if response.status_code not in (302, 303, 307) or not target.path.endswith("/authorize"):
        return Check("Giriş", False, f"GitHub'a yönlendirmedi (HTTP {response.status_code})")
    if not client_id:
        return Check("Giriş", False, "GITHUB_CLIENT_ID boş: GitHub girişi açılmaz")
    return Check("Giriş", True, "GitHub'a yönlendiriyor. App ayarlarında Callback URL "
                                f"{server.public_url}/auth/callback olmalı")  # fmt: skip


def check_worker(connect: Callable, timeout: float = 45, poll: float = 0.5) -> Check:
    """Puts a `ping` job in the queue and waits for a worker to take it."""
    conn = connect()
    try:
        job_id = queue.enqueue(conn, "ping", {})
        deadline = time.monotonic() + timeout
        status = "queued"
        while time.monotonic() < deadline:
            status = conn.execute("SELECT status FROM jobs WHERE id = %s",
                                  (job_id,)).fetchone()["status"]  # fmt: skip
            if status in ("done", "failed"):
                break
            time.sleep(poll)
        conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
    finally:
        conn.close()
    if status == "done":
        return Check("Worker", True, "kuyruktaki işi aldı ve bitirdi")
    if status == "failed":
        return Check("Worker", False, "işi aldı ama bitiremedi: docker compose logs worker")
    return Check("Worker", False, f"{int(timeout)} saniyede iş alınmadı: worker çalışıyor mu? "
                                  "docker compose ps / docker compose logs worker")  # fmt: skip


def run_selftest(
    server: ServerConfig,
    http=None,
    github_client=None,
    connect: Callable | None = None,
    worker_timeout: float = 45,
) -> list[Check]:
    import httpx

    from .github import AppConfig, GitHubApp

    http = http or httpx.Client()
    connect = connect or (lambda: db.connect(server.database_url))
    checks = [check_address(server, http)]
    if not checks[0].ok:  # nothing else can work through a dead address
        return checks
    checks.append(check_webhook(server, http))
    if github_client is None and server.github.get("app_id") and server.github.get("private_key"):
        github_client = GitHubApp(AppConfig(**server.github))
    if github_client is not None:
        checks.append(check_app_webhook(server, github_client))
    checks.append(check_login(server, http))
    checks.append(check_worker(connect, worker_timeout))
    return checks
