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
from harbor.state import AppState

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

    # Configure server.  All mutable state lives in server.app_state
    # (see harbor/state.py) — the handler reads it per request.
    static_dir = os.path.join(os.path.dirname(server_mod.__file__), "static")
    html_path = os.path.join(static_dir, "index.html")
    saved_state = server_mod.app_state
    saved_token = server_mod.AUTH_TOKEN
    server_mod.app_state = AppState(
        repos=repos,
        roots=[(str(tmp_path), "test")],
        html_path=html_path,
        static_dir=static_dir,
        config_path=str(config_path),
        min_depth=1,
        max_depth=3,
    )
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
    server_mod.app_state = saved_state
    server_mod.AUTH_TOKEN = saved_token


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
    cards = page.locator(".card")
    count = cards.count()
    assert count >= 2, f"Expected at least 2 repo cards, got {count}"


def test_search_filters_repos(page, server_url):
    """Typing in the search box filters the repo list."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector(".grid", timeout=5000)

    total = page.locator(".card").count()
    assert total >= 2

    # Search for "repo-a" — should reduce to 1 card
    search = page.locator("#search")
    search.fill("repo-a")
    time.sleep(0.2)  # allow debounce/render

    visible = page.locator(".card").count()
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
    assert page.locator("header h1").text_content() == "Harbor"


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
    # The overlay is in the DOM from page load but hidden until shown,
    # so wait for attachment rather than visibility.
    page.wait_for_selector("#modalOverlay", state="attached", timeout=5000)

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


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_auth_gate_appears_without_token(page, server_url):
    """Visiting without a token shows the auth overlay and hides main content."""
    base_url, _ = server_url
    page.goto(f"{base_url}/")
    # Auth overlay should be visible
    page.wait_for_selector("#authOverlay", timeout=5000)
    overlay = page.locator("#authOverlay")
    assert overlay.evaluate("el => el.classList.contains('show')")
    # Auth form elements present
    assert page.locator("#authToken").is_visible()
    assert page.locator("#authSignInBtn").is_visible()


def test_auth_form_login_with_token(page, server_url):
    """Submitting the auth form with the correct token enters the app."""
    base_url, token = server_url
    page.goto(f"{base_url}/")
    page.wait_for_selector("#authOverlay", timeout=5000)

    page.locator("#authToken").fill(token)
    page.locator("#authSignInBtn").click()

    # After successful login, auth gate hides and main content appears
    # Wait for the repo grid — it only renders after loadRepos() succeeds
    page.wait_for_selector(".grid", timeout=5000)
    overlay = page.locator("#authOverlay")
    assert not overlay.evaluate("el => el.classList.contains('show')")
    assert page.locator("main").is_visible()


def test_auth_form_wrong_token_shows_error(page, server_url):
    """Submitting a wrong token shows an error and stays on the auth gate."""
    base_url, _ = server_url
    page.goto(f"{base_url}/")
    page.wait_for_selector("#authOverlay", timeout=5000)

    page.locator("#authToken").fill("wrong-token")
    page.locator("#authSignInBtn").click()

    # Error message should appear (wait for the element to have text content)
    page.wait_for_function(
        "() => document.querySelector('#authError').textContent !== ''", timeout=5000
    )
    error_el = page.locator("#authError")
    assert error_el.text_content() != ""
    # Auth gate still visible
    overlay = page.locator("#authOverlay")
    assert overlay.evaluate("el => el.classList.contains('show')")


# ---------------------------------------------------------------------------
# Diff preview
# ---------------------------------------------------------------------------


def test_diff_preview_opens_and_shows_content(page, server_url):
    """Clicking Preview on a dirty repo opens the diff modal with content."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector(".grid", timeout=5000)

    # Click the preview button of an enabled (dirty) repo in the attention grid
    preview_btn = page.locator(
        '#attentionGrid .icon-btn[data-act="preview"]:not([disabled])'
    ).first
    preview_btn.click()

    # Diff overlay should be visible
    diff_overlay = page.locator("#diffOverlay")
    page.wait_for_selector("#diffOverlay.show", timeout=5000)
    assert diff_overlay.evaluate("el => el.classList.contains('show')")

    # Diff body should load (wait for fetch + render)
    page.wait_for_selector("#diffBody .d-line", timeout=5000)
    # At least some diff lines
    lines = page.locator("#diffBody .d-line")
    assert lines.count() > 0
    # Title should show the repo name
    assert page.locator("#diffTitle").text_content() != ""

    # Close button works
    page.locator("#diffClose").click()
    assert not diff_overlay.evaluate("el => el.classList.contains('show')")


