"""Tests for ``GET /cards/:id/tags`` (oracle tags lookup)."""

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


_VALID_UUID = "56ebc372-aabd-4174-a943-c7bf59e5028d"


@pytest.mark.asyncio
async def test_tags_endpoint_returns_list(app: FastAPI) -> None:
    oid = "00000000-0000-0000-0000-000000000000"
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(
        [(oid, "removal"), (oid, "instant-speed-removal")]
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_VALID_UUID}/tags")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["data"] == ["removal", "instant-speed-removal"]


@pytest.mark.asyncio
async def test_tags_endpoint_404_when_card_missing(app: FastAPI) -> None:
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine([])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_VALID_UUID}/tags")
    assert r.status_code == 404
    body = r.json()
    assert body["object"] == "error"
    assert body["code"] == "not_found"


@pytest.mark.asyncio
async def test_tags_endpoint_empty_list_when_card_has_no_tags(app: FastAPI) -> None:
    # LEFT JOIN: card exists, no tags → one row with NULL tag.
    oid = "00000000-0000-0000-0000-000000000000"
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine([(oid, None)])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_VALID_UUID}/tags")
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == []


@pytest.mark.asyncio
async def test_tags_endpoint_422_on_bad_uuid(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/cards/not-a-uuid/tags")
    assert r.status_code == 422
