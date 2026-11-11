"""D15.15 — `/cards/search?count=false` fast-path.

Skips the `COUNT(*) OVER ()` window on paginated agents that don't need
`total_cards` on every page. Response reports `total_cards=null` and
derives `has_more` from `len(data) == SEARCH_PAGE_SIZE`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from biblioplex.main import create_app
from biblioplex.routers.cards import SEARCH_PAGE_SIZE, get_read_engine

from ._search_helpers import (
    FakeConn,
    FakeEngine,
    client,
    make_row,
    settings,
)

SAMPLE_RAW: dict[str, Any] = {"object": "card", "id": "a", "name": "Llanowar Elves"}


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=settings())


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


async def test_count_false_drops_count_window(app: FastAPI) -> None:
    """D15.15: `?count=false` omits `count(*) over ()` from the rendered SQL."""
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, 1)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "elf", "count": "false"})
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["total_cards"] is None
    assert body["has_more"] is False  # len(data) < SEARCH_PAGE_SIZE
    sql = str(conn.executed[-1][0]).lower()
    assert "count(*) over ()" not in sql


async def test_count_true_default_keeps_count_window(app: FastAPI) -> None:
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, 42)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "elf"})
    body = resp.json()
    assert body["total_cards"] == 42
    sql = str(conn.executed[-1][0]).lower()
    assert "count(*) over ()" in sql


async def test_count_false_full_page_reports_has_more(app: FastAPI) -> None:
    """`has_more` when the response is a FULL page and count=false."""
    rows = [make_row(SAMPLE_RAW, 0) for _ in range(SEARCH_PAGE_SIZE)]
    conn = FakeConn(rows=rows)
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "elf", "count": "false"})
    body = resp.json()
    assert body["has_more"] is True
    assert body["next_page"].endswith("page=2&count=false") or "page=2" in body["next_page"]


async def test_count_false_empty_page_no_404(app: FastAPI) -> None:
    """Empty result under `count=false` returns 200 with empty data — no 404."""
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "elf", "count": "false"})
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["data"] == []
    assert body["total_cards"] is None
    assert body["has_more"] is False
