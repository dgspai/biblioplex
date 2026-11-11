"""HTTP cache headers on `/cards/search`.

Search results depend on user-mutable data (inventory, wishlist, label
overrides: `owned:`, `is:wanted`, `pred:`), none of which a validator can
cheaply fold in. So responses are `no-cache` with no ETag, and
`If-None-Match` is ignored: a 304 would serve stale ownership results.

Error paths (404 / 422) carry no cache headers at all.
"""

from __future__ import annotations

from fastapi import FastAPI

from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine

from ._search_helpers import FakeConn, FakeEngine, client, make_row, settings


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


async def test_200_is_no_cache_without_etag() -> None:
    app = create_app(settings=settings())
    _override(app, FakeConn(rows=[make_row({"name": "Bolt"}, total=1)]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "bolt"})
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"
    assert "etag" not in resp.headers


async def test_if_none_match_is_ignored() -> None:
    """A client replaying any validator still gets a fresh 200 + DB hit."""
    app = create_app(settings=settings())
    conn = FakeConn(rows=[make_row({"name": "Bolt"}, total=1)])
    _override(app, conn)
    async for c in client(app):
        await c.get("/cards/search", params={"q": "bolt"})
        resp = await c.get(
            "/cards/search",
            params={"q": "bolt"},
            headers={"If-None-Match": 'W/"anything", *'},
        )
    assert resp.status_code == 200
    assert len(conn.executed) == 2


async def test_404_has_no_cache_headers() -> None:
    """`total_cards == 0` → 404 per §6.1. No cache headers on the empty."""
    app = create_app(settings=settings())
    _override(app, FakeConn(rows=[]))  # empty result → 404
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "nothing"})
    assert resp.status_code == 404
    assert "cache-control" not in {k.lower() for k in resp.headers}
    assert "etag" not in {k.lower() for k in resp.headers}


async def test_422_has_no_cache_headers() -> None:
    app = create_app(settings=settings())
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": ""})
    assert resp.status_code == 422
    assert "cache-control" not in {k.lower() for k in resp.headers}
    assert "etag" not in {k.lower() for k in resp.headers}
