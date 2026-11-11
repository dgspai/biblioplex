"""Tests for ``GET /cards/:id/printings``: every printing, with owned counts.

The SQL itself is exercised against Postgres by hand; these pin the
response shape the web strip and MCP tool depend on.
"""

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


class _Row:
    """Duck-typed asyncpg-ish row exposing SQLAlchemy `.attr` access."""

    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


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


_CARD_ID = "56ebc372-aabd-4174-a943-c7bf59e5028d"
_ORACLE = "11111111-1111-1111-1111-111111111111"


def _printing(pid: str, quantity: int | None, foil: int | None) -> _Row:
    return _Row(
        oracle_id=_ORACLE, id=pid, set_code="cmm", set_name="Commander Masters",
        collector_number="400", released_at="2023-08-04", rarity="uncommon",
        finishes=["nonfoil", "foil"], image_small="s", image_normal="n",
        usd=1.5, usd_foil=None, quantity=quantity, foil_quantity=foil,
    )


async def _get(app: FastAPI, rows: list[Any]) -> Any:
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(rows)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get(f"/cards/{_CARD_ID}/printings")


@pytest.mark.asyncio
async def test_printings_carry_owned_counts(app: FastAPI) -> None:
    r = await _get(app, [_printing("p-owned", 3, 1), _printing("p-other", None, None)])
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["data"][0] == {
        "id": "p-owned",
        "set": "cmm",
        "set_name": "Commander Masters",
        "collector_number": "400",
        "released_at": "2023-08-04",
        "rarity": "uncommon",
        "finishes": ["nonfoil", "foil"],
        "image_uris": {"small": "s", "normal": "n"},
        "prices": {"usd": "1.5", "usd_foil": None},
        "owned": {"quantity": 3, "foil_quantity": 1},
    }
    assert body["data"][1]["owned"] is None


@pytest.mark.asyncio
async def test_known_card_without_printings_is_empty_list(app: FastAPI) -> None:
    """Before the first refresh fills `printings`, the LEFT JOIN yields one
    all-NULL printing row: 200 with no data, not 404."""
    r = await _get(app, [_Row(oracle_id=_ORACLE, id=None)])
    assert r.status_code == 200
    assert r.json()["data"] == []


@pytest.mark.asyncio
async def test_unknown_card_is_404(app: FastAPI) -> None:
    r = await _get(app, [])
    assert r.status_code == 404
    assert r.json()["object"] == "error"
