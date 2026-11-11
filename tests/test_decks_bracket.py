"""Tests for ``POST /decks/bracket`` (D14.4).

Router-side integration on top of the pure-fn
:mod:`biblioplex.decklist.bracket` module (unit-tested separately in
``tests/test_bracket.py``). Verifies request validation, the two-shot
resolve → combos-count SQL sequence, and the payload envelope.
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

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Routes execute() by SQL substring: resolve fold vs combos count vs tag counts.

    D15.25: the router now fires ONE extra aggregate over `oracle_tags`
    for the 4 tag-based bracket signals. Fake exposes `tag_signal_counts`
    so callers can seed a "the deck's oracle_ids have this many hits
    per tag slug" world without having to seed the tags table.
    """

    def __init__(
        self,
        *,
        resolve_rows: list[Any] | None = None,
        combo_count: int = 0,
        tag_signal_counts: dict[str, int] | None = None,
    ) -> None:
        self._resolve = resolve_rows or []
        self._count = combo_count
        counts = tag_signal_counts or {}
        self._tag_row = SimpleNamespace(
            fast_mana=int(counts.get("fast_mana", 0)),
            mld=int(counts.get("mld", 0)),
            tutors=int(counts.get("tutors", 0)),
            extra_turns=int(counts.get("extra_turns", 0)),
        )

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        if "unnest(" in sql and ":ords" in sql:
            return _FakeResult(self._resolve)
        if "oracle_tags" in sql and "FILTER" in sql:
            return _FakeResult([self._tag_row])
        if "count(*)" in sql and "combos" in sql:
            return _FakeResult([SimpleNamespace(n=self._count)])
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


