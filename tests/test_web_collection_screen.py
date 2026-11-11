"""Collection screen: one summary line, destructive actions live in Settings."""

from __future__ import annotations

import re
from pathlib import Path

from tests.web_assets import app_js

INDEX = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()


def _pane(pane_id: str) -> str:
    start = INDEX.index(f'id="{pane_id}"')
    return INDEX[start : INDEX.index("\n  </section>", start)]


def test_clear_buttons_live_in_settings_next_to_their_import() -> None:
    """Their handlers already report into the Settings status lines."""
    settings = _pane("settingsPane")
    for btn in ("invClearBtn", "wlClearBtn"):
        assert f'id="{btn}"' in settings, btn
        assert f'id="{btn}"' not in _pane("inventoryPane") + _pane("wishlistPane"), btn


def test_summary_is_one_line_not_tiles() -> None:
    js = app_js()
    assert "inv-stat" not in js
    for fn in ("_renderInvSummary", "_renderWlSummary"):
        body = js[js.index(f"function {fn}("):]
        body = body[: body.index("\n}\n")]
        assert " · " in body, fn


def test_filter_bars_match_the_cards_bar() -> None:
    for pane in ("inventoryPane", "wishlistPane"):
        form = re.search(r"<form[^>]*>", _pane(pane)).group(0)
        assert "cards-filters" in form, pane


def test_pager_and_prices_use_the_shared_formatters() -> None:
    """"page 1 · 2832 cards" and "$0.1" read differently from every other list."""
    js = app_js()
    assert "page ${_invPage}" not in js and "page ${_wlPage}" not in js
    for pager in ("invPageInfoEl", "wlPageInfoEl", "cardsPageInfoEl", "combosPageInfoEl"):
        assert f"{pager}.textContent = pagerRange(" in js, pager
    assert "$${r.price_usd}" not in js
    assert "$${escapeHtml(String(" not in js
