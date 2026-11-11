"""Decks Library: list | detail split; Import and Plan open as dialogs.

Static pins (app.js has no JS unit harness). Element ids are unchanged, so
every handler and the older deck tests keep their targets.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.web_assets import app_js

INDEX = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()


def _block(start_marker: str, end_marker: str) -> str:
    start = INDEX.index(start_marker)
    return INDEX[start : INDEX.index(end_marker, start)]


def _pane() -> str:
    return _block('id="decksPane"', "\n  </section>\n")


def _dialog(dialog_id: str) -> str:
    return _block(f'<dialog id="{dialog_id}"', "</dialog>")


def test_import_and_plan_are_dialogs_opened_from_the_toolbar() -> None:
    assert 'id="deckImportForm"' in _dialog("deckImportDialog")
    assert 'id="deckPlanForm"' in _dialog("deckPlanCard")
    toolbar = _block('id="deckSearchForm"', "</form>")
    for target in ("deckImportDialog", "deckPlanCard"):
        assert f'data-open-dialog="{target}"' in toolbar, target


def test_dialogs_are_wired_natively() -> None:
    js = app_js()
    assert "showModal()" in js
    assert "[data-open-dialog]" in js and "[data-close-dialog]" in js


def test_library_messages_do_not_go_to_a_closed_dialog() -> None:
    """Folder rename, fork, unpin and delete used the import status line.

    Inside a closed dialog that line is invisible, so they get their own.
    """
    assert 'id="deckLibStatus"' in _pane()
    assert 'id="deckLibStatus"' not in _dialog("deckImportDialog")
    js = app_js()
    assert js.count("_deckStatus(deckLibStatusEl") >= 4
    handler = js[js.index('deckImportFormEl.addEventListener("submit"'):]
    handler = handler[: handler.index("\n  });\n")]
    assert "deckLibStatusEl" not in handler


def test_no_helper_paragraphs_in_the_library() -> None:
    assert not re.search(r'<p class="help', _pane())
    assert "Costs are rough" not in _pane()
