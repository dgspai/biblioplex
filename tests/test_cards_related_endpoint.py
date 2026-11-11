"""Tests for ``GET /cards/:id/related`` (D14.8).

Resolves each entry in ``cards.all_parts`` (Scryfall's per-printing
related_card list) back through our name index so consumers get canonical
card ids for our collapsed printings. Names that don't match a row in
``cards`` return ``card: null`` (still surface component + name so agents
can render "combo piece: <name>" even for unhydrated related cards).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        return _FakeResult(self._rows)


class _FakeConnCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeEngine:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def connect(self) -> _FakeConnCtx:
        return _FakeConnCtx(_FakeConn(self._rows))


class _Row:
    """Duck-typed asyncpg-ish row exposing SQLAlchemy `.attr` access."""

    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def _settings() -> Settings:
    return Settings.model_construct(
        database_url="postgresql+psycopg://x/x",
        hf_embed_model="x",
        hf_embed_dim=384,
        hf_embed_device="cpu",
        embed_batch=256,
        bulk_source_type="default_cards",
        web_concurrency=1,
        cors_origins="*",
        log_level="INFO",
    )


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=_settings())


_ROOT_ID = "56ebc372-aabd-4174-a943-c7bf59e5028d"
_ROOT_ORACLE = "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_related_endpoint_returns_resolved_parts(app: FastAPI) -> None:
    """A card with two related parts, both resolvable, surfaces canonical cards.

    The Scryfall printing `scryfall_id` in `all_parts` may point at a
    different printing than our collapsed row; the endpoint must key on
    name and hand back OUR `id`.
    """
    resolved_a = {"id": "aaa", "name": "Elephant", "type_line": "Creature — Elephant"}
    rows = [
        _Row(
            root_oracle_id=_ROOT_ORACLE,
            component="token",
            name="Elephant",
            type_line="Token Creature — Elephant",
            scryfall_id="scry-elephant",
            resolved_id="aaa",
            resolved_raw=resolved_a,
        ),
        _Row(
            root_oracle_id=_ROOT_ORACLE,
            component="combo_piece",
            name="Unknown Card",
            type_line="Creature",
            scryfall_id="scry-unknown",
            resolved_id=None,
            resolved_raw=None,
        ),
    ]
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(rows)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_ROOT_ID}/related")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["data"] == [
        {
            "component": "token",
            "name": "Elephant",
            "type_line": "Token Creature — Elephant",
            "card": resolved_a,
        },
        {
            "component": "combo_piece",
            "name": "Unknown Card",
            "type_line": "Creature",
            "card": None,
        },
    ]


@pytest.mark.asyncio
async def test_related_endpoint_empty_when_card_has_no_parts(app: FastAPI) -> None:
    """LEFT JOIN LATERAL on an empty `all_parts` returns one NULL-filled row.

    Detect via NULL component → empty data list, still 200 (distinct
    from 404 "card unknown").
    """
    rows = [
        _Row(
            root_oracle_id=_ROOT_ORACLE,
            component=None,
            name=None,
            type_line=None,
            scryfall_id=None,
            resolved_id=None,
            resolved_raw=None,
        )
    ]
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(rows)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_ROOT_ID}/related")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["data"] == []


@pytest.mark.asyncio
async def test_related_endpoint_404_when_card_missing(app: FastAPI) -> None:
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine([])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_ROOT_ID}/related")
    assert r.status_code == 404
    body = r.json()
    assert body["object"] == "error"
    assert body["code"] == "not_found"


@pytest.mark.asyncio
async def test_related_endpoint_422_on_bad_uuid(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/cards/not-a-uuid/related")
    assert r.status_code == 422
