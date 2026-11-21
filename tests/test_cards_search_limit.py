"""`/cards/search?limit=` caps the page; it was silently ignored."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from biblioplex.main import create_app
from biblioplex.routers.cards import SEARCH_PAGE_SIZE, get_read_engine

from ._search_helpers import FakeConn, FakeEngine, client, make_row, settings

SAMPLE_RAW: dict[str, Any] = {"object": "card", "id": "a", "name": "Llanowar Elves"}


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=settings())


async def _get(app: FastAPI, conn: FakeConn, **params: Any) -> Any:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)
    async for c in client(app):
        return await c.get("/cards/search", params={"q": "elf", **params})


async def test_limit_caps_sql_and_pages_by_limit(app: FastAPI) -> None:
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, 42) for _ in range(5)])
    resp = await _get(app, conn, limit=5, page=2)
    body = resp.json()
    assert body["has_more"] is True  # 42 > 2 * 5
    assert "limit=5" in body["next_page"] and "page=3" in body["next_page"]
    stmt = conn.executed[-1][0]
    compiled = stmt.compile(compile_kwargs={"literal_binds": True}) if hasattr(stmt, "compile") else stmt
    sql = str(compiled).lower()
    assert "limit 5" in sql and "offset 5" in sql


async def test_limit_full_page_without_count_reports_more(app: FastAPI) -> None:
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, 0) for _ in range(3)])
    body = (await _get(app, conn, limit=3, count="false")).json()
    assert body["has_more"] is True


async def test_limit_above_page_size_is_rejected(app: FastAPI) -> None:
    resp = await _get(app, FakeConn(rows=[]), limit=SEARCH_PAGE_SIZE + 1)
    assert resp.status_code == 422
