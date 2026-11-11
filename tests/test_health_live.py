"""D16.20 — ``GET /health/live`` sub-second liveness probe.

Complements ``/health`` (readiness). K8s liveness probes must not block
on reltuples-side stats or refresh-state locks; ``/health/live`` only
issues ``SELECT 1``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.health import get_read_engine


class _FakeResult:
    def scalar_one(self) -> int:
        return 1


class _FakeConn:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.executed: list[str] = []

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        self.executed.append(str(stmt))
        if self._fail:
            raise RuntimeError("db down")
        return _FakeResult()


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


async def test_health_live_ok(app: FastAPI) -> None:
    """SELECT 1 succeeds → 200 + {"status": "ok"} only."""
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health/live")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    # SELECT 1 is the only query.
    assert len(conn.executed) == 1
    assert "SELECT 1" in conn.executed[0]


async def test_health_live_db_down(app: FastAPI) -> None:
    """DB error → 503 + {"status": "error"}."""
    conn = _FakeConn(fail=True)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health/live")

    assert resp.status_code == 503
    assert resp.json() == {"status": "error"}


async def test_health_live_body_only_has_status(app: FastAPI) -> None:
    """Body carries only `status` — no `db`, `refresh`, `cards_count`, etc.

    K8s liveness probes must be tiny; drift from this shape is a footgun
    that grows the probe payload silently.
    """
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health/live")

    body = resp.json()
    assert set(body.keys()) == {"status"}