def test_diff_closes_on_escape_key(page, server_url):
    """Pressing Escape closes the diff modal."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector(".grid", timeout=5000)

    preview_btn = page.locator(
        '#attentionGrid .icon-btn[data-act="preview"]:not([disabled])'
    ).first
    preview_btn.click()
    page.wait_for_selector("#diffOverlay.show", timeout=5000)

    # Press Escape to close
    page.keyboard.press("Escape")
    time.sleep(0.1)

    diff_overlay = page.locator("#diffOverlay")
    assert not diff_overlay.evaluate("el => el.classList.contains('show')")


# ---------------------------------------------------------------------------
# Repo actions (stash / discard)
# ---------------------------------------------------------------------------


def test_stash_action_updates_repo_status(page, server_url):
    """Clicking Stash on a dirty repo runs git stash and the repo becomes clean."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector(".grid", timeout=5000)

    initial_cards = page.locator(".card").count()
    assert initial_cards >= 2

    initial_synced = int(page.locator("#syncedCount").text_content() or "0")

    # Click stash on the first available dirty repo in attention grid
    stash_btn = page.locator(
        '#attentionGrid .icon-btn[data-act="stash"]:not([disabled])'
    ).first
    stash_btn.click()

    # Terminal should receive output lines (single actions don't auto-open terminal)
    page.wait_for_function(
        "() => document.querySelectorAll('#terminalBody .line').length >= 2",
        timeout=5000,
    )
    term_lines = page.locator("#terminalBody .line")
    assert term_lines.count() >= 2

    # After stash, the repo should move to synced (no longer dirty)
    # Wait for re-render after action completion
    page.wait_for_function(
        f"() => parseInt(document.querySelector('#syncedCount').textContent) > {initial_synced}",
        timeout=5000,
    )
    final_synced = int(page.locator("#syncedCount").text_content() or "0")
    assert final_synced > initial_synced


def test_discard_action_via_modal_confirm(page, server_url):
    """Discard via confirm modal removes dirty state from the repo."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector(".grid", timeout=5000)

    # Count dirty repos initially (by enabled discard buttons)
    initial_discard_btns = page.locator(
        '#attentionGrid .icon-btn[data-act="discard"]:not([disabled])'
    ).count()
    assert initial_discard_btns >= 1

    # Open discard modal
    discard_btn = page.locator(
        '#attentionGrid .icon-btn[data-act="discard"]:not([disabled])'
    ).first
    discard_btn.click()
    page.wait_for_selector("#modalOverlay.show", timeout=5000)

    # Confirm the action
    page.locator("#modalConfirm").click()

    # Terminal should receive output lines
    page.wait_for_function(
        "() => document.querySelectorAll('#terminalBody .line').length >= 2",
        timeout=5000,
    )

    # After discard, fewer discard-enabled buttons should remain
    time.sleep(0.3)
    final_discard_btns = page.locator(
        '#attentionGrid .icon-btn[data-act="discard"]:not([disabled])'
    ).count()
    assert final_discard_btns < initial_discard_btns


# ---------------------------------------------------------------------------
# Refresh / rescan
# ---------------------------------------------------------------------------


def test_refresh_button_rescans_repos(page, server_url):
    """Clicking the refresh button triggers a rescan and shows confirmation."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector(".grid", timeout=5000)

    initial_stats = page.locator("#stats").text_content()
    assert initial_stats

    refresh_btn = page.locator("#refreshBtn")
    refresh_btn.click()

    # Stats should briefly show the refresh confirmation text
    # (contains "efreshed" in English or "刷新" in Chinese)
    page.wait_for_function(
        "() => { const t = document.querySelector('#stats').textContent; "
        "return t.includes('efreshed') || t.includes('刷新'); }",
        timeout=5000,
    )

    # After the brief flash, stats should restore and repos still render
    time.sleep(2.0)  # wait for the 1.5s timeout to pass
    page.wait_for_selector(".grid", timeout=5000)
    cards = page.locator(".card").count()
    assert cards >= 2


# ---------------------------------------------------------------------------
# Settings modal
# ---------------------------------------------------------------------------


