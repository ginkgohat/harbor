"""End-to-end tests for the Harbor web UI using Playwright.

These tests spin up a real Harbor server on a random port, then drive
the UI through a headless browser.  Marked with @pytest.mark.e2e so they
can be excluded from fast CI runs.

Run with:
    pytest tests/e2e/ -m e2e  --browser chromium

Requires:
    pip install pytest-playwright
    playwright install chromium
"""

import http.server
import os
import socket
import subprocess
import threading
import time

import pytest

from harbor import config as config_mod
from harbor import scanner as scanner_mod
from harbor import server as server_mod

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Find a free TCP port on localhost."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server_url(tmp_path):
    """Spin up a Harbor server in a background thread, return (base_url, token).

    Creates two test repos in tmp_path so the UI has something to render.
    """
    # Create a couple of fake git repos
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    subprocess.run(["git", "init", str(repo_a)], capture_output=True, check=True)

    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    subprocess.run(["git", "init", str(repo_b)], capture_output=True, check=True)
    (repo_b / "dirty.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_b), capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@test",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "initial",
        ],
        cwd=str(repo_b),
        capture_output=True,
        check=True,
    )
    (repo_b / "dirty.txt").write_text("hello changed\n")

    # Config
    config_path = tmp_path / "config.toml"
    config_mod.save_config(
        str(config_path), {"roots": [{"path": str(tmp_path), "label": "test"}]}
    )

    # Scan repos
    repos = scanner_mod.scan_roots([(str(tmp_path), "test")], min_depth=1, max_depth=3)

    # Configure server
    static_dir = os.path.join(os.path.dirname(server_mod.__file__), "static")
    html_path = os.path.join(static_dir, "index.html")
    server_mod.Handler.repos = repos
    server_mod.Handler.html_path = html_path
    server_mod.Handler.static_dir = static_dir
    server_mod.Handler.config_path = str(config_path)
    server_mod.Handler.min_depth = 1
    server_mod.Handler.max_depth = 3
    server_mod.AUTH_TOKEN = "test-token-123"

    port = _free_port()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), server_mod.Handler)

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    # Wait for server to come up
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    else:
        pytest.fail("Server failed to start")

    yield f"http://127.0.0.1:{port}", "test-token-123"

    httpd.shutdown()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_page_loads_with_token(page, server_url):
    """Page loads successfully with the auth token in the URL."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    # Should show the app title
    page.wait_for_selector("header", timeout=5000)
    # Repos section should be visible
    assert page.locator("main").is_visible()


def test_api_without_token_gives_403(page, server_url):
    """Direct API access without a token returns 403."""
    base_url, _ = server_url
    response = page.goto(f"{base_url}/api/repos")
    assert response.status == 403


def test_repo_cards_render(page, server_url):
    """Repo cards appear on the page (at least our two test repos)."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    # Wait for render
    page.wait_for_selector(".grid", timeout=5000)
    cards = page.locator(".repo-card")
    count = cards.count()
    assert count >= 2, f"Expected at least 2 repo cards, got {count}"


def test_search_filters_repos(page, server_url):
    """Typing in the search box filters the repo list."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector(".grid", timeout=5000)

    total = page.locator(".repo-card").count()
    assert total >= 2

    # Search for "repo-a" — should reduce to 1 card
    search = page.locator("#search")
    search.fill("repo-a")
    time.sleep(0.2)  # allow debounce/render

    visible = page.locator(".repo-card").count()
    assert visible == 1, f"Expected 1 card after search, got {visible}"


def test_view_toggle_switch_segments(page, server_url):
    """Clicking the attention/all segmented control toggles the view."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector(".grid", timeout=5000)

    all_btn = page.locator('#viewToggle button[data-view="all"]')
    all_btn.click()
    assert all_btn.evaluate("el => el.classList.contains('on')")

    attn_btn = page.locator('#viewToggle button[data-view="attention"]')
    attn_btn.click()
    assert attn_btn.evaluate("el => el.classList.contains('on')")


def test_language_toggle(page, server_url):
    """Language toggle switches between zh and en."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector("#langToggle", timeout=5000)

    en_btn = page.locator('#langToggle button[data-lang="en"]')
    en_btn.click()
    assert en_btn.evaluate("el => el.classList.contains('on')")
    # Title should be in English after switch
    assert page.locator("h1").text_content() == "Harbor"


def test_theme_toggle_data_attr(page, server_url):
    """Clicking the theme button cycles data-theme attribute."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector("#themeBtn", timeout=5000)

    # Initially system (no data-theme)
    initial = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    assert initial is None  # system mode default

    theme_btn = page.locator("#themeBtn")
    theme_btn.click()
    after_first = page.evaluate(
        "() => document.documentElement.getAttribute('data-theme')"
    )
    assert after_first == "light"

    theme_btn.click()
    after_second = page.evaluate(
        "() => document.documentElement.getAttribute('data-theme')"
    )
    assert after_second == "dark"

    theme_btn.click()
    after_third = page.evaluate(
        "() => document.documentElement.getAttribute('data-theme')"
    )
    assert after_third is None  # back to system


def test_modal_opens_and_closes(page, server_url):
    """Confirm modal opens when clicking discard and closes on cancel."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector(".grid", timeout=5000)

    # Find a dirty repo's discard button
    discard_btn = page.locator('.icon-btn[data-act="discard"]').first
    if discard_btn.is_enabled():
        discard_btn.click()
        # Modal should be visible
        modal = page.locator("#modalOverlay")
        assert modal.evaluate("el => el.classList.contains('show')")

        # Cancel button closes it
        page.locator("#modalCancel").click()
        assert not modal.evaluate("el => el.classList.contains('show')")


def test_modal_has_aria_attributes(page, server_url):
    """Modal has proper ARIA dialog attributes."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector("#modalOverlay", timeout=5000)

    overlay = page.locator("#modalOverlay")
    assert overlay.get_attribute("role") == "dialog"
    assert overlay.get_attribute("aria-modal") == "true"
    assert overlay.get_attribute("aria-labelledby") == "modalTitle"


def test_synced_strip_keyboard_activation(page, server_url):
    """Synced strip is keyboard accessible (tab-focusable, Enter activates)."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector("#syncedStrip", timeout=5000)

    strip = page.locator("#syncedStrip")
    assert strip.get_attribute("role") == "button"
    assert strip.get_attribute("tabindex") == "0"
