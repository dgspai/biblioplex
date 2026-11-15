"""Card-name inputs show the server's ranked suggestions verbatim.

A native <datalist> re-filters its options by substring of the typed text, so
fuzzy hits the server returns ("yshtola" -> "Y'shtola, Night's Blessed") were
hidden and only exact spellings surfaced. Every name input uses the shared
`attachSuggest` dropdown instead. Static pins over web/js/*.js.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "web" / "index.html").read_text()
JS = app_js()


def test_shared_helper_exists_once() -> None:
    assert JS.count("function attachSuggest(") == 1


def test_main_search_uses_the_helper() -> None:
    assert re.search(r"attachSuggest\(\s*qInput, acDropdownEl", JS)
    assert "function _fetchAutocomplete" not in JS


def test_combos_card_input_uses_the_helper_not_a_datalist() -> None:
    inp = re.search(r'<input[^>]*id="combosCards"[^>]*>', INDEX).group(0)
    assert "list=" not in inp
    assert "combosCardList" not in INDEX
    assert re.search(r"attachSuggest\(\s*combosCardsEl", JS)


def test_commander_input_uses_the_helper_not_a_datalist() -> None:
    assert 'list="buildCmdList"' not in JS
    assert "buildCmdList" not in JS
    assert 'attachSuggest($("#buildCmdInput")' in JS


def test_stale_responses_are_dropped() -> None:
    start = JS.index("function attachSuggest(")
    body = JS[start : JS.index("\n}\n", start)]
    assert "seq" in body
