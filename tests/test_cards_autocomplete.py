"""``GET /cards/autocomplete`` — quickwin Scryfall-parity endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine


class _Result:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self._rows = rows
        self.last_params: dict[str, Any] | None = None

    async def execute(self, stmt: Any, params: dict[str, Any]) -> _Result:
        self.last_params = params
        return _Result(self._rows)


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


async def test_missing_q_returns_422(app: FastAPI) -> None:
    conn = _FakeConn(rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.get("/cards/autocomplete")
    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"


async def test_oversize_q_returns_422(app: FastAPI) -> None:
    conn = _FakeConn(rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.get("/cards/autocomplete", params={"q": "x" * 101})
    assert resp.status_code == 422


async def test_returns_catalog_shape(app: FastAPI) -> None:
    conn = _FakeConn(rows=[("Lightning Bolt",), ("Lightning Helix",)])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.get("/cards/autocomplete", params={"q": "light"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "object": "catalog",
        "total_values": 2,
        "data": ["Lightning Bolt", "Lightning Helix"],
    }
    # Prefix + q bound identically to what the SQL expects.
    assert conn.last_params is not None
    assert conn.last_params["prefix"] == "light%"
    assert conn.last_params["q"] == "light"
    assert conn.last_params["limit"] == 20


async def test_bug16_ilike_metachars_escaped(app: FastAPI) -> None:
    """Bug.16: `%`/`_`/`\\` in user q are ILIKE metachars — must be escaped.

    Without escaping, `foo_bar` would match `foobar`/`fooXbar` and `%foo`
    would match every row. Fix escapes those three chars before appending
    `%`, so the bound prefix has literal underscores/percents.
    """
    conn = _FakeConn(rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        await client.get("/cards/autocomplete", params={"q": "foo_bar"})
    assert conn.last_params is not None
    # `_` escaped to `\_` in the bound prefix; the trailing `%` is the
    # intended wildcard added by the router. `q` (trigram side) is left
    # verbatim — pg_trgm has no metachars.
    assert conn.last_params["prefix"] == "foo\\_bar%"
    assert conn.last_params["q"] == "foo_bar"


async def test_bug16_percent_metachar_escaped(app: FastAPI) -> None:
    conn = _FakeConn(rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        await client.get("/cards/autocomplete", params={"q": "50%"})
    assert conn.last_params is not None
    assert conn.last_params["prefix"] == "50\\%%"
