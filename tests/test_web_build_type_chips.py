"""Build recommendations: card-type chips filter like the Focus theme chips."""

from __future__ import annotations

from pathlib import Path

BUILD = (Path(__file__).resolve().parent.parent / "web" / "js" / "90-build.js").read_text()


def _fn(name: str) -> str:
    body = BUILD[BUILD.index(f"function {name}(") :]
    return body[: body.index("\n}\n")]


def test_type_chips_render_with_deck_counts() -> None:
    body = _fn("_renderThemeChips")
    assert "buildTypeChips" in body and "data-build-type" in body
    assert "_buildTypeSel" in body and "chip-count" in body


def test_selected_types_are_or_ed_into_the_search() -> None:
    body = _fn("_runBuildSearch")
    assert "_buildTypeSel" in body and "t:" in body


def test_type_chip_click_toggles_and_searches() -> None:
    handler = BUILD[BUILD.index('closest("[data-build-type]")') :]
    handler = handler[: handler.index("return;")]
    assert "_buildTypeSel" in handler and "_runBuildSearch" in handler


def test_reset_clears_type_selection() -> None:
    assert "_buildTypeSel = new Set()" in _fn("_resetBuild")
