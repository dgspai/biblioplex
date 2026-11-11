"""D17.5 — ``POST /cards/collection`` Scryfall-parity bulk lookup."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine


class _FakeRow:
    def __init__(
        self, id_: str, oracle_id: str, name: str, raw: dict[str, Any]
    ) -> None:
        self.id = id_
        self.oracle_id = oracle_id
        self.name = name
        self.raw = raw
        self.predicted_labels: list[str] = []
        self.label_scores: dict[str, float] = {}


class _FakeResult:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def all(self) -> list[_FakeRow]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows
        self.execute_count = 0
        self.last_params: dict[str, Any] | None = None

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        self.execute_count += 1
        self.last_params = params
        return _FakeResult(self._rows)


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


_SOL_ID = "11111111-1111-1111-1111-111111111111"
_SOL_ORACLE = "aaaa1111-1111-1111-1111-111111111111"
_BOLT_ID = "22222222-2222-2222-2222-222222222222"
_BOLT_ORACLE = "aaaa2222-2222-2222-2222-222222222222"


def _row(id_: str, oracle: str, name: str) -> _FakeRow:
    return _FakeRow(id_, oracle, name, {"object": "card", "id": id_, "name": name})


async def test_d17_5_three_identifier_types_resolve(app: FastAPI) -> None:
    """Body carrying id / oracle_id / name — each identifier type resolves."""
    conn = _FakeConn(
        rows=[
            _row(_SOL_ID, _SOL_ORACLE, "Sol Ring"),
            _row(_BOLT_ID, _BOLT_ORACLE, "Lightning Bolt"),
        ]
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.post(
            "/cards/collection",
            json={
                "identifiers": [
                    {"id": _SOL_ID},
                    {"oracle_id": _BOLT_ORACLE},
                    {"name": "Sol Ring"},
                ]
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 3
    # Order preserved.
    assert body["data"][0]["id"] == _SOL_ID
    assert body["data"][1]["id"] == _BOLT_ID
    assert body["data"][2]["id"] == _SOL_ID
    assert body["not_found"] == []


async def test_d17_5_unknown_ids_surface_in_not_found(app: FastAPI) -> None:
    """Identifiers with no match come back under `not_found` unchanged."""
    conn = _FakeConn(rows=[_row(_SOL_ID, _SOL_ORACLE, "Sol Ring")])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    unknown_id = "99999999-9999-9999-9999-999999999999"
    async for client in _client(app):
        resp = await client.post(
            "/cards/collection",
            json={
                "identifiers": [
                    {"id": _SOL_ID},
                    {"id": unknown_id},
                    {"name": "no such card"},
                ]
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["id"] == _SOL_ID
    # not_found preserves the exact identifier shape the caller sent.
    assert {"id": unknown_id} in body["not_found"]
    assert {"name": "no such card"} in body["not_found"]


async def test_d17_5_over_cap_returns_422(app: FastAPI) -> None:
    """Body with > 75 identifiers → 422 (Scryfall's own cap)."""
    conn = _FakeConn(rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.post(
            "/cards/collection",
            json={"identifiers": [{"name": f"card {i}"} for i in range(76)]},
        )

    assert resp.status_code == 422


async def test_d17_5_empty_identifiers_returns_empty_list(app: FastAPI) -> None:
    """Zero identifiers → 200 + empty data + no DB touch."""
    conn = _FakeConn(rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.post(
            "/cards/collection", json={"identifiers": []}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"] == []
    assert body["not_found"] == []
    assert conn.execute_count == 0


async def test_d17_5_name_match_case_insensitive(app: FastAPI) -> None:
    """Name resolution is case-insensitive (LOWER = LOWER)."""
    conn = _FakeConn(rows=[_row(_SOL_ID, _SOL_ORACLE, "Sol Ring")])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.post(
            "/cards/collection", json={"identifiers": [{"name": "sol ring"}]}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["name"] == "Sol Ring"
    assert body["not_found"] == []


async def test_d17_5_identifier_with_no_fields_flagged_as_not_found(
    app: FastAPI,
) -> None:
    """Identifier `{}` (no id/oracle_id/name) is undecidable — surface in not_found."""
    conn = _FakeConn(rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.post(
            "/cards/collection", json={"identifiers": [{}]}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["not_found"] == [{}]
