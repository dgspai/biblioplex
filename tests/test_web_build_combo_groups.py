"""Build review: combos grouped by what you need, each one opening in the Combos tab."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD = (ROOT / "web" / "js" / "90-build.js").read_text()
CORE = (ROOT / "web" / "js" / "00-core.js").read_text()


def _fn(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    return src[start : src.index("\n}\n", start) + 3]


PRESENT = [{"id": "p1", "cards": ["Basalt Monolith", "Rings of Brighthearth"], "results": ["Infinite colorless mana"]}]
NEAR = [
    {"id": "o1", "in_deck": ["Walking Ballista"], "gap": [{"name": "Heliod, Sun-Crowned", "id": "h", "owned": True}],
     "results": ["Infinite damage"]},
    {"id": "b1", "in_deck": ["Aetherflux Reservoir"], "gap": [{"name": "Karn's Touch", "id": "k", "owned": False}],
     "results": ["Infinite damage"]},
]


def _render(tmp_path: Path, present: list[object], near: list[object]) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    script = tmp_path / "panel.js"
    script.write_text(
        _fn(CORE, "escapeHtml")
        + "const _build = { deck: { 'Walking Ballista': 1 } };\n"
        + _fn(BUILD, "_addBtn")
        + _fn(BUILD, "_comboLink")
        + _fn(BUILD, "_renderComboPanel")
        + f"console.log(_renderComboPanel({json.dumps(present)}, {json.dumps(near)}));\n"
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_three_groups_in_order(tmp_path: Path) -> None:
    html = _render(tmp_path, PRESENT, NEAR)
    deck, owned, buy = html.index("In the deck"), html.index("From your collection"), html.index("Missing cards")
    assert deck < owned < buy
    assert html.index("Heliod") > owned and html.index("Heliod") < buy
    assert html.index("Karn&#39;s Touch") > buy or html.index("Karn's Touch") > buy


def test_every_combo_opens_in_the_combos_tab(tmp_path: Path) -> None:
    html = _render(tmp_path, PRESENT, NEAR)
    for cid in ("p1", "o1", "b1"):
        assert f'href="?tab=combos&amp;combo={cid}"' in html or f'href="?tab=combos&combo={cid}"' in html


def test_only_owned_gaps_get_an_add_button(tmp_path: Path) -> None:
    html = _render(tmp_path, PRESENT, NEAR)
    assert 'data-build-add="Heliod, Sun-Crowned"' in html
    assert "data-build-add=\"Karn" not in html


def test_empty_groups_are_left_out(tmp_path: Path) -> None:
    html = _render(tmp_path, [], NEAR[1:])
    assert "In the deck" not in html and "From your collection" not in html
    assert "Missing cards" in html
    assert _render(tmp_path, [], []).strip() == ""


def test_review_fetches_owned_and_missing_apart() -> None:
    body = _fn(BUILD, "_renderBuildStep7")
    assert "near-combos?missing=1" in body and "owned=${owned}" in body
    assert "nearCombos(true)" in body and "nearCombos(false)" in body
