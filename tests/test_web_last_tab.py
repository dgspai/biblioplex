"""Sidebar sections reopen the segment last used in them (Decks → Build)."""

from __future__ import annotations

from pathlib import Path

CORE = (Path(__file__).resolve().parent.parent / "web" / "js" / "00-core.js").read_text()


def _fn(name: str) -> str:
    body = CORE[CORE.index(f"function {name}(") :]
    return body[: body.index("\n}\n")]


def test_set_tab_remembers_the_tab_per_section() -> None:
    body = _fn("_setTab")
    assert "_LAST_TAB_KEY" in body and "localStorage.setItem" in body


def test_sidebar_item_reopens_the_remembered_tab() -> None:
    handler = CORE[CORE.index('closest("[data-goto-tab]")') :]
    handler = handler[: handler.index("});")]
    # Only sidebar items restore; empty-state CTAs keep their explicit target.
    assert ".nav-item" in handler and "_lastTab(" in handler
