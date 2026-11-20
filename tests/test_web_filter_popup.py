"""Advanced filter popup: one dialog builds search syntax for every card list."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from biblioplex.dsl.cards_table import CARDS
from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "web" / "index.html").read_text()
FILTER_JS = ROOT / "web" / "js" / "07-filter.js"
BUILD = (ROOT / "web" / "js" / "90-build.js").read_text()
CORE = (ROOT / "web" / "js" / "00-core.js").read_text()
PRED_LABELS_JS = CORE[CORE.index("const PRED_LABELS") :].split("];", 1)[0] + "];\n"

CASES: list[tuple[dict[str, object], str]] = [
    ({}, ""),
    ({"name": "Sol Ring"}, 'name:"Sol Ring"'),
    ({"text": "draw a card"}, 'o:"draw a card"'),
    ({"type": "Legendary Dragon"}, "t:Legendary t:Dragon"),
    ({"color": {"op": ">=", "on": ["w", "u"]}}, "c>=wu"),
    ({"color": {"op": "=", "on": ["c"]}}, "c=c"),
    ({"identity": ["b", "g"]}, "id<=bg"),
    ({"stat": {"key": "cmc", "op": "<=", "n": "3"}}, "cmc<=3"),
    ({"stat": {"key": "pow", "op": ">", "n": ""}}, ""),
    ({"price": {"key": "usd", "op": "<", "n": "2.5"}}, "usd<2.5"),
    ({"rarity": ["r"]}, "r:r"),
    ({"rarity": ["c", "u"]}, "(r:c or r:u)"),
    ({"role": ["ramp", "card_advantage"]}, "(pred:ramp or pred:card_advantage)"),
    ({"format": "commander"}, "f:commander"),
    ({"tag": "removal"}, "otag:removal"),
    ({"keyword": "First strike"}, 'kw:"First strike"'),
    ({"set": "Khans of Tarkir (ktk)"}, "set:ktk"),
    ({"collection": ["unowned", "wanted"]}, "is:unowned is:wanted"),
    ({"land": ["tapped"]}, "land:tapped"),
    ({"land": ["dual", "tri", "untapped"]}, "land:untapped (land:dual or land:tri)"),
    ({"land": ["fetch", "utility"]}, "land:utility land:fetch"),
    (
        {"land": ["untapped", "conditional", "colorless", "mono", "dual", "tri", "five", "utility", "fetch"]},
        "(land:untapped or land:conditional) (land:colorless or land:mono or land:dual or land:tri or land:five)"
        " land:utility land:fetch",
    ),
    (
        {"type": "Dragon", "color": {"op": "<=", "on": ["r"]}, "price": {"key": "usd", "op": "<", "n": "1"}},
        "t:Dragon c<=r usd<1",
    ),
]


def _node_run(script: str, tmp_path: Path) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    path = tmp_path / "run.js"
    path.write_text(
        "globalThis.document = { addEventListener() {} };\n"
        + PRED_LABELS_JS
        + FILTER_JS.read_text()
        + "\n"
        + script
    )
    proc = subprocess.run([node, str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_query_builder_output(tmp_path: Path) -> None:
    out = _node_run(
        f"const cases = {json.dumps([c for c, _ in CASES])};\n"
        "console.log(JSON.stringify(cases.map(_filterQuery)));",
        tmp_path,
    )
    assert json.loads(out) == [want for _, want in CASES]


@pytest.mark.parametrize("q", [want for _, want in CASES if want])
def test_every_built_query_compiles(q: str) -> None:
    compile_ast(parse(q), CARDS)


def test_one_dialog_in_the_markup() -> None:
    assert INDEX.count('<dialog id="filterDialog"') == 1


@pytest.mark.parametrize("target", ["q", "invQ", "wlQ"])
def test_card_list_inputs_have_a_filter_button(target: str) -> None:
    assert f'data-filter-for="{target}"' in INDEX


def test_build_search_has_a_filter_button() -> None:
    assert 'data-filter-for="bsQ"' in BUILD


def test_sections_hide_fields_they_already_cover(tmp_path: Path) -> None:
    out = _node_run("console.log(JSON.stringify(_FILTER_OMIT));", tmp_path)
    omit = json.loads(out)
    assert {"owned", "unowned"} <= set(omit["invQ"])
    assert "wanted" in omit["wlQ"]
    assert {"owned", "unowned", "format", "land"} <= set(omit["bsQ"])
