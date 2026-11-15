"""Build Focus chips show deck count / owned-in-identity count per tag."""

from __future__ import annotations

from pathlib import Path

BUILD = (Path(__file__).resolve().parent.parent / "web" / "js" / "90-build.js").read_text()


def _fn(name: str) -> str:
    body = BUILD[BUILD.index(f"function {name}(") :]
    return body[: body.index("\n}\n")]


def test_owned_counts_fetched_once_per_commander_identity() -> None:
    body = _fn("_renderThemeChips")
    cached = body[body.index("_buildCmdTagsFor !== _build.commander") :]
    cached = cached[: cached.index("_buildCmdTagsFor = _build.commander")]
    assert "/cards/owned-tag-counts?identity=" in cached
    assert "color_identity" in cached


def test_focus_chip_shows_deck_over_owned() -> None:
    body = _fn("_renderThemeChips")
    assert "${n}/${own}" in body
    assert "_buildOwnedTagCounts[t]" in body
