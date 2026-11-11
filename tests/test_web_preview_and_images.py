"""F18 / F29 — hover preview is dismissed when its row goes away; broken
card art degrades to a placeholder instead of raw alt text.

Static pins (app.js has no JS unit harness).
"""

from __future__ import annotations

from tests.web_assets import app_js

JS = app_js()


def test_preview_dismissed_on_click_scroll_leave_and_rerender() -> None:
    assert 'addEventListener("click", _dismissPreview, true)' in JS
    assert 'addEventListener("scroll", _dismissPreview, true)' in JS
    assert 'resultsEl.addEventListener("pointerleave", _dismissPreview)' in JS
    body = JS[JS.index("function renderResults(") :]
    assert "_dismissPreview();" in body[:200]


def test_preview_skips_the_card_already_in_detail() -> None:
    assert "hit.id === selectedId" in JS


def test_card_art_has_an_error_fallback() -> None:
    assert 'addEventListener("error"' in JS
    assert "art-missing" in JS
