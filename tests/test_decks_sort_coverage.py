"""D19.21 — library sorted by inventory coverage."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.decks import service as deck_service
from biblioplex.main import create_app


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
    """The second statement — the count query is first."""
    return str(conn.execute.await_args_list[1].args[0])


DECK_ROW = {
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "Iroh gift", "format": "commander", "commander": None, "companion": None,
    "notes": None, "deckbox_url": None, "moxfield_url": None, "archidekt_url": None,
    "folder": None,
    "created_at": MagicMock(isoformat=lambda: "2026-08-18T00:00:00+00:00"),
    "updated_at": MagicMock(isoformat=lambda: "2026-08-18T00:00:00+00:00"),
    "version": 1, "decklist": "1 Sol Ring\n", "total_cards": 100,
    "distinct_cards": 100, "unresolved_count": 0,
    "version_created_at": MagicMock(isoformat=lambda: "2026-08-18T00:00:00+00:00"),
    "cov_total": 100, "cov_owned": 56,
    "cov_missing_usd": 396.94, "cov_unpriced_missing": 0, "cov_unresolved": 0,
}


async def _list(**kw: Any) -> tuple[dict[str, Any], AsyncMock]:
    conn = _conn(_result([{"total": 1}]), _result([DECK_ROW]))
    out = await deck_service.list_decks(conn, **kw)
    return out, conn


# ---- ordering ----


@pytest.mark.asyncio
async def test_default_sort_is_unchanged() -> None:
    """Existing callers must not silently get a different order."""
    _out, conn = await _list()
    assert "ORDER BY d.updated_at DESC" in _rows_sql(conn)


@pytest.mark.asyncio
async def test_coverage_sort_orders_by_the_ratio_not_the_raw_count() -> None:
    """A 40/40 deck is fully built; a 60/100 deck is not.

    Ordering on owned cards alone would rank the bigger, less complete
    deck first.
    """
    _out, conn = await _list(sort="coverage")
    sql = _rows_sql(conn)
    assert "ORDER BY" in sql
    order = sql[sql.index("ORDER BY"):]
    assert "cov.owned" in order and "cov.total" in order


@pytest.mark.asyncio
async def test_coverage_sort_defaults_to_most_complete_first() -> None:
    _out, conn = await _list(sort="coverage")
    order = _rows_sql(conn)
    order = order[order.index("ORDER BY"):]
    assert "DESC" in order


@pytest.mark.asyncio
async def test_direction_can_be_reversed() -> None:
    """Ascending answers "what do I still need to buy?"."""
    _out, conn = await _list(sort="coverage", direction="asc")
    order = _rows_sql(conn)
    order = order[order.index("ORDER BY"):]
    assert "ASC" in order


@pytest.mark.asyncio
async def test_sort_is_applied_before_pagination() -> None:
    """Sorting after LIMIT would only reorder one page — page 2 is then
    meaningless, and the top-coverage deck may never appear at all."""
    _out, conn = await _list(sort="coverage")
    sql = _rows_sql(conn)
    assert sql.index("ORDER BY") < sql.index("LIMIT :limit")


@pytest.mark.asyncio
async def test_decks_with_no_indexed_cards_sort_last() -> None:
    """A deck predating the index has no rows; it must not outrank a real
    deck by being NULL."""
    _out, conn = await _list(sort="coverage")
    order = _rows_sql(conn)
    order = order[order.index("ORDER BY"):]
    assert "NULLS LAST" in order


@pytest.mark.asyncio
async def test_invalid_sort_key_is_rejected() -> None:
    conn = _conn(_result([{"total": 0}]), _result([]))
    with pytest.raises(ValueError):
        await deck_service.list_decks(conn, sort="'; DROP TABLE decks; --")


# ---- the coverage computation ----


@pytest.mark.asyncio
async def test_inventory_is_aggregated_once_not_per_deck_row() -> None:
    """A per-row LATERAL would do decks x cards index lookups per page."""
    _out, conn = await _list(sort="coverage")
    sql = _rows_sql(conn)
    assert "GROUP BY" in sql
    assert "FROM inventory" in sql


@pytest.mark.asyncio
async def test_ownership_is_clamped_per_card() -> None:
    """Owning 5 copies of a 1-of must contribute 1, not 5.

    Without LEAST a big collection pushes coverage above 100%.
    """
    _out, conn = await _list(sort="coverage")
    assert "LEAST(" in _rows_sql(conn)


@pytest.mark.asyncio
async def test_rows_carry_the_coverage_they_are_sorted_by() -> None:
    """Sorting by an invisible number is not usable."""
    out, _conn = await _list(sort="coverage")
    row = out["data"][0]
    assert row["owned_cards"] == 56
    assert row["coverage_pct"] == 56.0


@pytest.mark.asyncio
async def test_coverage_is_null_safe_for_an_unindexed_deck() -> None:
    conn = _conn(
        _result([{"total": 1}]),
        _result([{**DECK_ROW, "cov_total": None, "cov_owned": None,
                  "cov_missing_usd": None, "cov_unpriced_missing": None, "cov_unresolved": None}]),
    )
    out = await deck_service.list_decks(conn, sort="coverage")
    row = out["data"][0]
    assert row["owned_cards"] == 0
    assert row["coverage_pct"] is None


@pytest.mark.asyncio
async def test_uses_cast_not_double_colon() -> None:
    _out, conn = await _list(sort="coverage")
    assert "::" not in _rows_sql(conn)


# ---- router ----


def _placeholder_settings() -> Settings:
    return Settings.model_construct(
        database_url="postgresql+psycopg://placeholder/placeholder",
        hf_embed_model="m", hf_embed_dim=384, hf_embed_device="cpu",
        embed_batch=256, bulk_source_type="default_cards", web_concurrency=1,
        cors_origins="*", log_level="INFO",
    )


def _mock_engine() -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
    ctx.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = ctx
    engine.begin.return_value = ctx
    return engine


@pytest.fixture
def app() -> FastAPI:
    a = create_app(settings=_placeholder_settings())
    a.state.read_engine = _mock_engine()
    return a


async def _get(app: FastAPI, url: str) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(url)


@pytest.mark.asyncio
async def test_router_passes_sort_through(app: FastAPI) -> None:
    empty = {"object": "list", "data": [], "total_decks": 0, "page": 1, "limit": 50}
    with patch("biblioplex.routers.deck_library.deck_service.list_decks",
               new=AsyncMock(return_value=empty)) as spy:
        await _get(app, "/decks?sort=coverage&direction=asc")
    kw = spy.await_args.kwargs
    assert kw["sort"] == "coverage"
    assert kw["direction"] == "asc"


@pytest.mark.asyncio
async def test_router_422s_on_an_unknown_sort(app: FastAPI) -> None:
    resp = await _get(app, "/decks?sort=bogus")
    assert resp.status_code == 422
