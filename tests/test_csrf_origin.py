"""CSRF guard: unsafe methods with a foreign `Origin` get 403.

`POST /health` has no handler, so a request that passes the guard answers 405;
a blocked one answers 403. That keeps the tests off the DB.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app


def _app(cors_origins: str = ""):
    return create_app(
        settings=Settings.model_construct(
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
    )


async def _send(
    method: str,
    origin: str | None,
    cors_origins: str = "",
    base: str = "http://test",
    path: str = "/health",
) -> int:
    headers = {"Origin": origin} if origin else {}
    async with AsyncClient(
        transport=ASGITransport(app=_app(cors_origins)), base_url=base
    ) as ac:
        return (await ac.request(method, path, headers=headers)).status_code


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_cross_site_origin_blocked(method: str) -> None:
    assert await _send(method, "https://evil.example") == 403


async def test_cross_site_blocked_body_is_json_error() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as ac:
        resp = await ac.post("/health", headers={"Origin": "https://evil.example"})
    assert resp.json()["code"] == "forbidden_origin"


async def test_mcp_path_blocked_too() -> None:
    assert await _send("POST", "https://evil.example", path="/mcp") == 403


async def test_same_host_passes() -> None:
    assert await _send("POST", "http://test") != 403


async def test_same_host_with_port_passes() -> None:
    got = await _send("POST", "http://127.0.0.1:8080", base="http://127.0.0.1:8080")
    assert got != 403


async def test_same_host_different_port_blocked() -> None:
    got = await _send("POST", "http://127.0.0.1:9999", base="http://127.0.0.1:8080")
    assert got == 403


async def test_listed_origin_passes() -> None:
    assert await _send("POST", "https://a.com", "https://a.com,https://b.com") != 403


async def test_unlisted_origin_blocked_with_allowlist() -> None:
    assert await _send("POST", "https://c.com", "https://a.com") == 403


async def test_no_origin_passes() -> None:
    assert await _send("POST", None) != 403


async def test_get_unaffected() -> None:
    assert await _send("GET", "https://evil.example") != 403


@pytest.mark.parametrize("origin", ["chrome-extension://abc", "moz-extension://abc"])
async def test_extension_origin_rejected(origin: str) -> None:
    # The exemption existed only for the removed sync userscript.
    assert await _send("POST", origin) == 403


async def test_empty_cors_sends_no_allow_origin_header() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as ac:
        resp = await ac.get("/health", headers={"Origin": "https://example.com"})
    assert "access-control-allow-origin" not in resp.headers
