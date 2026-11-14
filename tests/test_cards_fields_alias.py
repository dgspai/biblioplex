"""D15.29 — ``?fields=`` alias for ``?projection=``.

Both query names accept identical CSV syntax on every endpoint that used
to accept ``?projection=``. ``?projection=`` remains supported for
wire-compat. The OpenAPI schema only surfaces ``fields`` (the shorter
name) so MCP tool-cards stay lean.
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
}


# ---- helpers (mirror test_cards_projection.py) ----


class _ScalarRow:
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


# ---- /cards/:id — fields alias parity ----


async def test_card_by_id_fields_alias_matches_projection(app: FastAPI) -> None:
    """`?fields=` produces the same body as `?projection=` on `/cards/:id`."""
    app.dependency_overrides[get_read_engine] = lambda: _ScalarEngine(_ScalarConn(RAW))
    async for c in _client(app):
        with_projection = await c.get(
            f"/cards/{SCRY_UUID}", params={"projection": "name,prices.usd"}
        )
        with_fields = await c.get(
            f"/cards/{SCRY_UUID}", params={"fields": "name,prices.usd"}
        )
    assert with_projection.status_code == 200
    assert with_fields.status_code == 200
    assert with_fields.json() == with_projection.json()


async def test_card_by_id_fields_precedence_when_both_supplied(app: FastAPI) -> None:
    """`?fields=` wins when both names appear (single-value semantics)."""
    app.dependency_overrides[get_read_engine] = lambda: _ScalarEngine(_ScalarConn(RAW))
    async for c in _client(app):
        # `fields=name` should trim to just the name key, ignoring the
        # more permissive `projection=name,type_line` companion.
        resp = await c.get(
            f"/cards/{SCRY_UUID}",
            params={"fields": "name", "projection": "name,type_line"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"name": "Llanowar Elves"}


# ---- /cards/search — fields alias parity ----


async def test_search_fields_alias_matches_projection(app: FastAPI) -> None:
    """`?fields=` narrows each row identically to `?projection=` on /search."""
    raws = [
        {"object": "card", "id": "a", "name": "Llanowar Elves", "type_line": "Creature"},
        {"object": "card", "id": "b", "name": "Elvish Mystic", "type_line": "Creature"},
    ]

    # Two separate FakeEngines so each request's rows come fresh.
    def _override_projection() -> FakeEngine:
        return FakeEngine(FakeConn(rows=[make_row(r, total=2) for r in raws]))

    def _override_fields() -> FakeEngine:
        return FakeEngine(FakeConn(rows=[make_row(r, total=2) for r in raws]))

    async for c in _client(app):
        app.dependency_overrides[get_read_engine] = _override_projection
        resp_projection = await c.get(
            "/cards/search", params={"q": "elf", "projection": "name"}
        )
        app.dependency_overrides[get_read_engine] = _override_fields
        resp_fields = await c.get(
            "/cards/search", params={"q": "elf", "fields": "name"}
        )
    assert resp_projection.status_code == 200
    assert resp_fields.status_code == 200
    assert resp_fields.json()["data"] == resp_projection.json()["data"]
    # Both should contain the shrunk shape.
    assert resp_fields.json()["data"] == [
        {"name": "Llanowar Elves"},
        {"name": "Elvish Mystic"},
    ]


async def test_search_projection_still_supported(app: FastAPI) -> None:
    """`?projection=` remains the wire-compat path (`?fields=` is additive)."""
    raws = [{"object": "card", "id": "a", "name": "Llanowar Elves"}]
    rows = [make_row(r, total=1) for r in raws]
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(FakeConn(rows=rows))
    async for c in _client(app):
        resp = await c.get(
            "/cards/search", params={"q": "elf", "projection": "name"}
        )
    assert resp.status_code == 200
    assert resp.json()["data"] == [{"name": "Llanowar Elves"}]


# ---- OpenAPI schema — `fields` surfaces, `projection` hidden from MCP ----


def _schema() -> dict[str, Any]:
    """Build the app and return its computed OpenAPI schema."""
    stg = Settings.model_construct(
        database_url="postgresql+psycopg://placeholder/placeholder",
        hf_embed_model="sentence-transformers/all-MiniLM-L6-v2",
        hf_embed_dim=384,
        hf_embed_device="cpu",
        embed_batch=256,
        bulk_source_type="default_cards",
        web_concurrency=1,
        cors_origins="*",
        log_level="INFO",
    )
    app = create_app(settings=stg)
    schema: dict[str, Any] = app.openapi()
    return schema


def _param_names(schema: dict[str, Any], path: str, method: str = "get") -> set[str]:
    """Return the set of query/path/header parameter names for an operation."""
    params = schema["paths"][path][method].get("parameters", [])
    return {p["name"] for p in params if isinstance(p, dict)}


def test_openapi_fields_alias_shown_on_cards_id() -> None:
    """`/cards/{card_id}` documents `fields`, hides `projection` from MCP."""
    schema = _schema()
    names = _param_names(schema, "/cards/{card_id}")
    assert "fields" in names, f"expected `fields` in {names}"
    assert "projection" not in names, (
        f"projection must be hidden from OpenAPI so MCP tool-cards show "
        f"only the shorter `fields` alias; got {names}"
    )


def test_openapi_fields_alias_shown_on_cards_search() -> None:
    """`/cards/search` documents `fields`, hides `projection` from MCP."""
    schema = _schema()
    names = _param_names(schema, "/cards/search")
    assert "fields" in names
    assert "projection" not in names
