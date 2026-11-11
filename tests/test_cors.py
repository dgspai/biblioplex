"""Tests for CORS middleware wiring per tech_spec.md §10 (`CORS_ORIGINS`)."""

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
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def one(self) -> Any:
        return self._value


class _FakeConn:
    """Minimal stand-in: ping=1, every reltuples estimate=0."""

    async def execute(
        self, stmt: Any, params: Any = None
    ) -> _FakeResult:
        sql = str(stmt)
        if "pg_class" in sql:
            return _FakeResult(0)
        return _FakeResult(1)

    async def rollback(self) -> None:
        return None


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


def _settings(cors_origins: str = "*") -> Settings:
    return Settings.model_construct(
        database_url="postgresql+psycopg://x/x",
        hf_embed_model="x",
        hf_embed_dim=384,
        hf_embed_device="cpu",
        embed_batch=256,
        bulk_source_type="default_cards",
        web_concurrency=1,
        cors_origins=cors_origins,
        log_level="INFO",
    )


def _app(cors_origins: str = "*") -> FastAPI:
    app = create_app(settings=_settings(cors_origins=cors_origins))
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(_FakeConn())
    return app


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_cors_wildcard_get_allows_any_origin() -> None:
    """`cors_origins="*"` echoes `*` on a simple GET cross-origin request."""
    app = _app("*")
    async for client in _client(app):
        resp = await client.get("/health", headers={"Origin": "https://example.com"})

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"


async def test_cors_wildcard_preflight_returns_200_with_methods() -> None:
    """OPTIONS preflight with `*` returns 200 + `access-control-allow-methods`."""
    app = _app("*")
    async for client in _client(app):
        resp = await client.options(
            "/health",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert resp.status_code == 200
    assert "access-control-allow-methods" in {k.lower() for k in resp.headers}


async def test_cors_csv_allowlist_echoes_allowed_origin() -> None:
    """CSV-split `cors_origins` honors the listed origins (positive case)."""
    app = _app("https://a.com,https://b.com")
    async for client in _client(app):
        resp = await client.get("/health", headers={"Origin": "https://a.com"})

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://a.com"


async def test_cors_csv_allowlist_rejects_unlisted_origin() -> None:
    """CSV allowlist omits the allow-origin header for an unlisted origin."""
    app = _app("https://a.com,https://b.com")
    async for client in _client(app):
        resp = await client.get("/health", headers={"Origin": "https://c.com"})

    # The request itself still succeeds (CORS is browser-enforced); we just
    # assert the server did NOT vouch for c.com.
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
