from __future__ import annotations

from pathlib import Path

import pytest

from tokenwise.config import Settings
from tokenwise.llm import Reply, Usage


class FakeLLM:
    """Returns queued replies and records every call, so no test spends real tokens."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[dict] = []

    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int) -> Reply:
        self.calls.append({"model": model, "system": system, "prompt": prompt})
        text = self.texts.pop(0)
        return Reply(text=text, model=model, usage=Usage(len(system + prompt) // 4, len(text) // 4))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "auth.py").write_text(
        "def login(user, password):\n"
        "    return check(user, password)\n"
        "\n"
        "\n"
        "def check(user, password):\n"
        "    return True\n",
        "utf-8",
    )
    (tmp_path / "app" / "util.py").write_text(
        "class Clock:\n    def now(self) -> int:\n        return 0\n", "utf-8"
    )
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "api.ts").write_text(
        "export async function fetchUser(id: string) {\n  return id;\n}\n"
        "export const sum = (a: number, b: number) => a + b;\n"
        "export class Store {}\n"
        "export interface User { id: string }\n",
        "utf-8",
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("function hidden() {}\n", "utf-8")
    (tmp_path / "README.md").write_text("# demo\n", "utf-8")
    return tmp_path


@pytest.fixture
def settings(project: Path) -> Settings:
    return Settings(root=project)


@pytest.fixture
def fake():
    return FakeLLM