@pytest.mark.asyncio
async def test_returns_bracket_envelope(app: FastAPI) -> None:
    """Bracket-1 deck (no signals, no combos) → bracket=1."""
    conn = _FakeConn(
        resolve_rows=[
            _resolve_row("Forest", "aaaaaaaa-0000-0000-0000-000000000001"),
        ],
        combo_count=0,
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/bracket", json={"decklist": "1 Forest"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "deck_bracket"
    assert body["bracket"] == 1
    assert "signals" in body
    assert body["signals"]["combos"] == 0
    assert body["unresolved"] == []


@pytest.mark.asyncio
async def test_combo_present_pushes_to_bracket_4(app: FastAPI) -> None:
    """One ingested combo present → bracket=4 floor."""
    conn = _FakeConn(
        resolve_rows=[
            _resolve_row("Sol Ring", "aaaaaaaa-0000-0000-0000-000000000002"),
        ],
        combo_count=1,
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/bracket", json={"decklist": "1 Sol Ring"})
    assert r.status_code == 200
    body = r.json()
    assert body["bracket"] == 4
    assert body["signals"]["combos"] == 1


@pytest.mark.asyncio
async def test_cedh_signals_push_to_bracket_5(app: FastAPI) -> None:
    """Combo + heavy fast mana → bracket=5.

    D15.25: fast_mana comes from oracle_tags now — seed the tag count
    directly rather than relying on Sol Ring/Mana Crypt name matching.
    """
    conn = _FakeConn(
        resolve_rows=[
            _resolve_row("Sol Ring", "aaaaaaaa-0000-0000-0000-000000000001"),
            _resolve_row("Mana Crypt", "aaaaaaaa-0000-0000-0000-000000000002"),
            _resolve_row("Chrome Mox", "aaaaaaaa-0000-0000-0000-000000000003"),
        ],
        combo_count=1,
        tag_signal_counts={"fast_mana": 3},
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/bracket",
            json={"decklist": "1 Sol Ring\n1 Mana Crypt\n1 Chrome Mox"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["bracket"] == 5


@pytest.mark.asyncio
async def test_game_changer_names_surfaced(app: FastAPI) -> None:
    """Response carries which game-changer cards matched."""
    conn = _FakeConn(
        resolve_rows=[
            _resolve_row("Rhystic Study", "aaaaaaaa-0000-0000-0000-000000000001"),
        ],
        combo_count=0,
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/bracket", json={"decklist": "1 Rhystic Study"}
        )
    assert r.status_code == 200
    body = r.json()
    # Bug.8: response surfaces proper-cased names, not lowercased.
    assert "Rhystic Study" in body["game_changers"]


@pytest.mark.asyncio
async def test_empty_decklist_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/bracket", json={"decklist": ""})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_decklist_too_long_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/bracket", json={"decklist": "A\n" * DECKLIST_MAX_LEN}
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_router_appends_combos_disabled_note_when_url_empty() -> None:
    """D15.18: COMBOS_SOURCE_URL="" (explicitly disabled) → disabled note on ≤3."""
    conn = _FakeConn(
        resolve_rows=[
            _resolve_row("Sol Ring", "aaaaaaaa-0000-0000-0000-000000000001"),
        ],
        combo_count=0,
    )
    app = create_app(
        settings=_settings().model_copy(update={"combos_source_url": ""})
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/bracket", json={"decklist": "1 Sol Ring"})
    assert r.status_code == 200
    notes = r.json()["notes"]
    assert any("combos signal disabled" in n for n in notes)


@pytest.mark.asyncio
async def test_router_omits_combos_disabled_note_when_url_set() -> None:
    """D15.18: COMBOS_SOURCE_URL non-empty → no disabled note appended."""
    conn = _FakeConn(
        resolve_rows=[
            _resolve_row("Sol Ring", "aaaaaaaa-0000-0000-0000-000000000001"),
        ],
        combo_count=0,
    )
    settings = _settings()
    settings.combos_source_url = "https://spellbook.example.com/api"
    app = create_app(settings=settings)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/bracket", json={"decklist": "1 Sol Ring"})
    assert r.status_code == 200
    notes = r.json()["notes"]
    assert not any("combos signal disabled" in n for n in notes)


@pytest.mark.asyncio
async def test_d15_25_router_fires_tag_counts_sql_with_slugs(app: FastAPI) -> None:
    """D15.25: router queries oracle_tags with the SIGNAL_TAG_SLUGS map."""
    executed: list[tuple[str, dict[str, Any]]] = []

    class _CapturingConn(_FakeConn):
        async def execute(self, stmt: Any, params: Any = None) -> Any:
            executed.append((str(stmt), dict(params or {})))
            return await super().execute(stmt, params)

    conn = _CapturingConn(
        resolve_rows=[
            _resolve_row("Demonic Tutor", "aaaaaaaa-0000-0000-0000-000000000001"),
        ],
        combo_count=0,
        tag_signal_counts={"tutors": 1},
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/bracket", json={"decklist": "1 Demonic Tutor"}
        )
    assert r.status_code == 200
    body = r.json()
    # Tag-based signal survived the pipeline.
    assert body["signals"]["tutors"] == 1
    # Confirm the tag SQL fired with the expected slugs.
    tag_stmt = next(
        (sql, params) for sql, params in executed
        if "oracle_tags" in sql and "FILTER" in sql
    )
    sql, params = tag_stmt
    assert params["slug_tutors"] == "tutor"
    assert params["slug_fast_mana"] == "fast-mana"
    assert params["slug_mld"] == "mass-land-destruction"
    assert params["slug_extra_turns"] == "extra-turn"


@pytest.mark.asyncio
async def test_d15_25_empty_deck_skips_tag_counts_sql(app: FastAPI) -> None:
    """When the resolve step returns 0 rows, skip both combos + tag SQL."""
    executed: list[str] = []

    class _CapturingConn(_FakeConn):
        async def execute(self, stmt: Any, params: Any = None) -> Any:
            executed.append(str(stmt))
            return await super().execute(stmt, params)

    conn = _CapturingConn(resolve_rows=[], combo_count=0)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/bracket", json={"decklist": "1 Nonexistent Card"}
        )
    assert r.status_code == 200
    # Tag SQL and combos SQL should NOT have fired — empty deck short-circuits.
    assert not any("oracle_tags" in sql for sql in executed)
    assert not any("count(*)" in sql and "combos" in sql for sql in executed)


@pytest.mark.asyncio
async def test_unresolved_surface_in_response(app: FastAPI) -> None:
    """Names that miss both resolve passes surface in `unresolved`."""
    conn = _FakeConn(
        resolve_rows=[
            _resolve_row("Sol Ring", "aaaaaaaa-0000-0000-0000-000000000001"),
        ],
        combo_count=0,
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/bracket",
            json={"decklist": "1 Sol Ring\n1 Nonexistent Card"},
        )
    assert r.status_code == 200
    body = r.json()
    assert "Nonexistent Card" in body["unresolved"]