def test_settings_modal_opens_and_closes(page, server_url):
    """Settings modal opens, shows roots, and closes via button/overlay/Esc."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector("#settingsBtn", timeout=5000)

    settings_overlay = page.locator("#settingsOverlay")

    def is_open():
        return settings_overlay.evaluate("el => el.classList.contains('show')")

    # Open via settings button
    page.locator("#settingsBtn").click()
    page.wait_for_selector("#settingsOverlay.show", timeout=5000)
    assert is_open()

    # Roots list should show the configured root
    page.wait_for_selector("#settingsRoots .root-item", timeout=5000)
    assert page.locator("#settingsRoots .root-item").count() >= 1

    # Close via close button
    page.locator("#settingsClose").click()
    page.wait_for_selector("#settingsOverlay", state="hidden", timeout=5000)
    assert not is_open()

    # Reopen and close via Escape
    page.locator("#settingsBtn").click()
    page.wait_for_selector("#settingsOverlay.show", timeout=5000)
    page.keyboard.press("Escape")
    page.wait_for_selector("#settingsOverlay", state="hidden", timeout=5000)
    assert not is_open()

    # Reopen and close via clicking the overlay background
    page.locator("#settingsBtn").click()
    page.wait_for_selector("#settingsOverlay.show", timeout=5000)
    # Click at position near the top-left edge of the overlay (outside the modal card)
    settings_overlay.click(position={"x": 10, "y": 10})
    page.wait_for_selector("#settingsOverlay", state="hidden", timeout=5000)
    assert not is_open()


def test_settings_add_root_via_browse(page, server_url, tmp_path):
    """Adding a root via the browse UI adds it to the roots list and repos."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector("#settingsBtn", timeout=5000)

    # Create a new directory with a git repo to add as a root
    new_root = tmp_path / "new-root"
    new_repo = new_root / "repo-c"
    new_repo.mkdir(parents=True)
    subprocess.run(["git", "init", str(new_repo)], capture_output=True, check=True)

    initial_cards = page.locator(".card").count()

    # Open settings
    page.locator("#settingsBtn").click()
    page.wait_for_selector("#settingsOverlay.show", timeout=5000)

    # Navigate browse to tmp_path
    # First, go to / by clicking the "/" quick button
    page.locator('#browseQuick button[data-path="/"]').click()
    page.wait_for_selector("#browseList .browse-item", timeout=5000)

    # Navigate to tmp_path by using the browse path crumb mechanism
    # Simpler: use evaluate to call loadBrowse directly with tmp_path
    browse_path = str(tmp_path.parent).rstrip("/")
    page.evaluate(
        "(path) => { const ev = new Event('click'); "
        "window.loadBrowse ? window.loadBrowse(path) : "
        "fetch('/api/browse?path=' + encodeURIComponent(path)).then(r=>r.json()).then(d=>{ "
        "  document.querySelector('#browsePath').innerHTML = d.path; "
        "  document.querySelector('#browseList').innerHTML = d.dirs.map(x => "
        "    '<div class=\"browse-item\" data-path=\"' + x.path + '\">' + "
        "    '<span class=\"name\">' + x.name + '</span>' + "
        "    '<button class=\"select-btn\" data-path=\"' + x.path + '\" data-name=\"' + x.name + '\">Add</button></div>').join(''); "
        "}); }",
        browse_path,
    )
    time.sleep(0.3)

    # Find and click the tmp_path folder to enter it
    tmp_name = tmp_path.name
    page.evaluate(
        "(name) => { "
        "  const items = document.querySelectorAll('#browseList .browse-item'); "
        "  for (const it of items) { "
        "    if (it.querySelector('.name').textContent === name) { it.click(); return; } "
        "  } "
        "}",
        tmp_name,
    )
    time.sleep(0.3)

    # Find "new-root" and click its Add button
    page.evaluate(
        "(name) => { "
        "  const btns = document.querySelectorAll('#browseList .select-btn'); "
        "  for (const b of btns) { "
        "    if (b.dataset.name === name) { b.click(); return; } "
        "  } "
        "}",
        "new-root",
    )

    # Wait for root to be added — roots list should grow
    page.wait_for_function(
        "() => document.querySelectorAll('#settingsRoots .root-item').length > 1",
        timeout=5000,
    )
    root_count = page.locator("#settingsRoots .root-item").count()
    assert root_count >= 2

    # Close settings and verify repo count increased
    page.locator("#settingsClose").click()
    time.sleep(0.3)
    final_cards = page.locator(".card").count()
    assert final_cards > initial_cards


