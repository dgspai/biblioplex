"""D17.4 ``GET /tags`` — oracle-tag slug catalog.

Biggest MCP blocker before D17.4 was that agents couldn't guess
``otag:aristocrats`` vs ``otag:aristocrat``: the DSL happily accepts
either but only one hits any rows. This endpoint enumerates every slug
present in the ``oracle_tags`` side table with a hit count, so agents
can (a) discover the correct slug via prefix search, (b) rank suggestions
by how many cards actually carry each tag.

Response is a Scryfall-shaped ``List`` envelope with per-row
``{slug, count}``. Optional ``prefix=`` narrows via ILIKE.
``limit`` clamps ``[1, 500]``; response cached via weak ETag keyed on
refresh epoch (D13.5 pattern).
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


def _row(slug: str, n: int) -> dict[str, Any]:
    return {"slug": slug, "count": n}


async def test_tags_envelope_shape(app: FastAPI) -> None:
    """Envelope is ``{object: "list", data: [{slug, count}], has_more}``."""
    conn = FakeConn(rows=[_row("aristocrats", 128), _row("ramp", 512)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/tags")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    assert body["data"][0] == {"slug": "aristocrats", "count": 128}
    assert body["has_more"] is False


async def test_tags_prefix_filter_binds_ilike(app: FastAPI) -> None:
    """``?prefix=arist`` compiles to ``tag ILIKE :prefix || '%'``."""
    conn = FakeConn(rows=[_row("aristocrats", 128)])
    _override(app, conn)
    async for c in client(app):
        await c.get("/tags", params={"prefix": "arist"})
    stmt, params = conn.executed[-1]
    sql = str(stmt).lower()
    assert "ilike" in sql
    assert params is not None
    assert params.get("prefix") == "arist"


async def test_tags_orders_by_count_desc_then_slug(app: FastAPI) -> None:
    """Ordering: ``count DESC, tag ASC``."""
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        await c.get("/tags")
    sql = str(conn.executed[-1][0]).lower()
    assert "order by" in sql
    assert "desc" in sql
    # Group-by on tag so per-slug counts collapse.
    assert "group by" in sql


async def test_tags_limit_clamped_low(app: FastAPI) -> None:
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/tags", params={"limit": 0})
    assert resp.status_code == 422


async def test_tags_limit_clamped_high(app: FastAPI) -> None:
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/tags", params={"limit": 501})
    assert resp.status_code == 422


async def test_tags_empty_result(app: FastAPI) -> None:
    """Zero rows → 200 with empty data (unknown prefix is not an error)."""
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/tags", params={"prefix": "zzz-unknown"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["has_more"] is False


async def test_tags_etag_present_and_refresh_epoch_folded(app: FastAPI) -> None:
    """Weak ETag on 200; epoch bump changes the tag (D13.5 pattern)."""
    conn = FakeConn(rows=[_row("aristocrats", 1)])
    _override(app, conn)
    async for c in client(app):
        r1 = await c.get("/tags")
        etag1 = r1.headers.get("etag", "")
        assert etag1.startswith('W/"')
        app.state.refresh_status.mark_finished()
        r2 = await c.get("/tags")
        etag2 = r2.headers.get("etag", "")
    assert etag1 != etag2


async def test_tags_if_none_match_returns_304(app: FastAPI) -> None:
    """Conditional revalidation short-circuits: same ETag → 304 no body."""
    conn = FakeConn(rows=[_row("aristocrats", 1)])
    _override(app, conn)
    async for c in client(app):
        r1 = await c.get("/tags")
        etag = r1.headers["etag"]
        assert len(conn.executed) == 1
        r2 = await c.get("/tags", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""
    # DB not re-hit on the 304 path.
    assert len(conn.executed) == 1
