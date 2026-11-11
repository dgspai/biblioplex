"""App shell: sidebar with four sections, sub-views as segments.

Static pins over web/index.html, web/js/*.js and web/style.css (app.js has no
JS unit harness). The eight internal tab ids are unchanged, so every existing
`?tab=` deep-link keeps working; sections only group them.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "web" / "index.html").read_text()
CSS = (ROOT / "web" / "style.css").read_text()


def _sidebar() -> str:
    return re.search(r'<nav class="sidebar".*?</nav>', INDEX, re.S).group(0)


def test_sidebar_lists_four_sections_then_settings() -> None:
    sections = re.findall(r'class="nav-item[^"]*"[^>]*data-section="([a-z]+)"', _sidebar())
    assert sections == ["cards", "collection", "decks", "combos", "settings"]


def test_every_tab_belongs_to_a_section() -> None:
    """_setTab resolves the active section via closest([data-section]).

    A tab button outside any section would throw on that lookup.
    """
    for m in re.finditer(r'<button[^>]*data-tab="([a-z]+)"[^>]*>', INDEX):
        tag = m.group(0)
        if "data-section=" in tag:
            continue
        head = INDEX[: m.start()]
        opener = head.rindex('class="segments"')
        assert head.count("</div>", opener) == 0, f"{m.group(1)} is outside a segments bar"


def test_segment_bars_cover_collection_and_decks() -> None:
    bars = dict(re.findall(r'class="segments" data-section="([a-z]+)"(.*?)</div>', INDEX, re.S))
    assert re.findall(r'data-tab="([a-z]+)"', bars["collection"]) == ["inventory", "wishlist"]
    assert re.findall(r'data-tab="([a-z]+)"', bars["decks"]) == ["decks", "build", "compare"]


def test_set_tab_is_table_driven_and_shows_the_section() -> None:
    js = app_js()
    body = js[js.index("function _setTab("):]
    body = body[: body.index("\n}\n")]
    assert "_TAB_PANES" in body
    assert "segments" in body
    assert "isInventory" not in body, "per-tab boolean ladder should be gone"


def test_no_pane_hardcodes_the_old_header_height() -> None:
    assert "100vh - 60px" not in CSS
    assert "100vh - 100px" not in CSS


def test_sidebar_collapses_and_remembers() -> None:
    js = app_js()
    assert 'id="sidebarToggle"' in _sidebar()
    assert "sidebar-collapsed" in js
    assert "biblioplex.sidebar" in js
    # localStorage can throw (private mode, blocked storage): never fatal.
    body = js[js.index("function _setSidebarCollapsed("):]
    body = body[: body.index("\n}\n")]
    assert "try" in body


def test_collapsed_labels_stay_accessible() -> None:
    """Collapsed rail hides labels visually, not from screen readers."""
    for m in re.finditer(r'<button[^>]*class="nav-item[^>]*>.*?</button>', _sidebar(), re.S):
        assert 'class="nav-label"' in m.group(0), m.group(0)[:60]
    collapsed = re.findall(r"\.sidebar-collapsed[^{]*\.nav-label[^{]*\{([^}]*)\}", CSS)
    assert collapsed and all("display: none" not in rule for rule in collapsed)
