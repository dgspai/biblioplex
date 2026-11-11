"""D19.31 — price per card, and deck value in the summary line."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def js() -> str:
    return app_js()


def _fn(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


# ---- the naming trap ----


def test_price_cell_is_not_the_mana_cost_cell(js: str) -> None:
    """`deck-card-cost` is MANA cost and predates this deliverable.

    Reusing it for price would conflate two unrelated columns for whoever
    reads this file next.
    """
    row = _fn(js, "_deckCardRow")
    assert "deck-card-price" in row
    assert "deck-card-cost" in row, "mana cost cell must still exist"
    mana = row[row.index("deck-card-cost") : row.index("deck-card-cost") + 120]
    assert "mana_cost" in mana
    price = row[row.index("deck-card-price") : row.index("deck-card-price") + 120]
    assert "price" in price and "mana_cost" not in price


# ---- the column ----


def test_row_renders_a_price_cell(js: str) -> None:
    assert "_deckCardPrice(" in _fn(js, "_deckCardRow")


def test_header_offers_a_sortable_price_column(js: str) -> None:
    table = _fn(js, "_renderDeckCardTable")
    assert 'data-sort="price"' in table
    assert "Price" in table


def test_section_header_spans_every_column(js: str) -> None:
    """A stale colspan leaves the section heading short of the new column."""
    table = _fn(js, "_renderDeckCardTable")
    colspans = re.findall(r'colspan="(\d+)"', table)
    headers = len(re.findall(r'data-sort="', table))
    assert colspans, "section header lost its colspan"
    assert all(int(c) == headers for c in colspans), (
        f"colspan {colspans} must equal the {headers} sortable columns"
    )


def test_summary_line_shows_deck_value(js: str) -> None:
    assert "_deckTotalValueLabel(" in _fn(js, "_renderDeckCardTable")


def test_no_second_staleness_note_was_added(js: str) -> None:
    """D19.29 already ships one; a second would be noise."""
    assert js.count("priced as of the last card refresh") <= 1


# ---- formatter behaviour, executed ----


def test_price_and_value_formatters(js: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    script = tmp_path / "price.js"
    script.write_text(
        _fn(js, "_deckCardPrice") + "\n}\n"
        + _fn(js, "_deckTotalValueLabel") + "\n}\n"
        + """
const fail = (m) => { console.error(m); process.exit(1); };

// --- per-card price ---
const priced = _deckCardPrice({ price_usd: 12.5 });
if (!priced.includes("12.50")) fail(`priced cell wrong: ${priced}`);

const unpriced = _deckCardPrice({ price_usd: null });
if (unpriced.includes("$0.00") || unpriced.includes("0.00")) {
  fail(`unpriced must not read as zero, got ${JSON.stringify(unpriced)}`);
}

// A free card is genuinely $0.00 and must be distinguishable from unknown.
const free = _deckCardPrice({ price_usd: 0 });
if (free === unpriced) fail("a $0.00 card must not render identically to an unpriced one");

// --- deck value ---
const plain = _deckTotalValueLabel({ total_usd: 702.11, unpriced_total: 0 });
if (!plain.includes("702.11")) fail(`value wrong: ${plain}`);
if (plain.includes("unpriced")) fail("no caveat when nothing is unpriced");

const caveat = _deckTotalValueLabel({ total_usd: 18.4, unpriced_total: 3 });
if (!caveat.includes("18.40")) fail(`value lost: ${caveat}`);
if (!caveat.includes("3 unpriced")) fail(`caveat missing: ${caveat}`);

// The case live data cannot show: a deck whose cards are ALL unpriced
// reports $0.00 and must still say why, or it reads as worthless.
const allUnpriced = _deckTotalValueLabel({ total_usd: 0, unpriced_total: 5 });
if (!allUnpriced.includes("unpriced")) {
  fail(`a wholly unpriced deck must carry the caveat, got ${JSON.stringify(allUnpriced)}`);
}

// Missing summary must not throw.
_deckTotalValueLabel(undefined);
_deckTotalValueLabel({});
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ---- the whack-a-mole guard ----


def test_every_helper_the_table_calls_is_defined(js: str) -> None:
    """`node --check` validates syntax, not identifier existence.

    D19.22 shipped `renderManaCost()` — a function that does not exist —
    through a green gate. This copy understands function parameters, which
    the older `test_web_deck_card_table.py` one does not.
    """
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    defined |= set(
        re.findall(r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(|async|function)", js, re.M)
    )
    builtins = {
        "if", "for", "while", "switch", "catch", "return", "typeof",
        "String", "Number", "Array", "Object", "Set", "Map", "Promise",
        "JSON", "parseInt", "parseFloat", "encodeURIComponent", "fetch",
        "setTimeout", "console", "isNaN",
    }
    for fname in ("_deckCardPrice", "_deckTotalValueLabel", "_deckCardRow"):
        body = _fn(js, fname)
        params = re.findall(r"\(([^)]*)\)", body[: body.index("{")])
        local_names = {p.strip() for group in params for p in group.split(",") if p.strip()}
        body = re.sub(r"//[^\n]*", "", body)
        local_names |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", body))
        for call in set(re.findall(r"(?<![.\w$])([a-zA-Z_$][\w$]*)\s*\(", body)):
            assert (
                call in builtins or call in defined or call in local_names
            ), f"{fname} calls undefined {call}()"
