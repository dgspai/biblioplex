"""Tests for ``GET /cards/:id/combos`` (D14.2)."""

from __future__ import annotations

from types import SimpleNamespace
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
_ORACLE_ID = "00000000-0000-0000-0000-000000000000"


def _row(**kwargs: Any) -> Any:
    """Build a SA-row-like namespace for the fake conn."""
    base = {
        "oracle_id": _ORACLE_ID,
        "combo_id": None,
        "prereqs": None,
        "steps": None,
        "results": None,
        "card_names": None,
        "card_ids": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_combos_endpoint_returns_list(app: FastAPI) -> None:
    """One combo hit → data carries id + resolved card names + prose."""
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(
        [
            _row(
                combo_id="spellbook-42",
                prereqs="Nonland permanents that tap for >= 3 mana.",
                steps="Imprint Dramatic Reversal, activate Scepter.",
                results=["Infinite mana"],
                card_names=["Isochron Scepter", "Dramatic Reversal"],
                card_ids=["id-scepter", "id-reversal"],
            )
        ]
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_VALID_UUID}/combos")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 1
    combo = body["data"][0]
    assert combo["id"] == "spellbook-42"
    assert combo["cards"] == ["Isochron Scepter", "Dramatic Reversal"]
    assert combo["card_ids"] == ["id-scepter", "id-reversal"]
    assert combo["prereqs"] == "Nonland permanents that tap for >= 3 mana."
    assert combo["steps"] == "Imprint Dramatic Reversal, activate Scepter."
    assert combo["results"] == ["Infinite mana"]


@pytest.mark.asyncio
async def test_combos_endpoint_multiple_combos(app: FastAPI) -> None:
    """Card in two combos → data has both entries."""
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(
        [
            _row(combo_id="a", results=["Infinite mana"], card_names=["A", "B"]),
            _row(combo_id="b", results=["Infinite draws"], card_names=["A", "C"]),
        ]
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_VALID_UUID}/combos")
    assert r.status_code == 200
    body = r.json()
    assert [c["id"] for c in body["data"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_combos_endpoint_empty_when_card_has_no_combos(app: FastAPI) -> None:
    """LEFT JOIN: card exists, no combos → one row with NULL combo_id."""
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(
        [_row(combo_id=None)]
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_VALID_UUID}/combos")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["data"] == []


@pytest.mark.asyncio
async def test_combos_endpoint_404_when_card_missing(app: FastAPI) -> None:
    """Unknown card → 404 with Scryfall-shaped error."""
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine([])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_VALID_UUID}/combos")
    assert r.status_code == 404
    body = r.json()
    assert body["object"] == "error"
    assert body["code"] == "not_found"


@pytest.mark.asyncio
async def test_combos_endpoint_422_on_bad_uuid(app: FastAPI) -> None:
    """FastAPI path validator rejects a non-UUID with 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/cards/not-a-uuid/combos")
    assert r.status_code == 422


def test_bug26_get_card_combos_uses_single_lateral() -> None:
    """Bug.26: name + id aggregation share ONE LATERAL subquery.

    Prior shape fired two nearly-identical scalar subqueries — one for
    `card_names`, one for `card_ids` — each doing its own
    `unnest(oracle_ids) WITH ORDINALITY` + `JOIN cards`. On a combo
    with 3+ cards that doubles the per-row planner work. Fold them
    into one LATERAL that projects both aggregates.
    """
    import inspect
    import re

    from biblioplex.routers.cards import get_card_combos

    src = inspect.getsource(get_card_combos)
    # `t(oid, ord)` only appears inside the SQL string literal, not in
    # any comment — count once means one shared unnest, twice means the
    # old duplicate-subquery shape.
    unnest_hits = re.findall(r"t\(oid,\s*ord\)", src)
    assert len(unnest_hits) == 1, (
        f"Expected one `t(oid, ord)` occurrence in the SQL, got "
        f"{len(unnest_hits)} — subqueries should share one LATERAL"
    )
    # Both aggregates present.
    assert re.search(r"array_agg\s*\(\s*cd\.name", src)
    assert re.search(r"array_agg\s*\(\s*cd\.id::text", src)


@pytest.mark.asyncio
async def test_combos_endpoint_null_card_names_survive(app: FastAPI) -> None:
    """Defensive: NULL `card_names` (all resolve misses) surfaces as empty list."""
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(
        [_row(combo_id="c1", results=["Infinite mana"], card_names=None, card_ids=None)]
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/cards/{_VALID_UUID}/combos")
    assert r.status_code == 200
    body = r.json()
    assert body["data"][0]["cards"] == []
    assert body["data"][0]["card_ids"] == []
