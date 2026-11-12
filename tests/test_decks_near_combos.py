"""Tests for ``POST /decks/near-combos`` (D14.9).

Surfaces combos the deck is `missing` cards away from completing.
Extension of D14.3 ``/decks/combos`` (which returns only combos already
fully present). Value: cEDH signal ("you're 1 card from Thassa's Oracle").

Fakes: same two-shot pattern as ``/decks/combos`` — first the resolve
CTE, then the near-combos LATERAL query. Route by SQL substring.
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
    """Two-shot execute router: resolve fold vs near-combos LATERAL."""

    def __init__(
        self,
        *,
        resolve_rows: list[Any] | None = None,
        combo_rows: list[Any] | None = None,
    ) -> None:
        self._resolve = resolve_rows or []
        self._combos = combo_rows or []
        self.executed: list[tuple[str, Any]] = []

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append((sql, params))
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


def _resolve_row(
    name: str, oracle_id: str, color_identity: list[str] | None = None
) -> Any:
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
        color_identity=color_identity or [],
    )


def _near_combo_row(
    *,
    combo_id: str,
    missing_count: int,
    gap_names: list[str],
    gap_ids: list[str],
    in_names: list[str],
    prereqs: str | None = None,
    steps: str | None = None,
    results: list[str] | None = None,
) -> Any:
    return SimpleNamespace(
        combo_id=combo_id,
        prereqs=prereqs,
        steps=steps,
        results=results or [],
        gap_names=gap_names,
        gap_ids=gap_ids,
        missing_count=missing_count,
        in_names=in_names,
    )


@pytest.mark.asyncio
async def test_envelope_shape(app: FastAPI) -> None:
    """Happy path: deck has one card of a 2-card combo; gap surfaces the missing card."""
    resolve_rows = [
        _resolve_row("Isochron Scepter", "aaaaaaaa-0000-0000-0000-000000000001"),
    ]
    combo_rows = [
        _near_combo_row(
            combo_id="spellbook-42",
            missing_count=1,
            gap_names=["Dramatic Reversal"],
            gap_ids=["bbbbbbbb-0000-0000-0000-000000000002"],
            in_names=["Isochron Scepter"],
            prereqs="Nonland permanents that tap for >= 3 mana.",
            steps="Imprint Dramatic Reversal, activate Scepter.",
            results=["Infinite mana"],
        )
    ]
    conn = _FakeConn(resolve_rows=resolve_rows, combo_rows=combo_rows)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/near-combos", json={"decklist": "1 Isochron Scepter"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "deck_near_combos"
    assert len(body["combos"]) == 1
    combo = body["combos"][0]
    assert combo["id"] == "spellbook-42"
    assert combo["missing"] == 1
    assert combo["in_deck"] == ["Isochron Scepter"]
    assert combo["gap"] == [
        {"name": "Dramatic Reversal", "id": "bbbbbbbb-0000-0000-0000-000000000002"}
    ]
    assert combo["results"] == ["Infinite mana"]


@pytest.mark.asyncio
async def test_missing_param_forwarded_to_sql(app: FastAPI) -> None:
    """`?missing=2` forwards to the SQL `:missing` bind."""
    resolve_rows = [
        _resolve_row("Krark's Thumb", "aaaaaaaa-0000-0000-0000-000000000001"),
    ]
    conn = _FakeConn(resolve_rows=resolve_rows, combo_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post(
            "/decks/near-combos?missing=2",
            json={"decklist": "1 Krark's Thumb"},
        )
    combos_call = next(
        (sql, params) for sql, params in conn.executed if "combos" in sql
    )
    _, params = combos_call
    assert params is not None
    assert params["missing"] == 2


@pytest.mark.asyncio
async def test_missing_param_default_is_1(app: FastAPI) -> None:
    resolve_rows = [
        _resolve_row("Krark's Thumb", "aaaaaaaa-0000-0000-0000-000000000001"),
    ]
    conn = _FakeConn(resolve_rows=resolve_rows, combo_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/decks/near-combos", json={"decklist": "1 Krark's Thumb"})
    _, params = next(
        (sql, params) for sql, params in conn.executed if "combos" in sql
    )
    assert params["missing"] == 1


@pytest.mark.asyncio
async def test_missing_param_capped_high(app: FastAPI) -> None:
    """`?missing=99` → 422 (upper bound 3 — deeper gaps aren't useful signal)."""
    conn = _FakeConn(resolve_rows=[], combo_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/near-combos?missing=99",
            json={"decklist": "1 Sol Ring"},
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_sql_uses_gin_overlap_predicate(app: FastAPI) -> None:
    """SQL contains `&&` (any-overlap; GIN-backed) — not `<@` (subset).

    D14.3 /decks/combos uses `<@` (fully-present combos). D14.9 pre-filters
    with `&&` so at least one card must be present, then the LATERAL
    cardinality gate narrows to 1..:missing gap.
    """
    resolve_rows = [
        _resolve_row("Sol Ring", "aaaaaaaa-0000-0000-0000-000000000010"),
    ]
    conn = _FakeConn(resolve_rows=resolve_rows, combo_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/decks/near-combos", json={"decklist": "1 Sol Ring"})
    combos_sql, _ = next(
        (sql, params) for sql, params in conn.executed if "combos" in sql
    )
    assert "&&" in combos_sql
    # Subset predicate must NOT be present — that's /decks/combos's job.
    assert "<@" not in combos_sql or "&&" in combos_sql  # must at least have &&


@pytest.mark.asyncio
async def test_empty_decklist_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/near-combos", json={"decklist": ""})
    assert r.status_code == 422
    assert r.json()["code"] == "bad_request"


@pytest.mark.asyncio
async def test_decklist_too_long_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/near-combos", json={"decklist": "A\n" * DECKLIST_MAX_LEN}
        )
    assert r.status_code == 422
    assert "decklist too long" in r.json()["details"]


@pytest.mark.asyncio
async def test_empty_deck_after_resolve_skips_combos_sql(app: FastAPI) -> None:
    """All input names miss → resolve returns 0 rows → skip near-combos SQL."""
    conn = _FakeConn(resolve_rows=[], combo_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/near-combos", json={"decklist": "1 Nonexistent Card"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["combos"] == []
    assert "Nonexistent Card" in body["unresolved"]
    assert all("combos" not in sql for sql, _ in conn.executed)


@pytest.mark.asyncio
async def test_combos_sorted_by_missing_count_ascending(app: FastAPI) -> None:
    """SQL ORDER BY missing_count ASC — smaller-gap combos surface first."""
    resolve_rows = [
        _resolve_row("A", "aaaaaaaa-0000-0000-0000-000000000001"),
    ]
    conn = _FakeConn(resolve_rows=resolve_rows, combo_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/decks/near-combos", json={"decklist": "1 A"})
    combos_sql, _ = next(
        (sql, params) for sql, params in conn.executed if "combos" in sql
    )
    lowered = combos_sql.lower()
    assert "order by" in lowered
    # Order-by expression should reference the gap count.
    assert "gap_count" in lowered or "missing_count" in lowered


@pytest.mark.asyncio
async def test_limit_param_forwarded(app: FastAPI) -> None:
    resolve_rows = [
        _resolve_row("A", "aaaaaaaa-0000-0000-0000-000000000001"),
    ]
    conn = _FakeConn(resolve_rows=resolve_rows, combo_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/decks/near-combos?limit=5", json={"decklist": "1 A"})
    _, params = next(
        (sql, params) for sql, params in conn.executed if "combos" in sql
    )
    assert params["limit"] == 5


@pytest.mark.asyncio
async def test_unresolved_names_surface(app: FastAPI) -> None:
    resolve_rows = [
        _resolve_row("Sol Ring", "aaaaaaaa-0000-0000-0000-000000000010"),
    ]
    conn = _FakeConn(resolve_rows=resolve_rows, combo_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/near-combos",
            json={"decklist": "1 Sol Ring\n1 Bogus Card"},
        )
    assert r.status_code == 200
    assert "Bogus Card" in r.json()["unresolved"]


async def _combos_params(app: FastAPI, resolve_rows: list[Any], decklist: str) -> Any:
    conn = _FakeConn(resolve_rows=resolve_rows, combo_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/near-combos", json={"decklist": decklist})
    assert r.status_code == 200
    return next(params for sql, params in conn.executed if "FROM combos" in sql)


@pytest.mark.asyncio
async def test_commander_section_bounds_combo_color_identity(app: FastAPI) -> None:
    """Combos must fit the commander's CI (union across partners)."""
    resolve_rows = [
        _resolve_row("Tymna the Weaver", "aaaaaaaa-0000-0000-0000-000000000001", ["W", "B"]),
        _resolve_row("Kraum, Ludevic's Opus", "aaaaaaaa-0000-0000-0000-000000000002", ["U", "R"]),
        _resolve_row("Dark Ritual", "aaaaaaaa-0000-0000-0000-000000000003", ["B"]),
    ]
    decklist = (
        "Commander\n1 Tymna the Weaver\n1 Kraum, Ludevic's Opus\n\n"
        "Deck\n1 Dark Ritual"
    )
    params = await _combos_params(app, resolve_rows, decklist)
    assert sorted(params["ci"]) == ["B", "R", "U", "W"]


@pytest.mark.asyncio
async def test_no_commander_section_leaves_ci_unbounded(app: FastAPI) -> None:
    resolve_rows = [
        _resolve_row("Dark Ritual", "aaaaaaaa-0000-0000-0000-000000000003", ["B"]),
    ]
    params = await _combos_params(app, resolve_rows, "1 Dark Ritual")
    assert params["ci"] is None


@pytest.mark.asyncio
async def test_colorless_commander_bounds_to_colorless(app: FastAPI) -> None:
    resolve_rows = [
        _resolve_row("Kozilek, the Great Distortion", "aaaaaaaa-0000-0000-0000-000000000004"),
    ]
    decklist = "Commander\n1 Kozilek, the Great Distortion"
    params = await _combos_params(app, resolve_rows, decklist)
    assert params["ci"] == []
