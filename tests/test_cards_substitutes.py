"""D12.5 — `GET /cards/:id/substitutes` (§6.8).

Slot-constrained variant of `/similar`. Reuses the same two-step fake
conn pattern as test_cards_similar_d6_1.py — first execute() returns
the target row, second returns the neighbour list. The substitutes
endpoint adds one filter (`:slot = ANY(predicted_labels)`) inside the
candidate CTE; we assert that against the rendered SQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine

SCRY_UUID = "0000ec70-1c4e-4ad5-8d77-d3963fa1fa42"


class _TargetRow:
    def __init__(self, embedding: Any) -> None:
        self.embedding = embedding
        self.oracle_search = ""
        self.cmc = 1.0
        self.color_identity: list[str] = []
        self.type_search = ""
        self.oracle_tags: list[str] = []


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def one_or_none(self) -> Any:
        return None if self._value is None else _TargetRow(self._value)


class _MappingsResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingsResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConn:
    def __init__(
        self,
        target_embedding: Any,
        neighbours: list[dict[str, Any]],
    ) -> None:
        self.target = target_embedding
        self.neighbours = neighbours
        self.executed: list[tuple[Any, Any]] = []
        self._call = 0

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.executed.append((stmt, params))
        self._call += 1
        sql = str(stmt)
        if "FROM cards c WHERE c.id = :id" in sql:
            return _ScalarResult(self.target)
        return _MappingsResult(self.neighbours)


class _Ctx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeEngine:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self) -> _Ctx:
        return _Ctx(self._conn)


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


async def test_valid_slot_returns_slot_only_neighbours(app: FastAPI) -> None:
    """Happy path: slot=ramp returns a list with the bound filter visible in SQL."""
    id1 = "11111111-1111-1111-1111-111111111111"
    id2 = "22222222-2222-2222-2222-222222222222"
    neighbours = [
        {
            "id": id1,
            "raw": {"object": "card", "id": id1, "name": "Cultivate"},
            "distance": 0.12,
            "predicted_labels": ["ramp"],
            "label_scores": {"ramp": 0.95},
        },
        {
            "id": id2,
            "raw": {"object": "card", "id": id2, "name": "Kodama's Reach"},
            "distance": 0.15,
            "predicted_labels": ["ramp"],
            "label_scores": {"ramp": 0.95},
        },
    ]
    conn = _FakeConn(target_embedding=[0.1] * 384, neighbours=neighbours)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get(
            f"/cards/{SCRY_UUID}/substitutes",
            params={"slot": "ramp", "limit": 5},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["total_cards"] == 2
    assert all(c["name"] in {"Cultivate", "Kodama's Reach"} for c in body["data"])
    # Slot binding reached the candidate filter.
    stmts = [str(s) for s, _ in conn.executed]
    assert any(":slot = ANY(predicted_labels)" in s for s in stmts)
    params = conn.executed[1][1]
    assert params["slot"] == "ramp"
    # similarity decoration applied
    assert body["data"][0]["similarity"] == pytest.approx(1.0 - 0.12)


async def test_unknown_slot_returns_422(app: FastAPI) -> None:
    """Slug not in LABELS → 422 unsupported_operator."""
    conn = _FakeConn(target_embedding=[0.1] * 384, neighbours=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get(
            f"/cards/{SCRY_UUID}/substitutes",
            params={"slot": "frobnicate"},
        )

    assert resp.status_code == 422
    body = resp.json()
    assert body["object"] == "error"
    assert body["code"] == "unsupported_operator"
    assert "frobnicate" in body["details"]


async def test_missing_slot_returns_422(app: FastAPI) -> None:
    """Slot is required (FastAPI's own missing-param 422)."""
    conn = _FakeConn(target_embedding=[0.1] * 384, neighbours=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/substitutes")
    assert resp.status_code == 422


async def test_missing_card_returns_404(app: FastAPI) -> None:
    """Target id has no row → 404, same shape as /similar."""
    conn = _FakeConn(target_embedding=None, neighbours=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.get(
            f"/cards/{SCRY_UUID}/substitutes",
            params={"slot": "ramp"},
        )
    assert resp.status_code == 404
    body = resp.json()
    assert body["object"] == "error"
    assert body["code"] == "not_found"


async def test_each_label_slug_accepted(app: FastAPI) -> None:
    """Every LABELS slug is acceptable input."""
    from biblioplex.classify.rules import LABELS

    conn = _FakeConn(target_embedding=[0.1] * 384, neighbours=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        for slug in LABELS:
            resp = await client.get(
                f"/cards/{SCRY_UUID}/substitutes",
                params={"slot": slug, "limit": 1},
            )
            assert resp.status_code == 200, slug
