"""D19.47 — naming the cards that failed to match."""

from __future__ import annotations

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
    if js[max(0, start - 6) : start] == "async ":
        start -= 6
    return js[start : js.index("\n}\n", start) + 3]


def test_the_list_is_rendered_above_the_table(js: str) -> None:
    """Below the 100 rows it would never be seen."""
    table = _fn(js, "_renderDeckCardTable")
    assert "_renderUnresolvedCards()" in table
    body = table[table.index("_renderUnresolvedCards()") :]
    assert "_sortedDeckCards" not in body or table.index("_renderUnresolvedCards()") > table.index(
        "_sortedDeckCards()"
    ), "the block must be assigned before rows are appended"


def test_reads_resolved_not_a_price_or_id_proxy(js: str) -> None:
    """`resolved` is the field that means this.

    A `price_usd == null` card is merely unpriced and a real card; using
    that as the signal would list ~5% of the corpus as broken.
    """
    fn = _fn(js, "_renderUnresolvedCards")
    assert "c.resolved" in fn
    assert "price_usd" not in fn


def test_renders_nothing_when_every_card_resolved(tmp_path: Path, js: str) -> None:
    """Executed: an empty heading is noise on every clean deck, and a
    substring pin cannot tell "" from a heading with an empty list."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    script = tmp_path / "u.js"
    script.write_text(
        "const escapeHtml = (s) => String(s);\n"
        "let _deckCards = [];\n"
        + _fn(js, "_renderUnresolvedCards")
        + """
const fail = (m) => { console.error(m); process.exit(1); };

_deckCards = [{ resolved: true, name: "Sol Ring", qty: 1 }];
if (_renderUnresolvedCards() !== "") fail("a clean deck must render nothing at all");

_deckCards = [
  { resolved: true, name: "Sol Ring", qty: 1 },
  { resolved: false, name: "Lightning Greaves (PLST) C19-217", qty: 1 },
  { resolved: false, name: "Bogus Name", qty: 3 },
];
const out = _renderUnresolvedCards();
if (out === "") fail("unresolved cards must be listed");
if (!out.includes("2 unresolved cards")) fail(`expected a count, got ${out.slice(0, 80)}`);
if (out.includes("Sol Ring")) fail("a resolved card must not be listed");
// Verbatim: the failing string IS the diagnostic.
if (!out.includes("Lightning Greaves (PLST) C19-217")) fail("the name must render verbatim");
if (!out.includes("3x")) fail("quantity must survive");

_deckCards = [{ resolved: false, name: "Only One", qty: 1 }];
const one = _renderUnresolvedCards();
if (one.includes("1 unresolved cards")) fail(`bad pluralisation: ${one.slice(0, 60)}`);
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_names_are_escaped(js: str) -> None:
    """They are unmatched user input by definition."""
    assert "escapeHtml(c.name)" in _fn(js, "_renderUnresolvedCards")


def test_explains_the_consequence_not_just_the_count(js: str) -> None:
    """"3 unresolved" alone does not say why the reader should care."""
    fn = _fn(js, "_renderUnresolvedCards")
    assert "inventory" in fn and "priced" in fn


# ---- D19.48: the version timeline must not report it ----


def test_version_rows_do_not_render_unresolved_count(js: str) -> None:
    """It is a write-time snapshot nothing recomputes.

    `resolve_names` fuzzy-matches where the derived `deck_cards` index does
    not, so the stored value is produced by a different resolver — and after
    Bug.36 fixed the collector-number parse, every pre-fix row reported a
    number from a parser that no longer exists. On a HEAD version that
    contradicted the header badge outright: 1 on the row, 0 in the header,
    same content on the same screen.
    """
    fn = _fn(js, "_renderVersionRows")
    assert "unresolved" not in fn.split("_renderVersionRows(")[1], (
        "the version row must not report an unresolved count"
    )


def test_the_current_state_still_has_a_home(js: str) -> None:
    """Dropping the stale number must not drop the accurate ones.

    The derived badge (library row + detail header) and D19.47's named list
    are where "what is unresolved now" lives.
    """
    assert "d.unresolved_count" in js, "the library row badge must survive"
    assert "deck.unresolved_count" in js, "the detail header badge must survive"
    assert "_renderUnresolvedCards" in js, "the named list must survive"
