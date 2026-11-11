"""D17.4 ``GET /keywords`` — Scryfall keyword catalog with hit counts.

Complements ``/tags`` for the Scryfall ``keywords`` array (``Flying``,
``Trample``, ``Ward``, etc.). Extracts via
``jsonb_array_elements_text(raw->'keywords')`` so agents can
autocomplete the corpus-canonical casing before crafting a
``keyword:flying`` DSL query (D15.30).

Response envelope mirrors ``/tags``: ``{keyword, count}`` per row.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from biblioplex.main import create_app
from biblioplex.routers.catalog import get_read_engine

from ._search_helpers import FakeConn, FakeEngine, client, settings


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=settings())


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


def _row(kw: str, n: int) -> dict[str, Any]:
    return {"keyword": kw, "count": n}


async def test_keywords_envelope_shape(app: FastAPI) -> None:
    conn = FakeConn(rows=[_row("Flying", 1234), _row("Trample", 543)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/keywords")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"][0] == {"keyword": "Flying", "count": 1234}
    assert body["has_more"] is False


async def test_keywords_source_is_jsonb_array_elements(app: FastAPI) -> None:
    """SQL fans ``raw->'keywords'`` out via ``jsonb_array_elements_text``."""
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        await c.get("/keywords")
    sql = str(conn.executed[-1][0]).lower()
    assert "jsonb_array_elements_text" in sql
    assert "keywords" in sql


async def test_keywords_prefix_ilike(app: FastAPI) -> None:
    conn = FakeConn(rows=[_row("Flying", 1234)])
    _override(app, conn)
    async for c in client(app):
        await c.get("/keywords", params={"prefix": "fly"})
    stmt, params = conn.executed[-1]
    sql = str(stmt).lower()
    assert "ilike" in sql
    assert params is not None
    assert params.get("prefix") == "fly"


async def test_keywords_limit_clamp_low(app: FastAPI) -> None:
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/keywords", params={"limit": 0})
    assert resp.status_code == 422


async def test_keywords_limit_clamp_high(app: FastAPI) -> None:
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/keywords", params={"limit": 501})
    assert resp.status_code == 422


async def test_keywords_etag_present(app: FastAPI) -> None:
    conn = FakeConn(rows=[_row("Flying", 1)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/keywords")
    etag = resp.headers.get("etag", "")
    assert etag.startswith('W/"')
