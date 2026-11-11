"""Cards zero-results state is built from helpers.emptyResultsCopy, CTA included."""

from __future__ import annotations

from tests.web_assets import app_js

APP = app_js()


def test_render_results_uses_empty_copy_with_query_and_cta() -> None:
    assert "emptyResultsCopy(_healthKind, qInput.value)" in APP
    assert "emptyStateHtml(copy.title, copy.body, copy.cta)" in APP
