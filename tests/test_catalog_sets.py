"""D17.4 ``GET /sets`` — Scryfall set catalog with hit counts.

Enumerates every set present in the corpus with ``{code, name,
released_at, count}``. Agents building `set:war` DSL queries can look
up the canonical short-code plus the release date to disambiguate
promo/masterpiece printings that share a base set name.

Ordering by ``released_at DESC NULLS LAST`` so the most recent set
surfaces first — agents asking "what's the latest set?" get the answer
in row 0.
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


def _row(code: str, name: str, released: str | None, n: int) -> dict[str, Any]:
    return {"code": code, "name": name, "released_at": released, "count": n}


async def test_sets_envelope_shape(app: FastAPI) -> None:
    conn = FakeConn(
        rows=[
            _row("blb", "Bloomburrow", "2024-08-02", 271),
            _row("mh3", "Modern Horizons 3", "2024-06-14", 303),
        ]
    )
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/sets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    first = body["data"][0]
    assert first["code"] == "blb"
    assert first["name"] == "Bloomburrow"
    assert first["released_at"] == "2024-08-02"
    assert first["count"] == 271


async def test_sets_orders_by_released_desc_nulls_last(app: FastAPI) -> None:
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        await c.get("/sets")
    sql = str(conn.executed[-1][0]).lower()
    assert "order by" in sql
    assert "released_at" in sql
    assert "desc" in sql
    assert "nulls last" in sql
    assert "group by" in sql


async def test_sets_no_prefix_no_limit_params(app: FastAPI) -> None:
    """``/sets`` has no prefix/limit — the corpus is <1k sets total.

    Deliberately simpler than /tags and /keywords: bounded universe,
    no need for pagination. Agents get the whole list in one payload.
    """
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        # Extra params silently ignored (FastAPI default) — the endpoint
        # doesn't declare them.
        resp = await c.get("/sets")
    assert resp.status_code == 200


async def test_sets_etag_present(app: FastAPI) -> None:
    conn = FakeConn(rows=[_row("blb", "Bloomburrow", "2024-08-02", 271)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/sets")
    etag = resp.headers.get("etag", "")
    assert etag.startswith('W/"')


async def test_sets_epoch_bump_changes_etag(app: FastAPI) -> None:
    conn = FakeConn(rows=[_row("blb", "Bloomburrow", "2024-08-02", 271)])
    _override(app, conn)
    async for c in client(app):
        r1 = await c.get("/sets")
        app.state.refresh_status.mark_finished()
        r2 = await c.get("/sets")
    assert r1.headers["etag"] != r2.headers["etag"]
