"""Editing a card's roles: one shared writer, reachable from the Build tab."""

from __future__ import annotations

from pathlib import Path

from tests.web_assets import app_js

JS = app_js()
BUILD = (Path(__file__).resolve().parent.parent / "web" / "js" / "90-build.js").read_text()


def _fn(src: str, name: str) -> str:
    body = src[src.index(f"function {name}(") :]
    return body[: body.index("\n}\n")]


def test_one_label_writer_shared_by_every_editor() -> None:
    assert JS.count("/labels`") == 1  # only putCardLabels builds the URL
    assert "putCardLabels(" in _fn(JS, "_saveCardLabels")
    assert "putCardLabels(" in _fn(BUILD, "_recategoriseCard")


def test_recategorise_uses_the_shared_role_rule() -> None:
    assert "toggleRole(" in _fn(BUILD, "_recategoriseCard")


CSS = (Path(__file__).resolve().parent.parent / "web" / "style.css").read_text()


def test_build_tags_are_tap_targets() -> None:
    edit = _fn(BUILD, "_roleEdit")
    assert "data-role-edit=" in edit and "data-roles=" in edit
    assert 'aria-label="Edit roles"' in edit
    assert _fn(BUILD, "_buildCardRows").count("_roleEdit(c.id, c.predicted_labels)") == 2
    assert "_roleEdit(" in _fn(BUILD, "_dlRow")
    assert "e.isLand" in _fn(BUILD, "_dlRow")  # lands count by type, not role


def test_popover_toggles_through_the_shared_rule_and_writer() -> None:
    body = BUILD[BUILD.index("async function _setRoles(") :]
    body = body[: body.index("\n}\n")]
    assert "putCardLabels(" in body and "_scheduleBuildAnalyze()" in body
    click = BUILD[BUILD.index('_rolePop.addEventListener("click"') :][:900]
    assert "toggleRole(" in click and "null" in click  # toggle + reset to auto


def test_popover_closes_on_escape_and_outside_tap() -> None:
    assert '"Escape"' in BUILD and "_closeRolePop()" in BUILD


def test_popover_fits_a_phone() -> None:
    pop = CSS[CSS.index(".role-pop {") :].split("}", 1)[0]
    assert "100vw" in pop and "position: fixed" in pop
