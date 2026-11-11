"""D4.5 — ``unique=`` param for ``GET /cards/search`` (§6.1).

We dedupe to one row per ``oracle_id`` at ingest (§1.1), so ``cards``,
``prints``, and ``art`` are equivalent against this schema. The endpoint
accepts all three values silently (aliases ``prints`` / ``art`` to
``cards``). Any other value is rejected with 422.
"""

from __future__ import annotations

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

SAMPLE_RAW = {"object": "card", "id": "a", "name": "Llanowar Elves"}


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=settings())


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


@pytest.mark.parametrize("unique", ["cards", "prints", "art"])
async def test_unique_documented_values_accepted(
    app: FastAPI, unique: str
) -> None:
    """All three §6.1 values are accepted (aliased to cards)."""
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, total=1)])
    _override(app, conn)

    async for c in client(app):
        resp = await c.get(
            "/cards/search", params={"q": "elf", "unique": unique}
        )

    assert resp.status_code == 200


@pytest.mark.parametrize("unique", ["garbage", "Cards", "PRINTS", "anything-else"])
async def test_unique_invalid_returns_422(app: FastAPI, unique: str) -> None:
    """Anything outside the documented set → 422."""
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get(
            "/cards/search", params={"q": "elf", "unique": unique}
        )

    assert resp.status_code == 422


async def test_unique_default_is_cards(app: FastAPI) -> None:
    """Missing ``unique=`` behaves identically to ``unique=cards``."""
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, total=1)])
    _override(app, conn)

    async for c in client(app):
        resp_no = await c.get("/cards/search", params={"q": "elf"})

    conn2 = FakeConn(rows=[make_row(SAMPLE_RAW, total=1)])
    _override(app, conn2)
    async for c in client(app):
        resp_cards = await c.get(
            "/cards/search", params={"q": "elf", "unique": "cards"}
        )

    assert resp_no.status_code == resp_cards.status_code == 200
    assert resp_no.json()["data"] == resp_cards.json()["data"]
