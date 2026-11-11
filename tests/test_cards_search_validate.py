"""D17.6 — ``POST /cards/search/validate`` DSL preflight."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app


def _settings(default_filter: str = "") -> Settings:
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
        search_default_filter=default_filter,
    )


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=_settings())


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_d17_6_valid_query_returns_200_with_ast_summary(
    app: FastAPI,
) -> None:
    """Well-formed q → 200 + ok=true + ast_summary + null default_filter."""
    async for client in _client(app):
        resp = await client.post(
            "/cards/search/validate", json={"q": "t:instant c:r"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body["ast_summary"], str)
    assert body["ast_summary"]  # non-empty
    # No default filter configured in the test fixture.
    assert body["applied_default_filter"] is None


async def test_d17_6_default_filter_surfaces_when_set() -> None:
    """`applied_default_filter` echoes settings.search_default_filter."""
    app = create_app(settings=_settings(default_filter="f:commander"))
    async for client in _client(app):
        resp = await client.post(
            "/cards/search/validate", json={"q": "t:instant"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied_default_filter"] == "f:commander"


async def test_d17_6_unsupported_key_returns_422_with_did_you_mean(
    app: FastAPI,
) -> None:
    """`set:` is in _REJECTED_KEYS → 422 + did_you_mean suggests the typo."""
    async for client in _client(app):
        resp = await client.post(
            "/cards/search/validate", json={"q": "st:war"}
        )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "unsupported_operator"
    assert body["message"]
    assert body["details"]
    # `st` is rejected; nearest match should include something.
    assert isinstance(body["did_you_mean"], list)


async def test_d17_6_parse_error_returns_422_with_null_did_you_mean(
    app: FastAPI,
) -> None:
    """Unbalanced parens → parser raises → 422 with did_you_mean=null."""
    async for client in _client(app):
        resp = await client.post(
            "/cards/search/validate", json={"q": "(unbalanced"}
        )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "bad_request"
    # Parse errors don't have a key to typo-correct — null.
    assert body["did_you_mean"] is None


async def test_d17_6_empty_q_returns_422(app: FastAPI) -> None:
    """Empty / whitespace q → 422 bad_request."""
    async for client in _client(app):
        resp = await client.post(
            "/cards/search/validate", json={"q": "   "}
        )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "bad_request"


async def test_d17_6_typo_close_to_supported_key_surfaces(
    app: FastAPI,
) -> None:
    """`typoish` key close to a supported one → did_you_mean lists it."""
    async for client in _client(app):
        resp = await client.post(
            "/cards/search/validate", json={"q": "typ:instant"}
        )
    assert resp.status_code == 422
    body = resp.json()
    dym = body["did_you_mean"]
    assert isinstance(dym, list)
    # `t` / `type` should be within edit-distance range of `typ`.
    # Not asserting exact contents (difflib heuristics), just non-empty.
    assert len(dym) >= 1
