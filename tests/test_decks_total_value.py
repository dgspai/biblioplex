"""D19.30 — whole-deck value in the card-table summary."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from biblioplex.decks import service as deck_service


def _result(rows: list[dict[str, Any]] | None = None) -> MagicMock:
    rows = rows or []
    mappings = MagicMock()
    mappings.all.return_value = rows
    mappings.one.return_value = rows[0] if rows else None
    mappings.one_or_none.return_value = rows[0] if rows else None
    res = MagicMock()
    res.mappings.return_value = mappings
    return res


def _conn(*results: MagicMock) -> AsyncMock:
    conn = AsyncMock()
    it = iter(results)

    def _next(*_a: Any, **_k: Any) -> Any:
        try:
            return next(it)
        except StopIteration:
            return _result()

    conn.execute.side_effect = _next
    return conn


DECK_ID = "11111111-1111-1111-1111-111111111111"


def _card(**kw: Any) -> dict[str, Any]:
    base = {
        "section": "main", "card_name": "Sol Ring", "qty": 1,
        "oracle_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "name": "Sol Ring",
        "type_line": "Artifact", "mana_cost": "{1}", "cmc": 1,
        "price_usd": None, "qty_owned": 0, "on_wishlist": False,
    }
    base.update(kw)
    if "card_name" in kw and "name" not in kw:
        base["name"] = kw["card_name"]
    return base


async def _summary(cards: list[dict[str, Any]]) -> dict[str, Any]:
    conn = _conn(_result([{"id": DECK_ID}]), _result(cards))
    out = await deck_service.list_deck_cards(conn, DECK_ID)
    assert out is not None
    return out["summary"]


# ---- qty weighting ----


@pytest.mark.asyncio
async def test_value_is_quantity_weighted() -> None:
    """4x a $3 card is $12, not $3.

    A per-distinct-card sum looks plausible and is silently wrong on every
    playset — the exact failure this pin exists to catch.
    """
    s = await _summary([_card(card_name="Bolt", qty=4, price_usd=3.0)])
    assert s["total_usd"] == 12.0


@pytest.mark.asyncio
async def test_value_spans_every_section() -> None:
    """Commander and sideboard are part of what the deck is worth."""
    s = await _summary([
        _card(card_name="Cmd", section="commander", qty=1, price_usd=10.0),
        _card(card_name="Main", section="main", qty=2, price_usd=1.5),
        _card(card_name="Side", section="sideboard", qty=1, price_usd=4.0),
    ])
    assert s["total_usd"] == 17.0


@pytest.mark.asyncio
async def test_value_counts_cards_you_already_own() -> None:
    """Deck VALUE is what the deck is worth, not what it would cost to
    finish — owning a card does not make it worthless."""
    s = await _summary([_card(card_name="Owned", qty=1, qty_owned=1, price_usd=50.0)])
    assert s["total_usd"] == 50.0
    assert s["missing_usd"] == 0.0


# ---- owned is derivable, deliberately not a field ----


@pytest.mark.asyncio
async def test_owned_value_is_derivable_and_not_a_field() -> None:
    """`total_usd - missing_usd` IS the owned value.

    Shipping it as a third field would create a number that can drift out
    of agreement with the other two.
    """
    s = await _summary([
        _card(card_name="Have", qty=1, qty_owned=1, price_usd=30.0),
        _card(card_name="Need", qty=2, qty_owned=0, price_usd=5.0),
    ])
    assert s["total_usd"] == 40.0
    assert s["missing_usd"] == 10.0
    assert round(s["total_usd"] - s["missing_usd"], 2) == 30.0
    assert "owned_usd" not in s


# ---- unpriced honesty, per D19.28's rule ----


@pytest.mark.asyncio
async def test_unpriced_cards_are_counted_not_charged_at_zero() -> None:
    s = await _summary([
        _card(card_name="Priced", qty=1, price_usd=7.5),
        _card(card_name="Unpriced", qty=1, price_usd=None),
    ])
    assert s["total_usd"] == 7.5
    assert s["unpriced_total"] == 1


@pytest.mark.asyncio
async def test_a_wholly_unpriced_deck_is_not_worth_zero_silently() -> None:
    """$0.00 must arrive with the caveat that says why."""
    s = await _summary([
        _card(card_name="A", qty=1, price_usd=None),
        _card(card_name="B", qty=3, price_usd=None),
    ])
    assert s["total_usd"] == 0.0
    assert s["unpriced_total"] == 2, "counted per distinct card, not per copy"


@pytest.mark.asyncio
async def test_unpriced_total_counts_owned_cards_too() -> None:
    """`unpriced_missing` only sees gaps; `unpriced_total` sees the deck."""
    s = await _summary([_card(card_name="OwnedUnpriced", qty=1, qty_owned=1, price_usd=None)])
    assert s["unpriced_total"] == 1
    assert s["unpriced_missing"] == 0


@pytest.mark.asyncio
async def test_fully_priced_deck_reports_no_unpriced() -> None:
    s = await _summary([_card(qty=1, price_usd=2.0)])
    assert s["unpriced_total"] == 0


# ---- edges ----


@pytest.mark.asyncio
async def test_empty_deck_is_worth_zero() -> None:
    s = await _summary([])
    assert s["total_usd"] == 0.0
    assert s["unpriced_total"] == 0


@pytest.mark.asyncio
async def test_value_is_rounded_to_cents() -> None:
    """Float accumulation must not surface 12.000000000000002."""
    s = await _summary([_card(card_name="Odd", qty=3, price_usd=0.1)])
    assert s["total_usd"] == 0.3


@pytest.mark.asyncio
async def test_total_is_never_less_than_missing() -> None:
    """An invariant the UI's derived "owned" depends on."""
    s = await _summary([
        _card(card_name="A", qty=4, qty_owned=1, price_usd=2.0),
        _card(card_name="B", qty=1, qty_owned=0, price_usd=9.0),
    ])
    assert s["total_usd"] >= s["missing_usd"]
    assert s["total_usd"] == 17.0
    assert s["missing_usd"] == 15.0
