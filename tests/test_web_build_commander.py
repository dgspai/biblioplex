"""F5 — the builder resolves the commander when it is set.

Static pins over web/js/*.js (app.js has no JS unit harness). An
unresolved name used to be accepted, ticked as done, and only fail two steps
later with recommendations that ignored the commander's colour identity.
"""

from __future__ import annotations

from pathlib import Path

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent
JS = app_js()


def _set_handler() -> str:
    start = JS.index('e.target.closest("#buildSetCmd")')
    return JS[start : JS.index("#buildSuggestCmd", start)]


def test_set_commander_resolves_via_fuzzy_lookup() -> None:
    assert "/cards/named?fuzzy=" in _set_handler()


def test_failed_resolution_shows_inline_error_and_keeps_old_commander() -> None:
    h = _set_handler()
    assert "buildCmdError" in h and "state-error" in h
    # The commander is only assigned from the resolved card, never the raw text.
    assert "_build.commander = v" not in h
    assert "card.name" in h


def test_step1_markup_has_the_error_slot() -> None:
    assert 'id="buildCmdError"' in JS


def test_search_filters_colourless_commander_to_colourless_cards() -> None:
    """Empty identity means colourless: push `id<=c`, not skip the filter."""
    start = JS.index("async function _runBuildSearch")
    body = JS[start : JS.index("const slotChk", start)]
    assert 'id<=c"' in body
    assert "if (ci) parts.push" not in body


def test_slot_goal_line_refreshes_after_every_analyze() -> None:
    """The "Ramp (-12 to target)" header was rendered once per step, so it went
    stale as cards were added; _buildAnalyze must repaint it."""
    start = JS.index("async function _buildAnalyze")
    body = JS[start : JS.index("\n}\n", start)]
    assert "_refreshSlotGoals()" in body
    assert "function _buildSlotGoalsHtml" in JS


def _load_saved_deck() -> str:
    start = JS.index("async function _loadSavedDeck")
    return JS[start : JS.index("function _renderBuild", start)]


def test_saved_deck_load_uses_stored_commander_and_no_duplicate() -> None:
    h = _load_saved_deck()
    assert "_deckDetailData.commander" in h
    # Commander slot is decremented out of the main deck, never double-counted.
    assert "_build.deck[_build.commander] -= 1" in h


def test_saved_deck_load_carries_sideboard() -> None:
    h = _load_saved_deck()
    assert "_build.sideboard[r.name]" in h
    assert 'r.section === "sideboard") continue' not in h
    dl = JS[JS.index("function _buildDecklist") : JS.index("function _buildCount")]
    assert '"Sideboard"' in dl
