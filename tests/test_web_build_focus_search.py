"""Build tab: search the Focus theme chips and add any other oracle tag."""

from __future__ import annotations

from pathlib import Path

JS = (Path(__file__).resolve().parent.parent / "web" / "js" / "90-build.js").read_text()


def _fn(name: str) -> str:
    body = JS[JS.index(f"function {name}(") :]
    return body[: body.index("\n}\n")]


def test_focus_has_a_search_box_with_tag_autocomplete() -> None:
    body = _fn("_renderThemeChips")
    assert 'id="buildFocusQ"' in body and 'list="buildFocusList"' in body
    assert '<datalist id="buildFocusList">' in body
    assert "/tags?prefix=" in JS


def test_rerendering_chips_keeps_the_search_box() -> None:
    """Adding a card re-renders chips; that must not wipe what is being typed."""
    body = _fn("_renderThemeChips")
    assert "#buildFocusChips" in body
    assert "_filterFocusChips()" in body


def test_filter_keeps_selected_chips_visible() -> None:
    body = _fn("_filterFocusChips")
    assert "selected" in body and ".hidden" in body


def test_picking_a_tag_adds_and_selects_it() -> None:
    body = JS[JS.index('e.target.id !== "buildFocusQ"') :]
    body = body[: body.index("\n});\n")]
    assert "_buildFocusPool.add(" in body and "_buildOtagSel.add(" in body
    assert "_runBuildSearch()" in body
