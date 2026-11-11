"""D7.5 — ?projection= wired into /cards/:id/similar.

The four other read endpoints already accept ``?projection=`` (M7 D7.3).
Similar landed in parallel and missed that wiring; D7.5 closes the gap.

Per §7: empty/unset → raw unchanged; bad projection → 422
``invalid_projection``; valid projection → only those paths emitted,
``similarity`` annotation projectable like any other top-level key.
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


class _TargetRow:
    def __init__(
        self,
        embedding: Any,
        oracle_search: str = "",
        cmc: Any = None,
        color_identity: list[str] | None = None,
        type_search: str = "",
        oracle_tags: list[str] | None = None,
    ) -> None:
        self.embedding = embedding
        self.oracle_search = oracle_search
        self.cmc = cmc
        self.color_identity = color_identity
        self.type_search = type_search
        self.oracle_tags = oracle_tags


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def one_or_none(self) -> Any:
        return None if self._value is None else _TargetRow(self._value)


class _MappingsResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingsResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class FakeConn:
    def __init__(self, *, target: Any, neighbours: list[dict[str, Any]]) -> None:
        self.target = target
        self.neighbours = neighbours
        self._call = 0

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self._call += 1
        if self._call == 1:
            return _ScalarResult(self.target)
        return _MappingsResult(self.neighbours)


class _Ctx:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeEngine:
    def __init__(self, conn: FakeConn) -> None:
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


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


SCRY_UUID = "0000ec70-1c4e-4ad5-8d77-d3963fa1fa42"
A_UUID = "1111ec70-1c4e-4ad5-8d77-d3963fa1fa42"


def _neighbour() -> dict[str, Any]:
    return {
        "id": A_UUID,
        "raw": {
            "object": "card",
            "id": A_UUID,
            "name": "Llanowar Elves",
            "type_line": "Creature — Elf Druid",
            "prices": {"usd": "0.10"},
        },
        "distance": 0.10,
        "predicted_labels": [],
        "label_scores": {},
    }


async def test_projection_filters_returned_fields(app: FastAPI) -> None:
    conn = FakeConn(target=[0.1] * 384, neighbours=[_neighbour()])
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(
            f"/cards/{SCRY_UUID}/similar?projection=name,similarity"
        )

    entry = resp.json()["data"][0]
    assert entry == {"name": "Llanowar Elves", "similarity": pytest.approx(0.90)}


async def test_projection_unknown_key_returns_422(app: FastAPI) -> None:
    conn = FakeConn(target=[0.1] * 384, neighbours=[_neighbour()])
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar?projection=bogus")

    assert resp.status_code == 422
    body = resp.json()
    assert body["object"] == "error"
    assert body["code"] == "invalid_projection"


async def test_projection_unset_returns_raw_with_similarity(app: FastAPI) -> None:
    conn = FakeConn(target=[0.1] * 384, neighbours=[_neighbour()])
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    entry = resp.json()["data"][0]
    assert entry["name"] == "Llanowar Elves"
    assert entry["type_line"] == "Creature — Elf Druid"
    assert entry["similarity"] == pytest.approx(0.90)
