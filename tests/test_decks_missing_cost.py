"""D19.28 — rough cost of a deck's missing cards, and `sort=missing_cost`."""

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


def _rows_sql(conn: AsyncMock) -> str:
    return str(conn.execute.await_args_list[1].args[0])


DECK_ROW = {
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "Iroh gift", "format": "commander", "commander": None, "companion": None,
    "notes": None, "deckbox_url": None, "moxfield_url": None, "archidekt_url": None,
    "folder": None,
    "created_at": MagicMock(isoformat=lambda: "2026-08-19T00:00:00+00:00"),
    "updated_at": MagicMock(isoformat=lambda: "2026-08-19T00:00:00+00:00"),
    "version": 1, "decklist": "1 Sol Ring\n", "total_cards": 100,
    "distinct_cards": 100, "unresolved_count": 0,
    "version_created_at": MagicMock(isoformat=lambda: "2026-08-19T00:00:00+00:00"),
    "cov_total": 100, "cov_owned": 56,
    "cov_missing_usd": 396.94, "cov_unpriced_missing": 0, "cov_unresolved": 0,
}


async def _list(**kw: Any) -> tuple[dict[str, Any], AsyncMock]:
    conn = _conn(_result([{"total": 1}]), _result([DECK_ROW]))
    return await deck_service.list_decks(conn, **kw), conn


# ---- the cost computation ----


@pytest.mark.asyncio
async def test_cost_clamps_so_owning_extras_is_never_negative() -> None:
    """Mirrors D19.21's LEAST: owning 5 of a 1-of must cost 0, not -4 x price."""
    _out, conn = await _list(sort="missing_cost")
    assert "GREATEST(" in _rows_sql(conn)


@pytest.mark.asyncio
async def test_price_is_joined_laterally_not_by_a_plain_join() -> None:
    """`cards` can hold several rows per name (reversible faces, §3.4).

    A plain join would multiply a deck's rows and inflate the total.
    """
    _out, conn = await _list(sort="missing_cost")
    sql = _rows_sql(conn)
    assert "LEFT JOIN LATERAL" in sql
    assert "price_usd" in sql
    assert "LIMIT 1" in sql


@pytest.mark.asyncio
async def test_unpriced_missing_is_counted_separately() -> None:
    """Author decision: ~5% of the corpus has no price. Summing them as $0
    would make a deck missing three unpriced staples look cheap."""
    _out, conn = await _list(sort="missing_cost")
    sql = _rows_sql(conn)
    assert "price_usd IS NULL" in sql


@pytest.mark.asyncio
async def test_rows_carry_the_cost_and_the_caveat() -> None:
    out, _conn = await _list(sort="missing_cost")
    row = out["data"][0]
    assert row["missing_usd"] == 396.94
    assert row["unpriced_missing"] == 0


@pytest.mark.asyncio
async def test_cost_is_null_for_a_deck_the_index_has_not_covered() -> None:
    """`null`, never 0 — "not indexed" is not "costs nothing"."""
    conn = _conn(
        _result([{"total": 1}]),
        _result([{**DECK_ROW, "cov_total": None, "cov_owned": None,
                  "cov_missing_usd": None, "cov_unpriced_missing": None, "cov_unresolved": None}]),
    )
    row = (await deck_service.list_decks(conn, sort="missing_cost"))["data"][0]
    assert row["missing_usd"] is None
    assert row["unpriced_missing"] == 0


# ---- sorting ----


@pytest.mark.asyncio
async def test_missing_cost_is_a_known_sort_key() -> None:
    _out, conn = await _list(sort="missing_cost")
    order = _rows_sql(conn)
    order = order[order.index("ORDER BY"):]
    assert "missing_usd" in order


@pytest.mark.asyncio
async def test_missing_cost_defaults_to_cheapest_first() -> None:
    """Deliberately opposite to coverage's descending default — the question
    is "which deck can I finish cheaply?"."""
    _out, conn = await _list(sort="missing_cost")
    order = _rows_sql(conn)
    order = order[order.index("ORDER BY"):]
    assert "ASC" in order


@pytest.mark.asyncio
async def test_missing_cost_direction_can_be_reversed() -> None:
    _out, conn = await _list(sort="missing_cost", direction="desc")
    order = _rows_sql(conn)
    order = order[order.index("ORDER BY"):]
    assert "DESC" in order


@pytest.mark.asyncio
async def test_missing_cost_sorts_before_pagination() -> None:
    _out, conn = await _list(sort="missing_cost")
    sql = _rows_sql(conn)
    assert sql.index("ORDER BY") < sql.index("LIMIT :limit")


@pytest.mark.asyncio
async def test_missing_cost_puts_unknown_last() -> None:
    _out, conn = await _list(sort="missing_cost")
    order = _rows_sql(conn)
    order = order[order.index("ORDER BY"):]
    assert "NULLS LAST" in order


@pytest.mark.asyncio
async def test_uses_cast_not_double_colon() -> None:
    _out, conn = await _list(sort="missing_cost")
    assert "::" not in _rows_sql(conn)


# ---- deck card table summary ----


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


@pytest.mark.asyncio
async def test_card_summary_sums_only_the_missing_and_priced() -> None:
    conn = _conn(
        _result([{"id": DECK_ID}]),
        _result([
            _card(card_name="Owned", qty=1, qty_owned=1, price_usd=10.0),
            _card(card_name="Missing", qty=2, qty_owned=0, price_usd=3.5),
            _card(card_name="Unpriced", qty=1, qty_owned=0, price_usd=None),
        ]),
    )
    s = (await deck_service.list_deck_cards(conn, DECK_ID))["summary"]
    assert s["missing_usd"] == 7.0, "owned card must not be charged for"
    assert s["unpriced_missing"] == 1


@pytest.mark.asyncio
async def test_card_summary_cost_is_zero_for_a_complete_deck() -> None:
    conn = _conn(
        _result([{"id": DECK_ID}]),
        _result([_card(qty=1, qty_owned=1, price_usd=10.0)]),
    )
    s = (await deck_service.list_deck_cards(conn, DECK_ID))["summary"]
    assert s["missing_usd"] == 0.0
    assert s["unpriced_missing"] == 0
