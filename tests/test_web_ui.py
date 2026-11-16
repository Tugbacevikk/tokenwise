"""Opens the page in a real browser and walks through a plan. Skipped unless Playwright and its
Chromium are installed: `pip install playwright && playwright install chromium`."""

import socket
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
sync_api = pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402

from tests.conftest import FakeLLM  # noqa: E402
from tokenwise.web import create_app  # noqa: E402

PLAN = (
    '[{"title": "Şifre", "prompt": "app/auth.py check fonksiyonuna kontrol ekle", '
    '"files": ["app/auth.py"], "check": "", "level": "easy", "depends_on": []},'
    ' {"title": "Test", "prompt": "test ekle", "files": ["app/auth.py"], "check": "",'
    ' "level": "easy", "depends_on": [0]}]'
)
EDIT = (
    "app/auth.py\n<<<<<<< SEARCH\n    return True\n=======\n"
    "    return len(password) >= 8\n>>>>>>> REPLACE\n"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(project):
    llm = FakeLLM(PLAN, '["app/auth.py"]', EDIT)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(project, llm_factory=lambda s: llm),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/", project
    server.should_exit = True
    thread.join(timeout=5)


def test_plan_thread_is_applied_in_the_browser(served):
    url, project = served
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # the browser binary is not installed here
            pytest.skip(f"Chromium is not installed: {exc}")
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#files")
        page.click("#mode-plan")
        page.fill("#input", "login güvenliği")
        page.click("#send")
        page.wait_for_selector(".plan")
        assert page.locator(".thread").count() == 2
        assert page.locator(".thread >> nth=1 >> button.primary").is_disabled()

        page.locator(".thread >> nth=0 >> button.primary").click()
        page.wait_for_selector("text=Uygula")
        page.click("text=Uygula")
        page.wait_for_selector("text=Uygulandı.")
        assert page.locator(".thread >> nth=1 >> button.primary").is_enabled()
        browser.close()
    assert not errors
    assert "len(password)" in (project / "app" / "auth.py").read_text("utf-8")
