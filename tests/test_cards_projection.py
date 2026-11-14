"""Tests for ``?projection=`` wiring on the three existing endpoints (D7.3, §7).

Covers:

* ``GET /cards/:id?projection=`` — single card, `raw` projected through.
* ``GET /cards/named?exact=&projection=`` — single card.
* ``GET /cards/named?fuzzy=&projection=`` — single card.
* ``GET /cards/search?q=&projection=`` — every row in ``data`` projected.

Empty / unset ``projection`` returns the raw payload unchanged
(spec-mandated wire compatibility, §7 final line). The actual parse /
projector behavior is covered by the dedicated D7.1/D7.2 test files —
these tests just confirm the router hooks them up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine

from ._search_helpers import (
    FakeConn,
    FakeEngine,
    make_row,
    settings,
)

SCRY_UUID = "0000ec70-1c4e-4ad5-8d77-d3963fa1fa42"

RAW: dict[str, Any] = {
    "object": "card",
    "id": SCRY_UUID,
    "name": "Llanowar Elves",
    "type_line": "Creature — Elf Druid",
    "oracle_text": "{T}: Add {G}.",
    "prices": {"usd": "0.10", "eur": "0.05"},
    "card_faces": [
        {"name": "Front", "oracle_text": "F"},
    ],
}


# ---- helpers ----


class _ScalarRow:
    """Row-shaped fake exposing the Step-5 column attrs alongside ``raw``."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.oracle_id = "00000000-0000-0000-0000-000000000000"
        self.predicted_labels: list[str] = []
        self.label_scores: dict[str, float] = {}
        self.label_reasons: dict[str, Any] | None = None
        self.is_commander = False


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def one_or_none(self) -> Any:
        if self._value is None:
            return None
        return _ScalarRow(self._value)


class _ScalarConn:
    """Single-row connection for ``/cards/:id`` and ``/cards/named``."""

    def __init__(self, row: Any) -> None:
        self._row = row

    async def execute(self, stmt: Any, params: Any = None) -> _ScalarResult:
        return _ScalarResult(self._row)


class _ScalarCtx:
    def __init__(self, conn: _ScalarConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _ScalarConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _ScalarEngine:
    def __init__(self, conn: _ScalarConn) -> None:
        self._conn = conn

    def connect(self) -> _ScalarCtx:
        return _ScalarCtx(self._conn)


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=settings())


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---- /cards/:id ----


async def test_card_by_id_projection_subset(app: FastAPI) -> None:
    app.dependency_overrides[get_read_engine] = lambda: _ScalarEngine(_ScalarConn(RAW))
    async for c in _client(app):
        resp = await c.get(f"/cards/{SCRY_UUID}", params={"projection": "name,prices.usd"})
    assert resp.status_code == 200
    assert resp.json() == {"name": "Llanowar Elves", "prices": {"usd": "0.10"}}


async def test_card_by_id_no_projection_returns_raw(app: FastAPI) -> None:
    """Empty / unset projection → raw + Step-5 fields (§7)."""
    app.dependency_overrides[get_read_engine] = lambda: _ScalarEngine(_ScalarConn(RAW))
    async for c in _client(app):
        resp = await c.get(f"/cards/{SCRY_UUID}")
    assert resp.json() == {**RAW, "predicted_labels": [], "label_scores": {}, "labels_overridden": False, "is_commander": False}


async def test_card_by_id_empty_projection_returns_raw(app: FastAPI) -> None:
    """``projection=`` empty string → raw + Step-5 fields (treated as absent)."""
    app.dependency_overrides[get_read_engine] = lambda: _ScalarEngine(_ScalarConn(RAW))
    async for c in _client(app):
        resp = await c.get(f"/cards/{SCRY_UUID}", params={"projection": ""})
    assert resp.json() == {**RAW, "predicted_labels": [], "label_scores": {}, "labels_overridden": False, "is_commander": False}


async def test_card_by_id_missing_field_emits_null(app: FastAPI) -> None:
    """Path syntactically valid but value absent → null leaf."""
    app.dependency_overrides[get_read_engine] = lambda: _ScalarEngine(_ScalarConn(RAW))
    async for c in _client(app):
        resp = await c.get(
            f"/cards/{SCRY_UUID}", params={"projection": "loyalty"}
        )
    assert resp.json() == {"loyalty": None}


# ---- /cards/named ----


async def test_named_exact_projection_subset(app: FastAPI) -> None:
    app.dependency_overrides[get_read_engine] = lambda: _ScalarEngine(_ScalarConn(RAW))
    async for c in _client(app):
        resp = await c.get(
            "/cards/named",
            params={"exact": "Llanowar Elves", "projection": "name"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"name": "Llanowar Elves"}


async def test_named_fuzzy_projection_subset(app: FastAPI) -> None:
    app.dependency_overrides[get_read_engine] = lambda: _ScalarEngine(_ScalarConn(RAW))
    async for c in _client(app):
        resp = await c.get(
            "/cards/named",
            params={"fuzzy": "llanowar elf", "projection": "type_line"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"type_line": "Creature — Elf Druid"}


# ---- /cards/search ----


async def test_search_projection_applies_to_each_row(app: FastAPI) -> None:
    raws = [
        {
            "object": "card",
            "id": "a",
            "name": "Llanowar Elves",
            "type_line": "Creature",
        },
        {"object": "card", "id": "b", "name": "Elvish Mystic", "type_line": "Creature"},
    ]
    rows = [make_row(r, total=2) for r in raws]
    conn = FakeConn(rows=rows)
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)

    async for c in _client(app):
        resp = await c.get(
            "/cards/search", params={"q": "elf", "projection": "name"}
        )
    body = resp.json()
    assert resp.status_code == 200
    assert body["object"] == "list"
    assert body["data"] == [{"name": "Llanowar Elves"}, {"name": "Elvish Mystic"}]


async def test_search_no_projection_returns_raw_data(app: FastAPI) -> None:
    raws = [{"object": "card", "id": "a", "name": "Llanowar Elves"}]
    rows = [make_row(r, total=1) for r in raws]
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(FakeConn(rows=rows))
    async for c in _client(app):
        resp = await c.get("/cards/search", params={"q": "elf"})
    assert resp.json()["data"] == [
        {**r, "predicted_labels": [], "label_scores": {}} for r in raws
    ]
