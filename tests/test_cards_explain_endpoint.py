"""Tests for ``GET /cards/:id/explain`` (D15.3 explainability).

Sourced from `label_scores` + `label_reasons` written by the rules-layer
classifier during refresh; the router just fetches them and hydrates
`why` strings from ``RULE_DESCRIPTIONS``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine


class _Row:
    def __init__(
        self,
        oracle_id: str,
        label_scores: dict[str, float] | None,
        label_reasons: dict[str, list[str]] | None,
    ) -> None:
        self.oracle_id = oracle_id
        self.label_scores = label_scores
        self.label_reasons = label_reasons


class _Result:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def one_or_none(self) -> _Row | None:
        return self._row


class _Conn:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        return _Result(self._row)


class _Ctx:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Engine:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def connect(self) -> _Ctx:
        return _Ctx(_Conn(self._row))


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
_OID = "00000000-0000-0000-0000-000000000000"


@pytest.mark.asyncio
async def test_explain_envelope(app: FastAPI) -> None:
    row = _Row(
        _OID,
        {"ramp": 0.0, "card_advantage": 0.0, "targeted_disrupt": 1.0,
         "mass_disrupt": 0.0, "plan": 0.0},
        {"targeted_disrupt": ["targeted:destroy"]},
    )
    app.dependency_overrides[get_read_engine] = lambda: _Engine(row)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_VALID_UUID}/explain")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "explain"
    labels = body["labels"]
    assert set(labels.keys()) == {
        "ramp", "card_advantage", "targeted_disrupt", "mass_disrupt", "plan",
    }
    td = labels["targeted_disrupt"]
    assert td["score"] == 1.0
    assert td["rules"] == [
        {"id": "targeted:destroy", "why": "Destroys a single target"},
    ]
    assert labels["ramp"]["rules"] == []


@pytest.mark.asyncio
async def test_explain_404_when_missing(app: FastAPI) -> None:
    app.dependency_overrides[get_read_engine] = lambda: _Engine(None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_VALID_UUID}/explain")
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_explain_422_bad_uuid(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/cards/not-a-uuid/explain")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_explain_unknown_rule_falls_back_to_slug(app: FastAPI) -> None:
    row = _Row(
        _OID,
        {"ramp": 0.9, "card_advantage": 0.0, "targeted_disrupt": 0.0,
         "mass_disrupt": 0.0, "plan": 0.0},
        {"ramp": ["ramp:mystery"]},
    )
    app.dependency_overrides[get_read_engine] = lambda: _Engine(row)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_VALID_UUID}/explain")
    assert r.status_code == 200
    assert r.json()["labels"]["ramp"]["rules"] == [
        {"id": "ramp:mystery", "why": "ramp:mystery"},
    ]


@pytest.mark.asyncio
async def test_explain_null_columns_defensive(app: FastAPI) -> None:
    """Card exists but classify never ran → NULL columns → zero + empty rules."""
    row = _Row(_OID, None, None)
    app.dependency_overrides[get_read_engine] = lambda: _Engine(row)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_VALID_UUID}/explain")
    assert r.status_code == 200
    labels = r.json()["labels"]
    assert all(labels[lab]["score"] == 0.0 for lab in labels)
    assert all(labels[lab]["rules"] == [] for lab in labels)
