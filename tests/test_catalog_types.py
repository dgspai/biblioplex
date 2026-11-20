"""``GET /types`` — every type-line word with its card count, for `t:` autocomplete."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from biblioplex.main import create_app
from biblioplex.routers.catalog import get_read_engine

from ._search_helpers import FakeConn, FakeEngine, client, settings


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=settings())


async def _get(app: FastAPI, conn: FakeConn, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)
    async for c in client(app):
        return await c.get("/types", headers=headers or {})


async def test_types_envelope(app: FastAPI) -> None:
    conn = FakeConn(rows=[{"type": "Creature", "count": 17000}, {"type": "Dragon", "count": 600}])
    resp = await _get(app, conn)
    assert resp.status_code == 200
    assert resp.json() == {
        "object": "list",
        "data": [{"type": "Creature", "count": 17000}, {"type": "Dragon", "count": 600}],
    }


async def test_types_splits_type_line_on_dash_and_faces(app: FastAPI) -> None:
    conn = FakeConn(rows=[])
    await _get(app, conn)
    sql = str(conn.executed[-1][0])
    assert "regexp_split_to_table" in sql
    assert "—" in sql and "/" in sql
    assert "ORDER BY count(*) DESC" in sql


async def test_types_etag_304(app: FastAPI) -> None:
    resp = await _get(app, FakeConn(rows=[]))
    etag = resp.headers["ETag"]
    again = await _get(app, FakeConn(rows=[]), {"If-None-Match": etag})
    assert again.status_code == 304