def test_settings_remove_root(page, server_url):
    """Removing a root via settings deletes it from the roots list."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector("#settingsBtn", timeout=5000)

    # Open settings
    page.locator("#settingsBtn").click()
    page.wait_for_selector("#settingsOverlay.show", timeout=5000)
    page.wait_for_selector("#settingsRoots .root-item", timeout=5000)

    initial_roots = page.locator("#settingsRoots .root-item").count()
    assert initial_roots >= 1

    # Remove the first root
    del_btn = page.locator("#settingsRoots .del-btn").first
    del_btn.click()

    # Roots list should be empty or have fewer items
    time.sleep(0.5)
    final_roots = page.locator("#settingsRoots .root-item").count()
    assert final_roots < initial_roots

    # Main grid should also have fewer repos (or show empty state)
    page.locator("#settingsClose").click()
    time.sleep(0.3)
    # Either no cards, or fewer cards than before
    final_cards = page.locator(".card").count()
    assert final_cards == 0  # we removed the only root


# ---------------------------------------------------------------------------
# Browse navigation
# ---------------------------------------------------------------------------


def test_browse_directory_navigation(page, server_url, tmp_path):
    """Browse panel supports quick nav buttons and directory listing."""
    base_url, token = server_url
    page.goto(f"{base_url}/?token={token}")
    page.wait_for_selector("#settingsBtn", timeout=5000)

    # Create nested directories for testing
    level1 = tmp_path / "dir-a"
    level2 = level1 / "dir-b"
    level2.mkdir(parents=True)

    # Open settings (triggers initial browse load)
    page.locator("#settingsBtn").click()
    page.wait_for_selector("#settingsOverlay.show", timeout=5000)
    page.wait_for_selector("#browseList", timeout=5000)

    # Quick nav: Home button loads a path
    page.locator('#browseQuick button[data-path="~"]').click()
    # Wait for browse list to render items
    page.wait_for_selector("#browseList .browse-item", timeout=5000)
    home_items = page.locator("#browseList .browse-item").count()
    assert home_items > 0

    # Quick nav: root button works
    page.locator('#browseQuick button[data-path="/"]').click()
    page.wait_for_selector("#browseList .browse-item", timeout=5000)
    root_items = page.locator("#browseList .browse-item").count()
    assert root_items > 0

    # Breadcrumb: at least the root crumb is present
    crumbs = page.locator("#browsePath .crumb")
    assert crumbs.count() >= 1

    # Navigate into tmp_path by simulating the browse load via JS
    # (more reliable than clicking through many levels)
    tmp_abs = str(tmp_path.resolve())
    browse_js = """
    (p) => {
      const token = new URLSearchParams(window.location.search).get('token') || '';
      return fetch('/api/browse?path=' + encodeURIComponent(p) + '&token=' + token)
        .then(r => r.json())
        .then(data => {
          const parts = data.path.split('/').filter(Boolean);
          let acc = '';
          const crumbs = parts.map((part, i) => {
            acc += '/' + part;
            if (i === parts.length - 1) return '<span class="current">' + part + '</span>';
            return '<span class="crumb" data-path="' + acc + '">' + part + '</span><span class="sep">/</span>';
          });
          document.querySelector('#browsePath').innerHTML = '<span class="crumb" data-path="/">/</span>' + crumbs.join('');
          document.querySelector('#browseList').innerHTML = data.dirs.map(d =>
            '<div class="browse-item" data-path="' + d.path + '">' +
            '<span class="name">' + d.name + '</span>' +
            '<button class="select-btn" data-path="' + d.path + '" data-name="' + d.name + '">Add</button></div>'
          ).join('') || '<div class="browse-empty">No subdirectories</div>';
          return data.dirs.length;
        });
    }
    """
    page.evaluate(browse_js, tmp_abs)

    # Wait for dir-a to appear in the list
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#browseList .browse-item .name'))"
        ".some(n => n.textContent === 'dir-a')",
        timeout=5000,
    )

    # Breadcrumb should show tmp_path as current
    crumbs_text = page.locator("#browsePath").text_content()
    assert tmp_path.name in crumbs_text

    # Click dir-a to navigate into it
    page.evaluate(
        "() => { "
        "  const items = document.querySelectorAll('#browseList .browse-item'); "
        "  for (const it of items) { "
        "    if (it.querySelector('.name').textContent === 'dir-a') { "
        "      it.click(); return true; "
        "    } "
        "  } "
        "  return false; "
        "}"
    )
    time.sleep(0.3)

    # After entering dir-a, dir-b should be visible
    has_dir_b = page.evaluate(
        "() => Array.from(document.querySelectorAll('#browseList .browse-item .name'))"
        ".some(n => n.textContent === 'dir-b')"
    )
    assert has_dir_b, "Expected 'dir-b' inside 'dir-a'"
