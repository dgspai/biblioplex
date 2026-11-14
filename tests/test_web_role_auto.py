"""Role editors show the classifier's own verdict next to a manual edit."""

from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
CORE = (WEB / "js" / "00-core.js").read_text()
BUILD = (WEB / "js" / "90-build.js").read_text()
CSS = (WEB / "style.css").read_text()


def _fn(src: str, name: str) -> str:
    body = src[src.index(f"function {name}(") :]
    return body[: body.index("\n}\n")]


def test_detail_editor_marks_auto_roles_and_redundant_edits() -> None:
    body = _fn(CORE, "_renderPredictedLabelsSection")
    assert "autoRoles(" in body and "is-auto" in body
    assert "sameRoles(" in body and "matches auto" in body


def test_popover_marks_auto_roles_from_a_fresh_card() -> None:
    render = _fn(BUILD, "_renderRolePop")
    assert "autoRoles(" in render and "is-auto" in render
    assert "sameRoles(" in render and "matches auto" in render
    # Reset only when there is an override to discard.
    assert "labels_overridden" in render and "data-role-reset" in render
    assert "api(`/cards/" in _fn(BUILD, "_openRolePop")


def test_save_refreshes_the_popover_card() -> None:
    body = BUILD[BUILD.index("async function _setRoles(") :]
    assert "_rolePopCard = card" in body[: body.index("\n}\n")]


def test_auto_marker_is_styled() -> None:
    assert ".is-auto" in CSS
