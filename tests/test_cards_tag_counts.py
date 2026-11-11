"""``POST /cards/tag-counts`` — per-tag deck-card counts in one query."""

from __future__ import annotations

from collections.abc import AsyncIterator
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
        return list(self._rows)


class _FakeConn:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.last_params: dict[str, Any] | None = None

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        self.last_params = params
        return _FakeResult(self._rows)


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


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_tag_counts_aggregates_rows(app: FastAPI) -> None:
    conn = _FakeConn([("ramp", 3), ("goblin synergy", 2)])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/cards/tag-counts",
            json={"names": ["Sol Ring", "Krenko, Mob Boss", "Goblin Matron"]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "tag_counts"
    assert body["counts"] == {"ramp": 3, "goblin synergy": 2}
    # Names are lowercased for the case-insensitive match.
    assert conn.last_params["names"] == ["sol ring", "krenko, mob boss", "goblin matron"]


async def test_tag_counts_empty_names_short_circuits(app: FastAPI) -> None:
    conn = _FakeConn([])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post("/cards/tag-counts", json={"names": []})
    assert resp.status_code == 200
    assert resp.json()["counts"] == {}
    # No DB round-trip when there are no names.
    assert conn.last_params is None
