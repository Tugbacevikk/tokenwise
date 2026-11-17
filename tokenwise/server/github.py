"""Everything the server says to GitHub, behind one small interface so tests can swap in a fake.

Installation tokens live only in memory for their hour. Git gets them through an HTTP header
for a single command, never in a remote URL, so they are not written to `.git/config`; and every
error message passes through `mask` before it is logged or stored."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

API = "https://api.github.com"
COMMENT_MARKER = "<!-- tokenwise:risk -->"
_TOKEN = re.compile(r"(gh[pousr]_[A-Za-z0-9_]{20,}|v1\.[0-9a-f]{40}|x-access-token:[^@\s]+)")
_BASIC = re.compile(r"(AUTHORIZATION: basic )[A-Za-z0-9+/=]+", re.I)


class GitHubError(Exception):
    pass


def mask(text: str) -> str:
    """Removes anything that looks like a GitHub token from a message."""
    return _BASIC.sub(r"\1***", _TOKEN.sub("***", text or ""))


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + expected, header)


class GitHub(Protocol):
    """What the worker and the web app need from GitHub."""

    def clone(self, installation_id: int, full_name: str, ref: str, target: Path) -> str: ...

    def fetch(self, installation_id: int, full_name: str, ref: str, repo: Path) -> None: ...

    def push_branch(
        self, installation_id: int, full_name: str, repo: Path, branch: str
    ) -> None: ...

    def upsert_comment(self, installation_id: int, full_name: str, number: int, body: str,
                       comment_id: int | None) -> int: ...  # fmt: skip

    def create_draft_pr(self, installation_id: int, full_name: str, head: str, base: str,
                        title: str, body: str) -> str: ...  # fmt: skip

    def installation_repos(self, installation_id: int) -> list[dict]: ...

    # sign-in (OAuth of the GitHub App)
    def login_url(self, state: str) -> str: ...

    def exchange_code(self, code: str) -> str: ...

    def user(self, token: str) -> dict: ...

    def user_installations(self, token: str) -> list[dict]: ...


def _git(args: list[str], cwd: Path | None = None, token: str = "") -> str:
    command = ["git"]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        command += ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]
    command += ["-c", "core.hooksPath=/dev/null", *args]
    try:
        done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise GitHubError("git timed out") from exc
    if done.returncode != 0:
        raise GitHubError(mask(f"git {args[0]} failed: {done.stderr.strip()[-800:]}"))
    return done.stdout


def git_clone(url: str, ref: str, target: Path, token: str = "") -> str:
    """A shallow clone of `ref` (branch, tag or commit); returns the commit."""
    target.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], target)
    _git(["remote", "add", "origin", url], target)
    _git(["fetch", "-q", "--depth", "200", "origin", ref], target, token)
    _git(["checkout", "-q", "FETCH_HEAD"], target)
    return _git(["rev-parse", "HEAD"], target).strip()


def git_fetch(url: str, ref: str, repo: Path, token: str = "") -> None:
    """Fetches `ref` as origin/<ref> so a merge base can be found."""
    _git(["fetch", "-q", "--depth", "200", url, f"+{ref}:refs/remotes/origin/{ref}"], repo, token)


def git_push(url: str, repo: Path, branch: str, token: str = "") -> None:
    _git(["push", "-q", url, f"HEAD:refs/heads/{branch}"], repo, token)


@dataclass
class AppConfig:
    app_id: str
    private_key: str  # PEM
    webhook_secret: str
    client_id: str = ""
    client_secret: str = ""
    api: str = API
    web: str = "https://github.com"


class GitHubApp:
    """The real GitHub App client."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.http = httpx.Client(base_url=config.api, timeout=30,
                                 headers={"Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28"})  # fmt: skip
        self._tokens: dict[int, tuple[str, float]] = {}

    # -- authentication
    def _jwt(self) -> str:
        import jwt

        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self.config.app_id}
        return jwt.encode(payload, self.config.private_key, algorithm="RS256")

    def app_info(self) -> dict:
        """The app as GitHub sees it: name, events, permissions, installation count."""
        return self._json(self.http.get("/app", headers={"Authorization": f"Bearer {self._jwt()}"}))

    def token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached[1] > time.time() + 120:
            return cached[0]
        response = self.http.post(f"/app/installations/{installation_id}/access_tokens",
                                  headers={"Authorization": f"Bearer {self._jwt()}"})  # fmt: skip
        data = self._json(response)
        self._tokens[installation_id] = (data["token"], time.time() + 3300)
        return data["token"]

    def _json(self, response: httpx.Response):
        if response.status_code >= 400:
            raise GitHubError(mask(f"GitHub {response.status_code}: {response.text[:300]}"))
        return response.json() if response.content else {}

    def _call(self, installation_id: int, method: str, path: str, **kwargs):
        headers = {"Authorization": f"token {self.token(installation_id)}"}
        return self._json(self.http.request(method, path, headers=headers, **kwargs))

    def _url(self, full_name: str) -> str:
        return f"{self.config.web}/{full_name}.git"

    # -- git
    def clone(self, installation_id: int, full_name: str, ref: str, target: Path) -> str:
        return git_clone(self._url(full_name), ref, target, self.token(installation_id))

    def fetch(self, installation_id: int, full_name: str, ref: str, repo: Path) -> None:
        git_fetch(self._url(full_name), ref, repo, self.token(installation_id))

    def push_branch(self, installation_id: int, full_name: str, repo: Path, branch: str) -> None:
        git_push(self._url(full_name), repo, branch, self.token(installation_id))

    # -- REST
    def upsert_comment(self, installation_id: int, full_name: str, number: int, body: str,
                       comment_id: int | None) -> int:  # fmt: skip
        body = f"{COMMENT_MARKER}\n{body}"
        if comment_id:
            try:
                data = self._call(installation_id, "PATCH",
                                  f"/repos/{full_name}/issues/comments/{comment_id}",
                                  json={"body": body})  # fmt: skip
                return data["id"]
            except GitHubError:
                pass  # deleted by someone: write a new one
        data = self._call(installation_id, "POST", f"/repos/{full_name}/issues/{number}/comments",
                          json={"body": body})  # fmt: skip
        return data["id"]

    def create_draft_pr(self, installation_id: int, full_name: str, head: str, base: str,
                        title: str, body: str) -> str:  # fmt: skip
        data = self._call(installation_id, "POST", f"/repos/{full_name}/pulls",
                          json={"title": title, "head": head, "base": base, "body": body,
                                "draft": True})  # fmt: skip
        return data["html_url"]

    def installation_repos(self, installation_id: int) -> list[dict]:
        repos, page = [], 1
        while True:
            data = self._call(installation_id, "GET", "/installation/repositories",
                              params={"per_page": 100, "page": page})  # fmt: skip
            repos += data.get("repositories", [])
            if len(data.get("repositories", [])) < 100:
                return repos
            page += 1

    # -- sign-in
    def login_url(self, state: str) -> str:
        return (f"{self.config.web}/login/oauth/authorize?client_id={self.config.client_id}"
                f"&state={state}")  # fmt: skip

    def exchange_code(self, code: str) -> str:
        response = httpx.post(f"{self.config.web}/login/oauth/access_token", timeout=30,
                              headers={"Accept": "application/json"},
                              data={"client_id": self.config.client_id,
                                    "client_secret": self.config.client_secret,
                                    "code": code})  # fmt: skip
        data = self._json(response)
        if "access_token" not in data:
            raise GitHubError("GitHub did not accept the sign-in code")
        return data["access_token"]

    def user(self, token: str) -> dict:
        return self._json(self.http.get("/user", headers={"Authorization": f"token {token}"}))

    def user_installations(self, token: str) -> list[dict]:
        data = self._json(self.http.get("/user/installations", params={"per_page": 100},
                                        headers={"Authorization": f"token {token}"}))  # fmt: skip
        return data.get("installations", [])
