"""Build → Review: a build loaded from a saved deck saves back as a new version."""

from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
BUILD = (WEB / "js" / "90-build.js").read_text()


def _fn(src: str, name: str) -> str:
    body = src[src.index(f"function {name}(") :]
    return body[: body.index("\n}\n")]


def test_loading_a_saved_deck_remembers_its_origin() -> None:
    body = _fn(BUILD, "_loadSavedDeck")
    assert "_build.sourceId = id" in body
    assert "_build.sourceName" in body


def test_reset_forgets_the_origin() -> None:
    assert "sourceId" not in _fn(BUILD, "_resetBuild")


def test_sourced_build_offers_save_new_version_and_save_as_new() -> None:
    row = _fn(BUILD, "_buildSaveRowHtml")
    assert "_build.sourceId" in row
    assert 'id="buildSaveVersion"' in row and "Save new version" in row
    assert 'id="buildSaveDeck"' in row


def test_save_new_version_patches_the_source_deck() -> None:
    handler = BUILD[BUILD.index('e.target.closest("#buildSaveVersion")') :][:800]
    assert '"PATCH"' in handler
    assert "_build.sourceId" in handler and "decklist: _buildDecklist()" in handler


def test_save_as_new_deck_retargets_later_saves() -> None:
    handler = BUILD[BUILD.index('e.target.closest("#buildSaveDeck")') :][:900]
    assert "_build.sourceId = deck.id" in handler
