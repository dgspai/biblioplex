"""D19.29 — missing-card cost in the web UI.

The whole deliverable hangs on three rendering rules: an unpriced count
must never be hidden, a null cost must never read as $0.00, and prices
must be labelled as refresh-stale rather than implied live.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "index.html"


@pytest.fixture(scope="module")
def js() -> str:
    return app_js()


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text()


def _fn(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


# ---- the sort control ----


def test_sort_dropdown_offers_missing_cost(html: str) -> None:
    block = html[html.index('<select id="deckSort">') :]
    block = block[: block.index("</select>")]
    assert 'value="missing_cost"' in block


def test_sort_option_is_labelled_for_a_human(html: str) -> None:
    """`missing_cost` is the wire value; the label should say what it means."""
    block = html[html.index('<select id="deckSort">') :]
    block = block[: block.index("</select>")]
    row = block[block.index('value="missing_cost"') :]
    row = row[: row.index("</option>")].lower()
    assert "cheap" in row or "finish" in row or "cost" in row


def test_existing_sort_control_was_extended_not_duplicated(html: str) -> None:
    assert html.count('<select id="deckSort">') == 1
    assert html.count('<select id="deckSortDir">') == 1


def test_missing_cost_defaults_to_cheapest_first(js: str) -> None:
    """Server ascends for `missing_cost` and descends for `coverage`.

    The UI always sends an explicit direction, so picking the key must move
    the direction control with it — otherwise "cheapest to finish" silently
    lists the most expensive deck first.
    """
    assert "_deckSortDefaultDir" in js
    fn = _fn(js, "_deckSortDefaultDir")
    assert "missing_cost" in fn
    assert "asc" in fn


# ---- the cost label ----


def test_cost_label_helper_exists(js: str) -> None:
    assert "function _deckMissingCostLabel(" in js


def test_null_cost_is_not_rendered_as_zero(js: str) -> None:
    """"Not indexed yet" is not "costs nothing" — same distinction
    `_deckCoverageLabel` already draws for `coverage_pct` (D19.25)."""
    fn = _fn(js, "_deckMissingCostLabel")
    assert "null" in fn or "undefined" in fn
    assert "unknown" in fn.lower()


def test_unpriced_count_reaches_the_label(js: str) -> None:
    fn = _fn(js, "_deckMissingCostLabel")
    assert "unpriced_missing" in fn


def test_label_executes_correctly(js: str, tmp_path: Path) -> None:
    """Drive the formatter for real.

    A substring pin cannot distinguish `null` from `0`, nor prove the
    unpriced suffix appears — and both are the point of the deliverable.
    This is also how the unpriced path is exercised at all: every real deck
    on the live stack has `unpriced_missing: 0`.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    fn = js[js.index("function _deckMissingCostLabel(") :]
    fn = fn[: fn.index("\n}\n") + 3]
    script = tmp_path / "cost.js"
    script.write_text(
        fn
        + """
const fail = (m) => { console.error(m); process.exit(1); };
const L = _deckMissingCostLabel;

// null must never read as a price
const unknown = L({ missing_usd: null, unpriced_missing: 0 });
if (!/unknown/i.test(unknown)) fail(`null cost must read unknown, got ${JSON.stringify(unknown)}`);
if (/\\$0/.test(unknown)) fail("null cost must never render as $0.00");
if (/unknown/i.test(L({ missing_usd: 0, unpriced_missing: 3 }))) fail("a real 0 is not unknown");

// a genuine cost formats to two decimals
const real = L({ missing_usd: 396.94, unpriced_missing: 0 });
if (!real.includes("396.94")) fail(`expected 396.94, got ${JSON.stringify(real)}`);
if (/unpriced/.test(real)) fail("no unpriced suffix when the count is 0");
if (L({ missing_usd: 5.5, unpriced_missing: 0 }).indexOf("5.50") === -1) fail("must pad to 2dp");

// the unpriced suffix is the author decision — it must be visible
const partial = L({ missing_usd: 396.94, unpriced_missing: 3 });
if (!partial.includes("396.94")) fail("total lost when unpriced present");
if (!/3 unpriced/.test(partial)) fail(`unpriced count must show, got ${JSON.stringify(partial)}`);

// all-unpriced: total is 0 but the deck is NOT complete
const allUnpriced = L({ missing_usd: 0, unpriced_missing: 2 });
if (!/2 unpriced/.test(allUnpriced)) fail("a $0 total with unpriced cards must still say so");

// nothing missing renders nothing rather than "$0.00"
const none = L({ missing_usd: 0, unpriced_missing: 0 });
if (none !== "") fail(`a complete deck should render no cost segment, got ${JSON.stringify(none)}`);
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ---- where it is rendered ----


def test_deck_list_rows_show_the_cost(js: str) -> None:
    fn = _fn(js, "_renderDecksList")
    assert "_deckMissingCostLabel(" in fn


def test_deck_card_summary_shows_the_cost(js: str) -> None:
    fn = _fn(js, "_renderDeckCardTable")
    assert "_deckMissingCostLabel(" in fn


def test_price_staleness_is_stated_where_the_cost_is_shown(html: str) -> None:
    """Prices come from the bulk ingest, not live lookups.

    Asserted on the note element itself, not on the file containing the
    words "price" and "refresh" somewhere — the first version of this pin
    passed on unrelated copy, and the note was in fact only a `title=`
    attribute that never rendered on the list view at all.
    """
    assert "deck-cost-note" in html
    note = html[html.index("deck-cost-note") :]
    note = note[: note.index("</p>")].lower()
    assert "refresh" in note
    assert "cheapest printing" in note


# ---- the whack-a-mole guards ----


def test_every_helper_the_cost_path_calls_is_defined(js: str) -> None:
    """`node --check` validates syntax, not identifier existence (D19.22)."""
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    defined |= set(
        re.findall(r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(|async|function)", js, re.M)
    )
    builtins = {
        "if", "for", "while", "switch", "catch", "return", "typeof",
        "String", "Number", "Array", "Object", "Set", "Map", "Promise",
        "JSON", "parseInt", "parseFloat", "encodeURIComponent", "fetch",
        "setTimeout", "console", "isNaN", "alert", "confirm",
    }
    for fname in ("_deckMissingCostLabel", "_deckSortDefaultDir"):
        body = _fn(js, fname)
        body = re.sub(r"//[^\n]*", "", body)
        locals_ = set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", body))
        params = body[body.index("(") + 1 : body.index(")")]
        locals_ |= {p.strip() for p in params.split(",") if p.strip()}
        locals_ |= set(re.findall(r"\(\s*([A-Za-z_$][\w$]*)\s*\)\s*=>", body))
        for call in set(re.findall(r"(?<![.\w$])([a-zA-Z_$][\w$]*)\s*\(", body)):
            assert (
                call in builtins or call in defined or call in locals_
            ), f"{fname} calls undefined {call}()"


def test_boot_stays_the_last_top_level_statement(js: str) -> None:
    """D19.13: two TDZ bugs came from top-level code after the boot call."""
    tail = js[js.index("_bootFromUrl();") + len("_bootFromUrl();") :]
    assert not re.search(r"(?m)^[A-Za-z_$]", tail), "no top-level code after boot"
