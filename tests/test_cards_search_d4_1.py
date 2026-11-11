"""D4.1 — ``GET /cards/search`` wiring (tech_spec.md §6.1).

Covers: DSL parse + AST compile + WHERE assembly + LIMIT 175. Asserts that
``UnsupportedOperatorError`` (raised by D3.15 for §5.2 keys) and DSL
``ParseError`` (parser.py) both surface as Scryfall-shaped 422 bodies.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine

from ._search_helpers import (
    FakeConn,
    FakeEngine,
    client,
    make_row,
    settings,
)


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=settings())


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


SAMPLE_RAW: dict[str, Any] = {
    "object": "card",
    "id": "a",
    "name": "Llanowar Elves",
}


async def test_search_happy_path_returns_200(app: FastAPI) -> None:
    """A valid query returns 200 — minimal liveness for D4.1."""
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, total=1)])
    _override(app, conn)

    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "llanowar"})

    assert resp.status_code == 200


async def test_search_compiles_dsl_into_where_with_limit_175(app: FastAPI) -> None:
    """The endpoint must compile the DSL into a WHERE and apply LIMIT 175.

    Asserts on the rendered SQL string. Substring checks keep the test
    robust against SA's parameter-binding choices.
    """
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, total=1)])
    _override(app, conn)

    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "t:creature"})

    assert resp.status_code == 200
    assert conn.executed, "DB execute() never reached"
    sql = str(conn.executed[-1][0]).lower()
    assert "from cards" in sql
    assert "where" in sql
    assert "type_search" in sql  # `t:creature` hits type_search
    assert "limit" in sql


async def test_search_unsupported_operator_returns_422(app: FastAPI) -> None:
    """§5.2 rejected key → D3.15 raises → endpoint maps to 422."""
    conn = FakeConn(rows=[])
    _override(app, conn)

    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "lang:en"})

    assert resp.status_code == 422
    body = resp.json()
    assert body["object"] == "error"
    assert body["status"] == 422
    assert "unsupported operator" in body["details"]
    assert "lang" in body["details"]


async def test_search_parse_error_returns_422(app: FastAPI) -> None:
    """Unbalanced parens → parser raises → 422."""
    conn = FakeConn(rows=[])
    _override(app, conn)

    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "(unbalanced"})

    assert resp.status_code == 422
    body = resp.json()
    assert body["object"] == "error"
    assert body["status"] == 422
