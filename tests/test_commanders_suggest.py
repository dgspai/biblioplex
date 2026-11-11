"""D17.2 ``GET /commanders/suggest``.

Commander discovery endpoint. Closes the "I want an Aristocrats commander
in Mardu" gap for MCP agents. Filter `is_commander = true`; optional
`colors` (WUBRG string) enforces `color_identity <@ :ci`; optional
`tags` (comma-list of oracle-tag slugs) ANDs an `EXISTS` per slug against
`oracle_tags`. Order by `edhrec_rank ASC NULLS LAST`, secondary `id ASC`.
Response: Scryfall `List` envelope.

Unknown tag slugs silently return zero rows (agents guess slugs; 422
would be user-hostile — same design choice as /combos/search's card
filter, which shorts to 404 on unresolved names by policy).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from biblioplex.main import create_app
from biblioplex.routers.commanders import get_read_engine

from ._search_helpers import (
    FakeConn,
    FakeEngine,
    client,
    settings,
)


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=settings())


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


def _row(raw: dict[str, Any]) -> dict[str, Any]:
    return {"raw": raw, "predicted_labels": [], "label_scores": {}}


KRENKO = {
    "object": "card",
    "id": "aa000000-0000-0000-0000-000000000001",
    "name": "Krenko, Mob Boss",
    "type_line": "Legendary Creature — Goblin Warrior",
    "color_identity": ["R"],
}
MEREN = {
    "object": "card",
    "id": "aa000000-0000-0000-0000-000000000002",
    "name": "Meren of Clan Nel Toth",
    "type_line": "Legendary Creature — Human Shaman",
    "color_identity": ["B", "G"],
}


async def test_suggest_envelope_shape(app: FastAPI) -> None:
    conn = FakeConn(rows=[_row(KRENKO), _row(MEREN)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/commanders/suggest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    assert body["data"][0]["name"] == "Krenko, Mob Boss"


async def test_suggest_filters_is_commander(app: FastAPI) -> None:
    """SQL narrows on is_commander = true."""
    conn = FakeConn(rows=[_row(KRENKO)])
    _override(app, conn)
    async for c in client(app):
        await c.get("/commanders/suggest")
    sql = str(conn.executed[-1][0]).lower()
    assert "is_commander" in sql


async def test_suggest_colors_binds_ci_subset(app: FastAPI) -> None:
    """`colors=WU` forwards a parsed WUBRG array as `:ci`, subset match."""
    conn = FakeConn(rows=[_row(KRENKO)])
    _override(app, conn)
    async for c in client(app):
        await c.get("/commanders/suggest", params={"colors": "WU"})
    stmt, params = conn.executed[-1]
    sql = str(stmt).lower()
    assert "color_identity" in sql
    assert "<@" in sql
    assert params is not None and "ci" in params
    assert set(params["ci"]) == {"W", "U"}


async def test_suggest_colors_case_insensitive(app: FastAPI) -> None:
    """Lower-case input is normalized to canonical upper-case letters."""
    conn = FakeConn(rows=[_row(KRENKO)])
    _override(app, conn)
    async for c in client(app):
        await c.get("/commanders/suggest", params={"colors": "wub"})
    _, params = conn.executed[-1]
    assert set(params["ci"]) == {"W", "U", "B"}


async def test_suggest_colors_invalid_returns_422(app: FastAPI) -> None:
    """Non-WUBRG chars → 422 bad_request."""
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/commanders/suggest", params={"colors": "wxu"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"


async def test_suggest_tags_ands_exists_per_slug(app: FastAPI) -> None:
    """Each `tags` slug adds an EXISTS against oracle_tags."""
    conn = FakeConn(rows=[_row(MEREN)])
    _override(app, conn)
    async for c in client(app):
        await c.get(
            "/commanders/suggest",
            params={"tags": "sacrifice-outlet,graveyard-recursion"},
        )
    stmt, params = conn.executed[-1]
    sql = str(stmt).lower()
    # Two tag slugs → two EXISTS predicates.
    assert sql.count("exists") >= 2
    assert "oracle_tags" in sql
    assert params is not None
    assert params.get("tag_0") == "sacrifice-outlet"
    assert params.get("tag_1") == "graveyard-recursion"


async def test_suggest_orders_by_edhrec_rank(app: FastAPI) -> None:
    """Rank ASC NULLS LAST is the primary sort; id ASC breaks ties."""
    conn = FakeConn(rows=[_row(KRENKO)])
    _override(app, conn)
    async for c in client(app):
        await c.get("/commanders/suggest")
    sql = str(conn.executed[-1][0]).lower()
    assert "order by" in sql
    assert "edhrec_rank" in sql
    assert "nulls last" in sql
    assert "id" in sql


async def test_suggest_limit_clamped_low(app: FastAPI) -> None:
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/commanders/suggest", params={"limit": 0})
    assert resp.status_code == 422


async def test_suggest_limit_clamped_high(app: FastAPI) -> None:
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/commanders/suggest", params={"limit": 500})
    assert resp.status_code == 422


async def test_suggest_limit_forwarded(app: FastAPI) -> None:
    conn = FakeConn(rows=[_row(KRENKO)])
    _override(app, conn)
    async for c in client(app):
        await c.get("/commanders/suggest", params={"limit": 5})
    _, params = conn.executed[-1]
    assert params["limit"] == 5


async def test_suggest_empty_result_returns_200_empty_data(app: FastAPI) -> None:
    """Zero matches is legitimate (agent guessed a rare tag) — 200 + empty."""
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/commanders/suggest", params={"tags": "unknown-tag"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"] == []
    assert body["has_more"] is False


async def test_suggest_projection_applied(app: FastAPI) -> None:
    conn = FakeConn(rows=[_row(KRENKO)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get(
            "/commanders/suggest",
            params={"projection": "name,color_identity"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["data"][0].keys()) == {"name", "color_identity"}
