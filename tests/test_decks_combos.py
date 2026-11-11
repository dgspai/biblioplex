"""Tests for ``POST /decks/combos`` (D14.3).

Endpoint: resolve a plaintext decklist against ``cards``, then return
every Commander Spellbook combo whose full card set is present in the
deck (Postgres array subset: ``combos.oracle_ids <@ deck_oracle_ids``).

Fakes: a two-shot SQL execute — first the resolve CTE, then the combos
lookup. The endpoint runs both inside one ``engine.connect()`` context so
we route by the presence of ``:ids`` (resolve fold) vs ``combos``
(subset lookup) in the executed statement text.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine
from biblioplex.routers.decks._common import DECKLIST_MAX_LEN


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeConn:
    """Routes execute() calls by SQL substring: resolve fold vs combos.

    ``resolve_rows`` populates the resolve-and-fetch CTE (the analyze
    fold). ``combo_rows`` populates the combos subset query. Any other
    statement returns an empty result.
    """

    def __init__(
        self,
        *,
        resolve_rows: list[Any] | None = None,
        combo_rows: list[Any] | None = None,
    ) -> None:
        self._resolve = resolve_rows or []
        self._combos = combo_rows or []
        self.executed: list[str] = []

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append(sql)
        if "unnest(" in sql and ":ords" in sql:
            return _FakeResult(self._resolve)
        if "combos" in sql:
            return _FakeResult(self._combos)
        return _FakeResult([])


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


def _resolve_row(name: str, oracle_id: str) -> Any:
    """Build a row matching ``_RESOLVE_AND_FETCH_SQL``'s SELECT list."""
    return SimpleNamespace(
        ord=0,
        input_name=name,
        id=UUID("00000000-0000-0000-0000-000000000001"),
        oracle_id=UUID(oracle_id),
        name=name,
        type_line="Artifact",
        layout="normal",
        card_faces=None,
        predicted_labels=[],
        cmc=2,
        mana_cost="{2}",
        color_identity=[],
    )


def _combo_row(
    *,
    combo_id: str,
    prereqs: str | None = None,
    steps: str | None = None,
    results: list[str] | None = None,
    card_names: list[str] | None = None,
) -> Any:
    return SimpleNamespace(
        combo_id=combo_id,
        prereqs=prereqs,
        steps=steps,
        results=results or [],
        card_names=card_names or [],
    )


@pytest.mark.asyncio
async def test_returns_deck_combos_envelope(app: FastAPI) -> None:
    """Happy path: deck contains one combo → response carries it."""
    resolve_rows = [
        _resolve_row("Isochron Scepter", "aaaaaaaa-0000-0000-0000-000000000001"),
        _resolve_row("Dramatic Reversal", "aaaaaaaa-0000-0000-0000-000000000002"),
    ]
    combo_rows = [
        _combo_row(
            combo_id="spellbook-42",
            prereqs="Nonland permanents that tap for >= 3 mana.",
            steps="Imprint Dramatic Reversal, activate Scepter.",
            results=["Infinite mana"],
            card_names=["Isochron Scepter", "Dramatic Reversal"],
        )
    ]
    conn = _FakeConn(resolve_rows=resolve_rows, combo_rows=combo_rows)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/combos",
            json={"decklist": "1 Isochron Scepter\n1 Dramatic Reversal"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "deck_combos"
    assert len(body["combos"]) == 1
    combo = body["combos"][0]
    assert combo["id"] == "spellbook-42"
    assert combo["cards"] == ["Isochron Scepter", "Dramatic Reversal"]
    assert combo["results"] == ["Infinite mana"]
    assert body["unresolved"] == []


@pytest.mark.asyncio
async def test_empty_combos_when_none_hit(app: FastAPI) -> None:
    """Deck resolves but no combo is a subset → empty combos, still 200."""
    resolve_rows = [
        _resolve_row("Sol Ring", "aaaaaaaa-0000-0000-0000-000000000010"),
    ]
    conn = _FakeConn(resolve_rows=resolve_rows, combo_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/combos", json={"decklist": "1 Sol Ring"})
    assert r.status_code == 200
    body = r.json()
    assert body["combos"] == []
    assert body["unresolved"] == []


@pytest.mark.asyncio
async def test_unresolved_surface_separately(app: FastAPI) -> None:
    """Names that resolve to nothing → unresolved list; still 200 with combos."""
    resolve_rows = [
        _resolve_row("Sol Ring", "aaaaaaaa-0000-0000-0000-000000000010"),
    ]
    conn = _FakeConn(resolve_rows=resolve_rows, combo_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/combos",
            json={"decklist": "1 Sol Ring\n1 Nonexistent Card"},
        )
    assert r.status_code == 200
    body = r.json()
    assert "Nonexistent Card" in body["unresolved"]


@pytest.mark.asyncio
async def test_empty_decklist_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/combos", json={"decklist": ""})
    assert r.status_code == 422
    body = r.json()
    assert body["object"] == "error"
    assert body["code"] == "bad_request"


@pytest.mark.asyncio
async def test_decklist_too_long_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/combos",
            json={"decklist": "A\n" * DECKLIST_MAX_LEN},
        )
    assert r.status_code == 422
    body = r.json()
    assert "decklist too long" in body["details"]


@pytest.mark.asyncio
async def test_empty_deck_after_resolve_returns_no_combos(app: FastAPI) -> None:
    """All input names miss → resolve returns 0 rows → skip combos SQL entirely."""
    conn = _FakeConn(resolve_rows=[], combo_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/combos", json={"decklist": "1 Nonexistent Card"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["combos"] == []
    assert "Nonexistent Card" in body["unresolved"]
    # Optimization: no combos SQL should have fired when the deck is empty.
    assert all("combos" not in s for s in conn.executed)


@pytest.mark.asyncio
async def test_multiple_combos_returned_in_id_order(app: FastAPI) -> None:
    """Two combos hit → both returned, ordered by combo id ASC."""
    resolve_rows = [
        _resolve_row("A", "aaaaaaaa-0000-0000-0000-000000000001"),
        _resolve_row("B", "aaaaaaaa-0000-0000-0000-000000000002"),
        _resolve_row("C", "aaaaaaaa-0000-0000-0000-000000000003"),
    ]
    combo_rows = [
        _combo_row(combo_id="a", card_names=["A", "B"]),
        _combo_row(combo_id="b", card_names=["A", "C"]),
    ]
    conn = _FakeConn(resolve_rows=resolve_rows, combo_rows=combo_rows)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/combos", json={"decklist": "1 A\n1 B\n1 C"}
        )
    assert r.status_code == 200
    ids = [c["id"] for c in r.json()["combos"]]
    assert ids == ["a", "b"]
