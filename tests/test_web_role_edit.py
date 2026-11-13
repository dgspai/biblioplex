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
