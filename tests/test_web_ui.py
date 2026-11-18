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


def start(app):
    """Runs the app on a free port in a thread; returns the server and its URL."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server, f"http://127.0.0.1:{port}/"


@pytest.fixture
def served(project):
    llm = FakeLLM(PLAN, '["app/auth.py"]', EDIT)
    server, url = start(create_app(project, llm_factory=lambda s: llm))
    yield url, project
    server.should_exit = True


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


ROUTE_ASK = '{"intent": "change", "ready": false, "questions": ["Minimum uzunluk kaç olsun?"]}'
ROUTE_EXPLAIN = '{"intent": "explain", "ready": true, "questions": []}'


def open_page(browser, url):
    page = browser.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector("#files")
    return page, errors


def test_chat_asks_a_question_then_applies_the_change(project):
    llm = FakeLLM(ROUTE_ASK, '["app/auth.py"]', EDIT)
    server, url = start(create_app(project, llm_factory=lambda s: llm))
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            server.should_exit = True
            pytest.skip(f"Chromium is not installed: {exc}")
        page, errors = open_page(browser, url)
        page.fill("#input", "şifreyi kontrol et")
        page.click("#send")
        page.wait_for_selector("text=Minimum uzunluk kaç olsun?")
        page.fill("#input", "8 karakter")
        page.click("#send")
        page.wait_for_selector("text=Uygula")
        page.click("text=Uygula")
        page.wait_for_selector("text=Uygulandı.")
        browser.close()
    server.should_exit = True
    assert not errors
    assert "len(password)" in (project / "app" / "auth.py").read_text("utf-8")


def test_chat_explains_without_editing(project):
    llm = FakeLLM(ROUTE_EXPLAIN, "check True döndürüyor.")
    server, url = start(create_app(project, llm_factory=lambda s: llm))
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            server.should_exit = True
            pytest.skip(f"Chromium is not installed: {exc}")
        page, errors = open_page(browser, url)
        page.fill("#input", "check ne yapıyor?")
        page.click("#send")
        page.wait_for_selector("text=check True döndürüyor.")
        assert page.locator("text=Uygula").count() == 0
        browser.close()
    server.should_exit = True
    assert not errors
    assert "return True" in (project / "app" / "auth.py").read_text("utf-8")


def test_health_findings_and_risk_in_the_browser(tmp_path):
    from tests.test_platform import COMPLEX, commit, sh

    sh(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "start", {"app/core.py": "def f():\n    return 1\n"})
    (tmp_path / "app" / "rules.py").write_text(COMPLEX)
    server, url = start(create_app(tmp_path, llm_factory=lambda s: FakeLLM()))
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            server.should_exit = True
            pytest.skip(f"Chromium is not installed: {exc}")
        page, errors = open_page(browser, url)
        page.click("#scan-btn")
        page.wait_for_selector("text=Bulguları göster")
        assert "/100" in page.inner_text("#health-score")
        page.click("#findings-btn")
        page.wait_for_selector("text=Agent ile düzelt")
        page.fill("#risk-base", "main")
        page.click("#risk-btn")
        page.wait_for_selector("text=Yayın öncesi kontrol listesi")
        browser.close()
    server.should_exit = True
    assert not errors


def test_plan_threads_can_be_skipped_reopened_and_marked_done(served):
    url, _ = served
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        page, errors = open_page(browser, url)
        assert page.is_visible("#intent")
        page.click("#mode-plan")
        assert not page.is_visible("#intent")  # the intent only means something in chat mode
        page.fill("#input", "login güvenliği")
        page.click("#send")
        page.wait_for_selector(".plan")
        first, second = page.locator(".thread >> nth=0"), page.locator(".thread >> nth=1")
        first.locator("text=Atla").click()
        page.wait_for_selector(".thread.skipped")
        first.locator("text=Geri aç").click()
        page.wait_for_selector(".thread.pending >> nth=1")
        first.locator("text=Bitti say").click()
        page.wait_for_selector(".thread.done")
        assert second.locator("button.primary").is_enabled()  # its dependency is now done
        browser.close()
    assert not errors


def test_side_by_side_toggle_and_reopened_session(project):
    llm = FakeLLM('{"intent": "change", "ready": true, "questions": []}', '["app/auth.py"]', EDIT)
    server, url = start(create_app(project, llm_factory=lambda s: llm))
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            server.should_exit = True
            pytest.skip(f"Chromium is not installed: {exc}")
        page, errors = open_page(browser, url)
        page.fill("#input", "şifre uzunluğunu kontrol et")
        page.click("#send")
        page.wait_for_selector("text=Uygula")
        page.check("#sbs")  # existing diffs switch to side by side
        assert page.locator("#log .sbs").count() == 1 and page.locator("#log pre.diff").count() == 0
        page.uncheck("#sbs")
        assert page.locator("#log pre.diff").count() == 1
        page.click("text=Uygula")
        page.wait_for_selector("text=Uygulandı.")
        page.reload()  # the last chat comes back; its applied proposal cannot be applied again
        page.wait_for_selector("text=Bu öneri artık uygulanamaz")
        assert page.locator("#log button:has-text('Uygula')").count() == 0
        browser.close()
    server.should_exit = True
    assert not errors
