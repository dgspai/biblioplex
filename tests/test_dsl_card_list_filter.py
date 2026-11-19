"""Inventory and wishlist filters: DSL when a known key is used, else name text."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from biblioplex.dsl.card_list_filter import card_list_where, dsl_select


@pytest.mark.parametrize(
    "q",
    [
        "sol",
        "Sol Ring",
        "Circle of Protection: Red",
        "Fire // Ice",
        "-sol",
        "t:dragon cmc<x",
        "block:ktk",
    ],
)
def test_plain_or_broken_queries_stay_name_text(q: str) -> None:
    assert dsl_select(q, "inventory") is None


@pytest.mark.parametrize("q", ["t:dragon", "pred:ramp usd<2", "sol c:c", "-t:land"])
def test_known_key_queries_compile(q: str) -> None:
    sql = str(dsl_select(q, "inventory"))
    assert "FROM cards" in sql
    assert "FROM inventory" in sql


def _conn_returning(ids: list[str]) -> AsyncMock:
    conn = AsyncMock()
    conn.execute.return_value = AsyncMock(scalars=lambda: AsyncMock(all=lambda: ids))
    return conn


@pytest.mark.asyncio
async def test_name_text_keeps_ilike() -> None:
    params: dict[str, Any] = {}
    conn = _conn_returning([])
    where = await card_list_where(conn, "Fire // Ice", "w", "wishlist", params)
    assert where == "WHERE w.card_name ILIKE :q "
    assert params == {"q": "%Fire // Ice%"}
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_dsl_resolves_ids_once() -> None:
    params: dict[str, Any] = {}
    conn = _conn_returning(["11111111-1111-1111-1111-111111111111"])
    where = await card_list_where(conn, "t:artifact", "i", "inventory", params)
    assert where == "WHERE i.oracle_id = ANY(CAST(:ids AS uuid[])) "
    assert params == {"ids": ["11111111-1111-1111-1111-111111111111"]}
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_query_has_no_where() -> None:
    params: dict[str, Any] = {}
    assert await card_list_where(_conn_returning([]), "  ", "i", "inventory", params) == ""
    assert params == {}
