"""Combos screen: the same quiet bar and split as Cards and Collection."""

from __future__ import annotations

import re
from pathlib import Path

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "web" / "index.html").read_text()
CSS = (ROOT / "web" / "style.css").read_text()


def _pane() -> str:
    start = INDEX.index('id="combosPane"')
    return INDEX[start : INDEX.index("\n  </section>", start)]


def test_filter_bar_matches_the_cards_bar() -> None:
    form = re.search(r"<form[^>]*>", _pane()).group(0)
    assert "cards-filters" in form


def test_placeholders_are_labels_not_examples() -> None:
    for ph in re.findall(r'placeholder="([^"]*)"', _pane()):
        assert "e.g." not in ph and len(ph) <= 12, ph


def test_sort_options_do_not_repeat_sort() -> None:
    sel = _pane()[_pane().index('<select id="combosSort"') :]
    sel = sel[: sel.index("</select>")]
    assert "sort:" not in sel


def test_enter_submits_and_reset_is_an_icon() -> None:
    pane = _pane()
    assert '<button type="submit" class="visually-hidden">' in pane
    reset = re.search(r'<button[^>]*id="combosResetBtn"[^>]*>', pane).group(0)
    assert "icon-btn" in reset and "aria-label=" in reset


def test_no_instructional_empty_state() -> None:
    """The tab auto-loads every combo; the prompt was never readable."""
    assert "Enter filters and search" not in _pane()


def test_split_reuses_the_shared_splitter() -> None:
    pane = _pane()
    assert 'class="cards-split"' in pane and 'class="splitter"' in pane
    assert "#combosSplitter" not in CSS
    assert "--combos-split" not in CSS + app_js()
    assert "wireSplitter(combosSplitterEl" in app_js()


def test_pager_reads_like_the_cards_pager() -> None:
    js = app_js()
    body = js[js.index("async function runCombosSearch(") :]
    body = body[: body.index("\n}\n")]
    assert "· page" not in body
    assert "pagerRange(" in body


def test_detail_sections_use_headings_like_card_detail() -> None:
    js = app_js()
    body = js[js.index("function _renderComboDetail(") :]
    body = body[: body.index("\n}\n")]
    assert "<strong>" not in body
    assert body.count("<h3>") == 3


def _combos_js() -> str:
    return (ROOT / "web" / "js" / "40-combos.js").read_text()


def test_card_names_are_not_split_on_commas() -> None:
    """Legendary names carry commas: `Kenrith, the Returned King` is ONE card."""
    js = _combos_js()
    body = js[js.index("function _combosFormState(") :]
    body = body[: body.index("\n}\n")]
    assert '.split(",")' not in body
    assert 'getAll("card") || []).join' not in js


def test_card_input_autocompletes_via_native_datalist() -> None:
    pane = _pane()
    inp = re.search(r'<input[^>]*id="combosCards"[^>]*>', pane).group(0)
    assert 'list="combosCardList"' in inp
    assert '<datalist id="combosCardList">' in pane
    assert "/cards/autocomplete?q=" in _combos_js()


def test_picked_cards_render_as_removable_chips() -> None:
    assert 'id="combosCardChips"' in _pane()
    js = _combos_js()
    assert "data-remove-card=" in js
    assert 'aria-label="Remove ${' in js


def test_colors_are_mana_symbol_toggles_with_colorless() -> None:
    pane = _pane()
    assert '<input id="combosCi"' not in pane
    group = pane[pane.index('id="combosCi"') :]
    group = group[: group.index("</div>")]
    assert 'role="group"' in pane[pane.rindex("<div", 0, pane.index('id="combosCi"')) :]
    assert re.findall(r'data-ci="([WUBRGC])"', group) == list("WUBRGC")
    assert group.count('aria-pressed="false"') == 6
    # Symbol images are decorative; the button's aria-label names the color.
    assert re.findall(r'svgs\.scryfall\.io/card-symbols/([WUBRGC])\.svg', group) == list("WUBRGC")
    assert group.count('alt=""') == 6
    assert len(re.findall(r"<button[^>]*aria-label=", group)) == 6


def test_colorless_is_exclusive_with_colors() -> None:
    js = _combos_js()
    body = js[js.index('combosCiEl.addEventListener("click"') :]
    body = body[: body.index("\n  });\n")]
    assert '"C"' in body
