"""D19.44 — `GET /decks?completion=complete|incomplete`.

Filters the library by whether you already own every card. Rides D19.21's
coverage CTE rather than computing ownership a second time, so the filter
and the `coverage_pct` shown on each row can never disagree.
"""

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
    mappings.one.return_value = rows[0] if rows else {"total": 0}
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


async def _sql_for(**kwargs: Any) -> list[str]:
    """Every statement `list_decks` emits, in order."""
    conn = _conn(_result([{"total": 0}]))
    await deck_service.list_decks(conn, **kwargs)
    return [str(c.args[0]) for c in conn.execute.call_args_list]


# ---- the filter itself ----


@pytest.mark.asyncio
async def test_complete_matches_only_fully_owned_decks() -> None:
    sql = " ".join(await _sql_for(completion="complete"))
    assert "cov.owned >= cov.total" in sql
    assert "cov.total > 0" in sql, (
        "a deck with no indexed rows must not count as complete — "
        "'not indexed yet' is not 'you own all of it' (D19.21's rule)"
    )


@pytest.mark.asyncio
async def test_incomplete_matches_decks_with_a_gap() -> None:
    sql = " ".join(await _sql_for(completion="incomplete"))
    assert "cov.owned < cov.total" in sql


@pytest.mark.asyncio
async def test_unfiltered_listing_does_not_join_coverage_for_the_count() -> None:
    """The cheap COUNT stays cheap when nobody asked for the filter."""
    stmts = await _sql_for()
    count_sql = next(s for s in stmts if "COUNT(*)" in s)
    assert "cov" not in count_sql


@pytest.mark.asyncio
async def test_filtered_count_joins_coverage() -> None:
    """The trap: `where` is shared, but the COUNT has its own FROM clause.

    `SELECT COUNT(*) FROM decks d {where}` does not join `cov`. Adding a
    coverage predicate to the shared clause list without also joining it
    into the count makes the count query reference a missing alias — the
    listing would 500, or worse, report a total that disagrees with the
    rows it returned.
    """
    stmts = await _sql_for(completion="complete")
    count_sql = next(s for s in stmts if "COUNT(*)" in s)
    assert "LEFT JOIN cov" in count_sql
    assert "WITH cov" in count_sql or "cov AS" in count_sql


@pytest.mark.asyncio
async def test_unknown_completion_raises() -> None:
    """Whitelist, never interpolation — the value reaches SQL as text."""
    with pytest.raises(ValueError):
        await _sql_for(completion="'; DROP TABLE decks; --")


@pytest.mark.asyncio
async def test_completion_composes_with_the_other_filters() -> None:
    sql = " ".join(await _sql_for(completion="incomplete", q="urza", folder="Commander"))
    assert "d.name ILIKE" in sql
    assert "d.folder = :folder" in sql
    assert "cov.owned < cov.total" in sql


@pytest.mark.asyncio
async def test_filter_does_not_change_the_default_sort() -> None:
    """Filtering is not sorting; the default order must be untouched."""
    plain = await _sql_for()
    filtered = await _sql_for(completion="complete")
    order = lambda s: s[s.index("ORDER BY") :]  # noqa: E731
    assert order(plain[-1]) == order(filtered[-1])


# ---- routing ----


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
    a = create_app(Settings(database_url="postgresql+psycopg://u:p@h/db"))
    a.state.read_engine = _mock_engine()
    return a


@pytest.mark.asyncio
async def test_route_forwards_completion(app: FastAPI) -> None:
    with patch.object(deck_service, "list_decks", new=AsyncMock(return_value={
        "object": "list", "data": [], "total_cards": 0, "has_more": False,
    })) as spy:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            res = await c.get("/decks?completion=complete")
    assert res.status_code == 200
    assert spy.await_args.kwargs["completion"] == "complete"


@pytest.mark.asyncio
async def test_route_422s_on_a_bad_completion(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        res = await c.get("/decks?completion=mostly")
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_completion_is_optional(app: FastAPI) -> None:
    with patch.object(deck_service, "list_decks", new=AsyncMock(return_value={
        "object": "list", "data": [], "total_cards": 0, "has_more": False,
    })) as spy:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            res = await c.get("/decks")
    assert res.status_code == 200
    assert spy.await_args.kwargs["completion"] is None
