from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("required,exit_code", [("0", 0), ("1", 1)])
@pytest.mark.parametrize("filename", ["test_missing_browser.py", "test_interaction.py"])
def test_ci_rejects_browser_tests_that_would_silently_skip(
    tmp_path: Path, required: str, exit_code: int, filename: str,
) -> None:
    shutil.copy(Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / filename).write_text(
        "import pytest\ndef test_interaction():\n    pytest.skip('browser missing')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", filename],
        cwd=tmp_path, env={**os.environ, "CI_REQUIRE_BROWSER": required},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    if required == "1":
        assert "CI 必须实际执行浏览器测试" in result.stdout


@pytest.mark.parametrize("inline_close", [False, True])
def test_browser_failure_produces_screenshot_trace_and_console(
    tmp_path: Path, inline_close: bool,
) -> None:
    shutil.copy(Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    code = '''
import pytest
@pytest.fixture
def page():
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except api.Error:
            pytest.skip("Chrome unavailable")
        try:
            page = browser.new_page()
            page.set_content("<h1>Synthetic failure</h1>")
            yield page
        finally:
            browser.close()
def test_failure(page):
    page.evaluate("console.error('synthetic browser log')")
    assert False, "intentional fixture failure"
'''
    if inline_close:
        code = code.replace("@pytest.fixture\ndef page():", "def test_failure():").replace(
            "            yield page",
            "            page.evaluate(\"console.error('synthetic browser log')\")\n"
            "            assert False, 'intentional inline failure'",
        ).split("def test_failure(page):")[0]
    (tmp_path / "test_failed_interaction.py").write_text(code, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_failed_interaction.py"],
        cwd=tmp_path, env=os.environ.copy(), capture_output=True, text=True, timeout=45,
    )
    if result.returncode == 0 and "1 skipped" in result.stdout:
        pytest.skip("Local Chrome is required for failure artifact verification")
    assert result.returncode == 1, result.stdout + result.stderr
    artifacts = tmp_path / "test-results" / "browser"
    assert list(artifacts.rglob("*.png")), result.stdout + result.stderr
    assert list(artifacts.rglob("trace-*.zip"))
    logs = list(artifacts.rglob("console.txt"))
    assert logs and "synthetic browser log" in logs[0].read_text()
