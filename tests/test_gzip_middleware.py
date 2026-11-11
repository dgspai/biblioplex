"""GZipMiddleware wiring (D15.1).

Large `/cards/search` responses should carry `Content-Encoding: gzip`;
single-card GETs (small bodies) should not — the `minimum_size=1000`
floor prevents wasting CPU on payloads that don't shrink meaningfully.
"""

from __future__ import annotations

from fastapi import FastAPI

from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine

from ._search_helpers import FakeConn, FakeEngine, client, make_row, settings


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


def _big_raw(i: int) -> dict[str, object]:
    return {
        "object": "card",
        "id": f"00000000-0000-0000-0000-{i:012d}",
        "name": f"Card {i}",
        "oracle_text": "Draw a card. " * 40,
        "mana_cost": "{1}{U}",
        "type_line": "Instant",
        "colors": ["U"],
        "color_identity": ["U"],
    }


async def test_large_search_response_carries_gzip() -> None:
    app = create_app(settings=settings())
    rows = [make_row(_big_raw(i), total=25) for i in range(25)]
    _override(app, FakeConn(rows=rows))
    async for c in client(app):
        resp = await c.get(
            "/cards/search",
            params={"q": "u"},
            headers={"Accept-Encoding": "gzip"},
        )
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"


async def test_small_response_not_compressed() -> None:
    """Single tiny row under the 1000-byte floor → no compression."""
    app = create_app(settings=settings())
    _override(app, FakeConn(rows=[make_row({"name": "X"}, total=1)]))
    async for c in client(app):
        resp = await c.get(
            "/cards/search",
            params={"q": "x"},
            headers={"Accept-Encoding": "gzip"},
        )
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") != "gzip"
