"""`tokenwise server doctor`: checks a hosted setup before the first real installation.

Each check says what it looked at and, when it fails, what to do. Nothing is changed except
creating the tables when they are missing (the same as `tokenwise server migrate`)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import ServerConfig


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def check_database(server: ServerConfig) -> Check:
    from . import db

    try:
        with db.connect(server.database_url) as conn:
            version = conn.execute("SHOW server_version").fetchone()["server_version"]
            db.migrate(conn)
            jobs = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    except Exception as exc:  # noqa: BLE001 - any failure is the answer
        return Check("PostgreSQL", False, f"bağlanılamadı: {exc}. DATABASE_URL'i kontrol edin.")
    major = int(version.split(".")[0]) if version.split(".")[0].isdigit() else 0
    if major < 13:
        return Check("PostgreSQL", False, f"sürüm {version}; en az 13 gerekli")
    return Check("PostgreSQL", True, f"sürüm {version}, tablolar hazır, kuyrukta {jobs} iş")


def check_secrets(server: ServerConfig) -> list[Check]:
    gh = server.github
    checks = [
        Check("TOKENWISE_SECRET_KEY", len(server.secret_key) >= 32,
              "en az 32 karakter" if len(server.secret_key) >= 32 else "çok kısa"),
        Check("GITHUB_WEBHOOK_SECRET", len(gh.get("webhook_secret", "")) >= 16,
              "tanımlı" if gh.get("webhook_secret") else "boş: webhook'lar reddedilir"),
        Check("GITHUB_CLIENT_ID / SECRET", bool(gh.get("client_id") and gh.get("client_secret")),
              "tanımlı" if gh.get("client_id") else "boş: panele giriş yapılamaz"),
    ]  # fmt: skip
    https = server.public_url.startswith("https://")
    checks.append(Check("TOKENWISE_PUBLIC_URL", https, server.public_url
                        + ("" if https else ": https değil; çerezler güvenli işaretlenmez"),
                        required=False))  # fmt: skip
    return checks


def check_github_app(server: ServerConfig, client=None) -> Check:
    """Signs a JWT with the private key and asks GitHub who the app is."""
    gh = server.github
    if not gh.get("app_id") or not gh.get("private_key"):
        return Check("GitHub App", False, "GITHUB_APP_ID ya da özel anahtar eksik")
    try:
        if client is None:
            from .github import AppConfig, GitHubApp

            client = GitHubApp(AppConfig(**gh))
        info = client.app_info()
    except Exception as exc:  # noqa: BLE001
        return Check("GitHub App", False, f"GitHub kabul etmedi: {exc}. App ID ve PEM'i "
                                          "kontrol edin.")  # fmt: skip
    events = set(info.get("events") or [])
    missing = {"push", "pull_request"} - events
    perms = info.get("permissions") or {}
    weak = [p for p in ("contents", "pull_requests", "issues") if perms.get(p) != "write"]
    problems = []
    if missing:
        problems.append("abone olunmamış olay: " + ", ".join(sorted(missing)))
    if weak:
        problems.append("yazma izni eksik: " + ", ".join(weak))
    detail = f"{info.get('name', '?')} ({info.get('installations_count', 0)} kurulum)"
    return Check("GitHub App", not problems, detail + ("; " + "; ".join(problems)
                                                       if problems else ""))  # fmt: skip


def check_docker(server: ServerConfig) -> list[Check]:
    docker = shutil.which("docker")
    if docker is None:
        return [Check("Docker", False, "docker komutu yok: proje kontrolleri çalışmaz (atlanır)")]
    info = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"],
                          capture_output=True, text=True, timeout=30)  # fmt: skip
    if info.returncode != 0:
        return [Check("Docker", False, "Docker servisine ulaşılamadı: "
                                       + info.stderr.strip()[:200])]  # fmt: skip
    checks = [Check("Docker", True, f"sunucu {info.stdout.strip()}")]
    if server.sandbox_runtime:
        runtimes = subprocess.run([docker, "info", "--format", "{{json .Runtimes}}"],
                                  capture_output=True, text=True, timeout=30).stdout  # fmt: skip
        found = f'"{server.sandbox_runtime}"' in runtimes
        checks.append(Check("Sandbox runtime", found, server.sandbox_runtime + (
            "" if found else ": Docker'da kayıtlı değil (gVisor kurulumu: docs/hosting.md)"
        )))  # fmt: skip
    checks.append(isolation_check(server, docker))
    image = subprocess.run([docker, "image", "inspect", server.sandbox_image],
                           capture_output=True, text=True, timeout=30)  # fmt: skip
    checks.append(Check("Sandbox imajı", image.returncode == 0, server.sandbox_image + (
        "" if image.returncode == 0 else f": yok. Çalıştırın: docker pull {server.sandbox_image}"
    )))  # fmt: skip
    return checks


def check_work_dir(server: ServerConfig, docker_ok: bool) -> Check:
    """The Docker host mounts check copies by path, so a copy written here must be visible to
    a container at the same path."""
    base = server.work_dir or tempfile.gettempdir()
    try:
        Path(base).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            (Path(tmp) / "probe.txt").write_text("tokenwise", "utf-8")
            if not docker_ok:
                return Check("Çalışma klasörü", True, f"{base} yazılabilir (Docker denenmedi)")
            seen = subprocess.run(
                ["docker", "run", "--rm", "--network", "none", "-v", f"{tmp}:/w:ro",
                 server.sandbox_image, "sh", "-c", "read line < /w/probe.txt; echo $line"],
                capture_output=True, text=True, timeout=120,
            )  # fmt: skip
    except OSError as exc:
        return Check("Çalışma klasörü", False, f"{base} yazılamıyor: {exc}")
    if seen.stdout.strip() != "tokenwise":
        return Check("Çalışma klasörü", False, f"{base} konteynerden görünmüyor. Worker bir "
                     "konteynerdeyse TOKENWISE_WORK_DIR host'ta da aynı yolda olmalı "
                     "(docs/hosting.md).")  # fmt: skip
    return Check("Çalışma klasörü", True, f"{base} konteynerden de görünüyor")


def isolation_check(server: ServerConfig, docker: str) -> Check:
    """Customer code runs in the checks: a container escape should not mean root on the host.
    Not required (a first install works without it), but the beta should not go live without."""
    security = subprocess.run([docker, "info", "--format", "{{json .SecurityOptions}}"],
                              capture_output=True, text=True, timeout=30).stdout  # fmt: skip
    rootless = "rootless" in security
    layers = [name for name, on in (("gVisor", server.sandbox_runtime == "runsc"),
                                    ("rootless Docker", rootless)) if on]  # fmt: skip
    if layers:
        return Check("Sandbox izolasyonu", True, " + ".join(layers), required=False)
    advice = (
        "kontroller root Docker ile host çekirdeğinde çalışıyor. Canlıya çıkmadan önce gVisor "
        "(TOKENWISE_SANDBOX_RUNTIME=runsc) ya da rootless Docker kurun ve worker'ı web'den ayrı "
        "bir makinede çalıştırın (docs/hosting.md)"
    )
    return Check("Sandbox izolasyonu", False, advice, required=False)


def run_checks(server: ServerConfig, github_client=None) -> list[Check]:
    checks = [check_database(server), *check_secrets(server),
              check_github_app(server, github_client)]  # fmt: skip
    docker = check_docker(server)
    checks += docker
    docker_ok = all(c.ok for c in docker)
    checks.append(check_work_dir(server, docker_ok))
    return checks


def ready(checks: list[Check]) -> bool:
    return all(c.ok for c in checks if c.required)
