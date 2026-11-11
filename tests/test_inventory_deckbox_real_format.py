"""D18.12 — parser pinned against REAL Deckbox exports.

Fixtures in `tests/fixtures/` are trimmed from actual `Inventory_*.csv` /
`Wishlist_*.csv` files exported from Deckbox (2597 and 1235 data rows
respectively), keeping the exact 29- and 26-column headers plus the rows
that exercise the interesting branches: a split-card name, a foil row, a
duplicated (Name, Edition) pair, and the wishlist's all-blank printing
columns.

Everything the earlier tests asserted about Deckbox's column layout was
guesswork until these landed.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from biblioplex.inventory.deckbox import ParsedDeckboxItem, parse_deckbox_csv

FIXTURES = Path(__file__).parent / "fixtures"
INVENTORY_CSV = FIXTURES / "deckbox_inventory_sample.csv"
WISHLIST_CSV = FIXTURES / "deckbox_wishlist_sample.csv"


@pytest.fixture(scope="module")
def inventory_items() -> list[ParsedDeckboxItem]:
    items, warnings = parse_deckbox_csv(INVENTORY_CSV.read_text(encoding="utf-8"))
    assert warnings == []
    return items


@pytest.fixture(scope="module")
def wishlist_items() -> list[ParsedDeckboxItem]:
    items, warnings = parse_deckbox_csv(WISHLIST_CSV.read_text(encoding="utf-8"))
    assert warnings == []
    return items


def _by_name(items: list[ParsedDeckboxItem], name: str) -> ParsedDeckboxItem:
    hits = [i for i in items if i.card_name == name]
    assert len(hits) == 1, f"{name!r} -> {len(hits)} items"
    return hits[0]


def test_real_inventory_header_parses_without_warnings(
    inventory_items: list[ParsedDeckboxItem],
) -> None:
    """The real export is 29 columns wide; `Count`/`Name`/`Edition` are not
    the first three, and several columns are blank on most rows."""
    header = next(csv.reader(INVENTORY_CSV.read_text(encoding="utf-8").splitlines()))
    assert len(header) == 29
    assert header[0] == "Count"
    assert "Edition Code" in header
    assert "Scryfall ID" in header
    assert inventory_items


def test_edition_code_populates_set_code(
    inventory_items: list[ParsedDeckboxItem],
) -> None:
    """Regression: `set_code` was declared in the schema and on
    ParsedDeckboxItem but never read from the CSV — always NULL in the DB."""
    assert all(i.set_code for i in inventory_items)
    armed = _by_name(inventory_items, "Armed // Dangerous")
    assert armed.set_code == "dgm"
    assert armed.set_name == "Dragon's Maze"


def test_set_code_is_lowercased(inventory_items: list[ParsedDeckboxItem]) -> None:
    assert all(i.set_code == i.set_code.lower() for i in inventory_items if i.set_code)


def test_split_card_name_keeps_spaced_separator(
    inventory_items: list[ParsedDeckboxItem],
) -> None:
    """`Fire // Ice` must survive verbatim — the service's front-face
    fallback splits on ' // ' exactly."""
    assert _by_name(inventory_items, "Armed // Dangerous").card_name == "Armed // Dangerous"


def test_foil_column_uses_the_literal_string_foil(
    inventory_items: list[ParsedDeckboxItem],
) -> None:
    """Deckbox writes `foil`, not `1`/`true` — the value the parser's
    truthy set had only guessed at."""
    rows = list(csv.DictReader(INVENTORY_CSV.read_text(encoding="utf-8").splitlines()))
    foil_rows = [r for r in rows if (r.get("Foil") or "").strip()]
    assert foil_rows, "fixture must contain a foil row"
    assert foil_rows[0]["Foil"] == "foil"

    abrade = _by_name(inventory_items, "Abrade")
    assert abrade.foil_quantity == abrade.quantity == 1


def test_foil_quantity_never_exceeds_quantity(
    inventory_items: list[ParsedDeckboxItem],
    wishlist_items: list[ParsedDeckboxItem],
) -> None:
    """`quantity` is TOTAL owned and `foil_quantity` is the foil subset."""
    for item in [*inventory_items, *wishlist_items]:
        assert item.foil_quantity <= item.quantity


def test_duplicate_name_edition_rows_aggregate(
    inventory_items: list[ParsedDeckboxItem],
) -> None:
    """Deckbox emits one row per physical grouping, so the same card in the
    same edition appears more than once (different condition, foil, or just
    separate entries). They fold into a single summed item."""
    rows = list(csv.DictReader(INVENTORY_CSV.read_text(encoding="utf-8").splitlines()))
    raw = [r for r in rows if r["Name"] == "Astral Cornucopia"]
    assert len(raw) == 2, "fixture must contain the duplicated pair"

    folded = _by_name(inventory_items, "Astral Cornucopia")
    assert folded.quantity == sum(int(r["Count"]) for r in raw)


def test_real_wishlist_has_no_printing_columns(
    wishlist_items: list[ParsedDeckboxItem],
) -> None:
    """A Deckbox wishlist is printing-agnostic: Edition, Edition Code, Foil,
    Condition and Printing Id are blank on EVERY row. The parser must not
    require any of them."""
    rows = list(csv.DictReader(WISHLIST_CSV.read_text(encoding="utf-8").splitlines()))
    for col in ("Edition", "Edition Code", "Foil", "Condition", "Printing Id"):
        assert all(not (r.get(col) or "").strip() for r in rows), col

    assert wishlist_items
    assert all(i.set_name is None for i in wishlist_items)
    assert all(i.set_code is None for i in wishlist_items)
    assert all(i.foil_quantity == 0 for i in wishlist_items)


def test_wishlist_split_card_with_comma_survives_quoting(
    wishlist_items: list[ParsedDeckboxItem],
) -> None:
    """`"Aclazotz, Deepest Betrayal // Temple of the Dead"` is CSV-quoted
    because the name contains a comma AND a split separator."""
    item = _by_name(wishlist_items, "Aclazotz, Deepest Betrayal // Temple of the Dead")
    assert item.quantity == 1


def test_price_column_parsed_from_real_export(
    inventory_items: list[ParsedDeckboxItem],
    wishlist_items: list[ParsedDeckboxItem],
) -> None:
    """D18.13 — Deckbox reports a price for the printing actually held.
    `cards.price_usd` is the CHEAPEST printing across the whole oracle
    (D14.11), so joining to it under-values a real collection."""
    assert all(i.price_usd is not None for i in inventory_items)
    assert all(i.price_usd is not None for i in wishlist_items)
    assert all((i.price_usd or 0) >= 0 for i in inventory_items)


def test_price_strips_currency_and_thousands_separator() -> None:
    from biblioplex.inventory.deckbox import _parse_money

    assert _parse_money("$0.15") == 0.15
    assert _parse_money("$1,234.56") == 1234.56
    assert _parse_money("") is None
    assert _parse_money(None) is None
    assert _parse_money("n/a") is None


def test_folded_duplicate_keeps_unit_price_not_a_sum(
    inventory_items: list[ParsedDeckboxItem],
) -> None:
    """Two rows of one card in one edition differ by condition, so the
    folded item's price stays a UNIT price — summing would double it."""
    rows = list(csv.DictReader(INVENTORY_CSV.read_text(encoding="utf-8").splitlines()))
    raw = [r for r in rows if r["Name"] == "Astral Cornucopia"]
    unit_prices = [
        float((r["Price"] or "$0").lstrip("$").replace(",", "")) for r in raw
    ]
    folded = _by_name(inventory_items, "Astral Cornucopia")
    assert folded.price_usd == max(unit_prices)
    assert folded.price_usd < sum(unit_prices)
