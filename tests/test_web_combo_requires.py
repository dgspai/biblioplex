"""Combo template requirements ("Persist Creature") render wherever a combo does."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "web" / "js"
CORE = (JS / "00-core.js").read_text()
COMBOS = (JS / "40-combos.js").read_text()
BUILD = (JS / "90-build.js").read_text()
CSS = (ROOT / "web" / "style.css").read_text()

PERSIST = {"name": "Persist Creature", "query": "keyword:persist t:creature", "quantity": 1, "checked": True}
HASTE = {"name": "Haste Enabler", "query": None, "quantity": 2, "checked": False}
COMBO = {
    "id": "413-7841--5", "cards": ["Blasting Station", "Darksteel Angel"], "card_ids": ["a", "b"],
    "results": ["Infinite damage"], "requires": [PERSIST],
}


def _fn(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    return src[start : src.index("\n}\n", start) + 3]


def _run(tmp_path: Path, code: str) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    helpers = "".join(_fn(CORE, n) for n in ("escapeHtml", "_comboReqLabel", "_comboReqMark", "_comboReqTag", "_comboTitle"))
    script = tmp_path / "r.js"
    script.write_text(helpers + "const renderSymbols = (s) => escapeHtml(s);\n" + code)
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_combo_title_names_the_generic_piece(tmp_path: Path) -> None:
    out = _run(tmp_path, f"console.log(_comboTitle({json.dumps(COMBO)}));")
    assert out.strip() == "Blasting Station + Darksteel Angel + Persist Creature"


def test_detail_shows_requirement_as_a_card_slot_that_searches_it(tmp_path: Path) -> None:
    out = _run(tmp_path, _fn(COMBOS, "_proseToItems") + _fn(COMBOS, "_renderComboDetail")
               + f"console.log(_renderComboDetail({json.dumps(COMBO)}));")
    slot = out[out.index("combo-req"):]
    assert 'href="?q=keyword%3Apersist%20t%3Acreature"' in slot
    assert "Persist Creature" in slot
    assert out.index("Darksteel Angel</figcaption>") < out.index("combo-req")


def test_unchecked_requirement_is_marked_visibly_and_not_linked(tmp_path: Path) -> None:
    out = _run(tmp_path, f"console.log(_comboReqTag({json.dumps(HASTE)}, 'chip'));")
    assert "href" not in out
    assert "Haste Enabler ×2" in out
    assert ">?<" in out and "not checked" in out


def test_unchecked_slot_holds_its_mark_inside_the_tile(tmp_path: Path) -> None:
    combo = {**COMBO, "requires": [HASTE]}
    out = _run(tmp_path, _fn(COMBOS, "_proseToItems") + _fn(COMBOS, "_renderComboDetail")
               + f"console.log(_renderComboDetail({json.dumps(combo)}));")
    slot = out[out.index('class="combo-req-slot"'):]
    assert slot.index(">?<") < slot.index("</span><figcaption>")


def test_card_detail_combo_list_has_requirement_chip(tmp_path: Path) -> None:
    out = _run(tmp_path, _fn(CORE, "_renderComboEntry") + f"console.log(_renderComboEntry({json.dumps(COMBO)}));")
    assert "chip-req" in out
    assert "Darksteel Angel + Persist Creature" in out


def _panel(tmp_path: Path, present: list[object], near: list[object]) -> str:
    return _run(
        tmp_path,
        "const _build = { deck: {} };\n" + _fn(BUILD, "_addBtn") + _fn(BUILD, "_comboLink")
        + _fn(BUILD, "_comboGapReqs") + _fn(BUILD, "_renderComboPanel")
        + f"console.log(_renderComboPanel({json.dumps(present)}, {json.dumps(near)}));",
    )


def test_build_present_combo_names_the_card_that_fills_the_slot(tmp_path: Path) -> None:
    present = [{**COMBO, "requires": [{**PERSIST, "in_deck": ["Kitchen Finks"], "missing": 0}]}]
    assert "Blasting Station + Darksteel Angel + Kitchen Finks" in _panel(tmp_path, present, [])


def test_build_near_combo_missing_only_a_template_you_own(tmp_path: Path) -> None:
    near = [{"id": "n1", "in_deck": ["Blasting Station", "Darksteel Angel"], "gap": [], "missing": 1,
             "requires": [{**PERSIST, "in_deck": [], "missing": 1, "owned": 2}], "results": []}]
    html = _panel(tmp_path, [], near)
    assert "From your collection" in html
    assert "<strong>Persist Creature</strong>" in html


def test_requirement_slot_keeps_card_proportions() -> None:
    assert ".combo-req-slot" in CSS and "aspect-ratio" in CSS[CSS.index(".combo-req-slot"):][:300]


def test_requirement_link_opens_cards_in_place(tmp_path: Path) -> None:
    """Plain click switches to Cards with the query; href stays for new-tab and agents."""
    out = _run(tmp_path, f"console.log(_comboReqTag({json.dumps(PERSIST)}, 'chip'));")
    assert 'data-search-q="keyword:persist t:creature"' in out
    handler = CORE[CORE.index('closest?.("a[data-search-q]")') - 300:][:900]
    assert "preventDefault" in handler and '_setTab("search")' in handler
    assert "metaKey" in handler and "ctrlKey" in handler


def test_slot_cycles_through_matching_cards() -> None:
    body = _fn(COMBOS, "_fillComboReqSlides")
    assert "/cards/search?q=" in body and "order=edhrec" in body and ".slice(0, 6)" in body
    assert "prefers-reduced-motion" in body
    assert "isConnected" in body and "clearInterval" in body


def test_slideshow_is_decorative_and_crossfades() -> None:
    body = _fn(COMBOS, "_fillComboReqSlides")
    assert 'alt=""' in body
    rule = CSS[CSS.index(".combo-req-slot img"):][:400]
    assert "position: absolute" in rule and "transition" in rule


def test_slot_is_filled_once_per_render() -> None:
    body = _fn(COMBOS, "_fillComboReqSlides")
    assert body.index("delete slot.dataset.reqQ") < body.index("await api(")
