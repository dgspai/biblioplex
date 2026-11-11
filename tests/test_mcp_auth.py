"""D16.16 — MCP bearer-token auth.

`FastApiMCP.mount_http()` binds unauthenticated; without a gate, any
deployment exposing port 8000 hands anon callers every read tool.

Settings.mcp_auth_token gates a middleware that checks
`Authorization: Bearer <token>` on requests to /mcp*. Empty (default)
= mount stays open (dev). Non-empty = 401 without a matching bearer.
Constant-time compare via hmac.compare_digest — timing-attack surface
exists even on a shared secret.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app


def _settings(mcp_auth_token: str = "") -> Settings:
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
        mcp_auth_token=mcp_auth_token,
    )


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_d16_16_unset_token_allows_anonymous_mcp() -> None:
    """Empty MCP_AUTH_TOKEN (dev default) — no auth check, no 401."""
    app = create_app(settings=_settings(mcp_auth_token=""))
    async for client in _client(app):
        # /mcp responds with its own protocol; we care only about
        # NOT getting 401.
        resp = await client.post("/mcp")
    assert resp.status_code != 401


async def test_d16_16_missing_bearer_returns_401_when_token_set() -> None:
    """MCP_AUTH_TOKEN configured but caller sends no Authorization → 401."""
    app = create_app(settings=_settings(mcp_auth_token="s3cret"))
    async for client in _client(app):
        resp = await client.post("/mcp")
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == "unauthorized"


async def test_d16_16_wrong_bearer_returns_401() -> None:
    """MCP_AUTH_TOKEN configured, caller sends the wrong token → 401."""
    app = create_app(settings=_settings(mcp_auth_token="s3cret"))
    async for client in _client(app):
        resp = await client.post(
            "/mcp", headers={"Authorization": "Bearer wrong"}
        )
    assert resp.status_code == 401


async def test_d16_16_correct_bearer_passes_through() -> None:
    """Correct bearer → auth passes; response is not 401."""
    app = create_app(settings=_settings(mcp_auth_token="s3cret"))
    async for client in _client(app):
        resp = await client.post(
            "/mcp", headers={"Authorization": "Bearer s3cret"}
        )
    assert resp.status_code != 401


async def test_d16_16_non_mcp_paths_never_authed() -> None:
    """Auth middleware only guards /mcp — /health stays open even with a token."""
    app = create_app(settings=_settings(mcp_auth_token="s3cret"))
    async for client in _client(app):
        resp = await client.get("/health/live")
    # /health/live's 200/503 depends on the DB fake; it must NOT be 401.
    assert resp.status_code != 401


async def test_d16_16_non_bearer_scheme_returns_401() -> None:
    """`Authorization: Basic ...` (or any non-Bearer scheme) → 401."""
    app = create_app(settings=_settings(mcp_auth_token="s3cret"))
    async for client in _client(app):
        resp = await client.post(
            "/mcp", headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/mcp", "/mcp/tools/list", "/mcp/foo"])
async def test_d16_16_all_mcp_paths_guarded(path: str) -> None:
    """Every /mcp* path — not just /mcp — falls under the auth gate."""
    app = create_app(settings=_settings(mcp_auth_token="s3cret"))
    async for client in _client(app):
        resp = await client.post(path)
    assert resp.status_code == 401
