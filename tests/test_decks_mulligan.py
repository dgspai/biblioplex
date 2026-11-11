"""Router tests for ``POST /decks/mulligan`` (D14.10).

Pipeline: parse_decklist → resolve_and_fetch_deck_rows → build pool of
`(name, cmc, is_land)` tuples via the shared `_is_land` helper →
`sample_hands` → return stats envelope.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine
from biblioplex.routers.decks._common import DECKLIST_MAX_LEN


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeConn:
    def __init__(self, resolve_rows: list[Any] | None = None) -> None:
        self._resolve = resolve_rows or []
        self.executed: list[str] = []

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append(sql)
        if "unnest(" in sql and ":ords" in sql:
            return _FakeResult(self._resolve)
        return _FakeResult([])


class _FakeConnCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeEngine:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self) -> _FakeConnCtx:
        return _FakeConnCtx(self._conn)


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


def _resolve_row(
    name: str,
    oracle_id: str,
    *,
    cmc: int = 2,
    type_line: str = "Artifact",
    layout: str = "normal",
) -> Any:
    return SimpleNamespace(
        ord=0,
        input_name=name,
        id=UUID("00000000-0000-0000-0000-000000000001"),
        oracle_id=UUID(oracle_id),
        name=name,
        type_line=type_line,
        layout=layout,
        card_faces=None,
        predicted_labels=[],
        cmc=cmc,
        mana_cost="{2}",
        color_identity=[],
    )


def _land_row(name: str, oracle_id: str) -> Any:
    return _resolve_row(name, oracle_id, cmc=0, type_line="Basic Land — Forest")


@pytest.mark.asyncio
async def test_envelope_shape(app: FastAPI) -> None:
    """Happy path returns deck_mulligan envelope with stats + pool_size."""
    rows = [
        _land_row("Forest", "aaaaaaaa-0000-0000-0000-000000000001"),
        _resolve_row(
            "Bolt", "aaaaaaaa-0000-0000-0000-000000000002", cmc=1,
            type_line="Instant",
        ),
    ]
    conn = _FakeConn(resolve_rows=rows)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/mulligan",
            json={
                "decklist": "38 Forest\n61 Bolt",
                "hands": 200,
                "hand_size": 7,
                "seed": 42,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "deck_mulligan"
    assert body["hands"] == 200
    assert body["hand_size"] == 7
    assert body["seed"] == 42
    assert body["pool_size"] == 99
    assert "stats" in body
    stats = body["stats"]
    assert 0.0 <= stats["keep_rate"] <= 1.0
    assert 0 <= stats["land_min"] <= stats["land_max"] <= 7


@pytest.mark.asyncio
async def test_seed_reproducibility(app: FastAPI) -> None:
    """Same seed → identical stats block."""
    rows = [
        _land_row("Forest", "aaaaaaaa-0000-0000-0000-000000000001"),
        _resolve_row(
            "Bolt", "aaaaaaaa-0000-0000-0000-000000000002", cmc=1,
            type_line="Instant",
        ),
    ]
    conn = _FakeConn(resolve_rows=rows)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r1 = await ac.post(
            "/decks/mulligan",
            json={
                "decklist": "38 Forest\n61 Bolt",
                "hands": 100,
                "seed": 7,
            },
        )
        r2 = await ac.post(
            "/decks/mulligan",
            json={
                "decklist": "38 Forest\n61 Bolt",
                "hands": 100,
                "seed": 7,
            },
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["stats"] == r2.json()["stats"]


@pytest.mark.asyncio
async def test_empty_decklist_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/mulligan", json={"decklist": ""})
    assert r.status_code == 422
    assert r.json()["code"] == "bad_request"


@pytest.mark.asyncio
async def test_decklist_too_long_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/mulligan", json={"decklist": "A\n" * DECKLIST_MAX_LEN}
        )
    assert r.status_code == 422
    assert "decklist too long" in r.json()["details"]


@pytest.mark.asyncio
async def test_all_lands_keep_rate_zero_because_all_are_land(app: FastAPI) -> None:
    """100 Forest → every 7-card hand is all lands → outside [2,5] → keep=0."""
    rows = [_land_row("Forest", "aaaaaaaa-0000-0000-0000-000000000001")]
    conn = _FakeConn(resolve_rows=rows)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/mulligan",
            json={"decklist": "100 Forest", "hands": 100, "seed": 0},
        )
    body = r.json()
    assert body["stats"]["keep_rate"] == 0.0
    assert body["stats"]["land_min"] == 7


@pytest.mark.asyncio
async def test_unresolved_names_surface(app: FastAPI) -> None:
    rows = [_land_row("Forest", "aaaaaaaa-0000-0000-0000-000000000001")]
    conn = _FakeConn(resolve_rows=rows)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/mulligan",
            json={"decklist": "38 Forest\n1 Bogus Card"},
        )
    assert r.status_code == 200
    assert "Bogus Card" in r.json()["unresolved"]


@pytest.mark.asyncio
async def test_hands_out_of_range_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r_low = await ac.post(
            "/decks/mulligan", json={"decklist": "1 X", "hands": 0}
        )
        r_high = await ac.post(
            "/decks/mulligan", json={"decklist": "1 X", "hands": 999999}
        )
    assert r_low.status_code == 422
    assert r_high.status_code == 422


@pytest.mark.asyncio
async def test_hand_size_out_of_range_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/mulligan", json={"decklist": "1 X", "hand_size": 99}
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_pool_size_reflects_qty_expansion(app: FastAPI) -> None:
    """4 Forest → pool has 4 copies of the tuple, pool_size=4."""
    rows = [_land_row("Forest", "aaaaaaaa-0000-0000-0000-000000000001")]
    conn = _FakeConn(resolve_rows=rows)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/mulligan", json={"decklist": "4 Forest", "hands": 1, "hand_size": 4}
        )
    body = r.json()
    assert body["pool_size"] == 4
