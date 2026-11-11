"""``POST /cards/resolve-pt`` — Portuguese name → oracle_id endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine

_FOG_ORACLE = "aaaa1111-1111-1111-1111-111111111111"


class _Row:
    def __init__(self, input_name: str, oracle_id: str) -> None:
        self.input_name = input_name
        self.oracle_id = oracle_id


class _Result:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def all(self) -> list[_Row]:
        return list(self._rows)


class _FakeConn:
    """Routes by pass like the real DB — PT-exact hits, the rest empty."""

    def __init__(self, pt_exact: dict[str, str]) -> None:
        self._pt_exact = pt_exact
        self.execute_count = 0

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        self.execute_count += 1
        sql = str(stmt)
        if "FROM pt_names p" in sql and "similarity" not in sql:
            names = params["names"]
            return _Result([_Row(n, self._pt_exact[n]) for n in names if n in self._pt_exact])
        return _Result([])


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


async def test_resolves_pt_name_and_reports_miss(app: FastAPI) -> None:
    conn = _FakeConn(pt_exact={"Banco de Nevoeiro": _FOG_ORACLE})
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.post(
            "/cards/resolve-pt",
            json={"names": ["Banco de Nevoeiro", "Carta Fantasma"]},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "resolve_pt"
    assert body["matches"] == {"Banco de Nevoeiro": _FOG_ORACLE}
    assert body["unresolved"] == ["Carta Fantasma"]


async def test_empty_names_short_circuits_without_db(app: FastAPI) -> None:
    conn = _FakeConn(pt_exact={})
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.post("/cards/resolve-pt", json={"names": []})

    assert resp.status_code == 200
    body = resp.json()
    assert body["matches"] == {}
    assert body["unresolved"] == []
    assert conn.execute_count == 0


async def test_over_cap_returns_422(app: FastAPI) -> None:
    conn = _FakeConn(pt_exact={})
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.post(
            "/cards/resolve-pt",
            json={"names": [f"carta {i}" for i in range(1001)]},
        )

    assert resp.status_code == 422
