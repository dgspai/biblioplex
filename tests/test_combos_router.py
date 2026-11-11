"""Tests for the standalone combos router (`GET /combos/search`, `/combos/:id`)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.combos import get_read_engine


class _FakeResult:
    def __init__(self, rows: list[Any], scalar: int | None = None) -> None:
        self._rows = rows
        self._scalar = scalar

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one(self) -> int:
        return self._scalar if self._scalar is not None else len(self._rows)


class _FakeConn:
    def __init__(
        self,
        rows: list[Any],
        total: int | None = None,
        name_to_oid: dict[str, str] | None = None,
    ) -> None:
        self._rows = rows
        self._total = total
        # Bug.13: the router now resolves `card=` names to oracle_ids up
        # front so the combos WHERE can use the GIN-indexed `= ANY()`
        # form instead of a correlated `EXISTS (SELECT ... FROM cards)`
        # subquery. Fake echoes the resolution set the tests requested.
        self._name_to_oid = name_to_oid or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.calls.append((sql, dict(params or {})))
        if "COUNT(*)" in sql:
            total = self._total if self._total is not None else len(self._rows)
            return _FakeResult([], scalar=total)
        if "oracle_id" in sql and "LOWER(name) = ANY" in sql:
            lowered = params["lowered"] if params else []
            resolved = [
                SimpleNamespace(oracle_id=self._name_to_oid[n], lname=n)
                for n in lowered
                if n in self._name_to_oid
            ]
            return _FakeResult(resolved)
        return _FakeResult(self._rows)


class _FakeConnCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeEngine:
    def __init__(
        self,
        rows: list[Any],
        total: int | None = None,
        name_to_oid: dict[str, str] | None = None,
    ) -> None:
        self._rows = rows
        self._total = total
        self._name_to_oid = name_to_oid or {}
        self.last_conn: _FakeConn | None = None

    def connect(self) -> _FakeConnCtx:
        self.last_conn = _FakeConn(
            self._rows, self._total, name_to_oid=self._name_to_oid
        )
        return _FakeConnCtx(self.last_conn)


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


def _row(**kwargs: Any) -> Any:
    base = {
        "combo_id": "spellbook-1",
        "color_identity": ["U", "R"],
        "prereqs": None,
        "steps": None,
        "results": ["Infinite mana"],
        "card_names": ["Isochron Scepter", "Dramatic Reversal"],
        "card_ids": ["id-a", "id-b"],
        "card_count": 2,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_search_returns_list_envelope(app: FastAPI) -> None:
    engine = _FakeEngine([_row()], total=1)
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["has_more"] is False
    assert len(body["data"]) == 1
    c = body["data"][0]
    assert c["id"] == "spellbook-1"
    assert c["cards"] == ["Isochron Scepter", "Dramatic Reversal"]
    assert c["card_ids"] == ["id-a", "id-b"]
    assert c["color_identity"] == ["U", "R"]
    assert c["card_count"] == 2


@pytest.mark.asyncio
async def test_search_ci_filter_binds_wubrg_array(app: FastAPI) -> None:
    # Bug.23: total=0 now short-circuits to 404, so seed a row to reach the
    # bind-assertion body; this test only cares about params on the count +
    # list stmts, not the response shape.
    engine = _FakeEngine([_row()], total=1)
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search?ci=ur")
    assert r.status_code == 200
    assert engine.last_conn is not None
    # Both count + list stmts get params; check `ci` normalized to WUBRG order.
    for _sql, params in engine.last_conn.calls:
        assert params.get("ci") == ["U", "R"]


@pytest.mark.asyncio
async def test_search_empty_ci_does_not_filter(app: FastAPI) -> None:
    """`?ci=` must not append the `<@` filter — spec says empty = 5c allowed.

    Bug.1: prior code sent `ci: []` into `co.color_identity <@ CAST(:ci AS text[])`
    which only matches colorless combos.
    """
    engine = _FakeEngine([_row()], total=1)
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search?ci=")
    assert r.status_code == 200
    assert engine.last_conn is not None
    for sql, params in engine.last_conn.calls:
        assert "color_identity <@" not in sql
        assert "ci" not in params


@pytest.mark.asyncio
async def test_search_ci_with_digit_returns_400(app: FastAPI) -> None:
    """D10.11: `?ci=W1U` — digits in a CI string are not WUBRG; reject."""
    engine = _FakeEngine([], total=0)
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search?ci=W1U")
    assert r.status_code == 400
    assert r.json()["object"] == "error"


@pytest.mark.asyncio
async def test_search_ci_with_punct_returns_400(app: FastAPI) -> None:
    """D10.11: `?ci=W-U` — punctuation in a CI string is invalid; reject."""
    engine = _FakeEngine([], total=0)
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search?ci=W-U")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_search_ci_invalid_returns_400(app: FastAPI) -> None:
    engine = _FakeEngine([], total=0)
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search?ci=XYZ")
    assert r.status_code == 400
    body = r.json()
    assert body["object"] == "error"


@pytest.mark.asyncio
async def test_search_bad_sort_returns_400(app: FastAPI) -> None:
    engine = _FakeEngine([], total=0)
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search?sort=bogus")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_search_card_filter_resolves_to_oracle_ids(app: FastAPI) -> None:
    """Bug.13: card names resolve to oracle_ids up front so combos WHERE
    can use the GIN-indexed `= ANY(co.oracle_ids)` form directly.

    Prior code used a correlated `EXISTS (SELECT 1 FROM cards cd WHERE
    lower(cd.name) = lower(:card_i) AND cd.oracle_id = ANY(...))` per
    requested card, which the planner had to run inside the seq-scan
    of `combos` — no way to use `combos_oracle_ids_gin`.
    """
    engine = _FakeEngine(
        [_row()],
        total=1,
        name_to_oid={
            "sol ring": "oid-sol",
            "basalt monolith": "oid-basalt",
        },
    )
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search?card=Sol+Ring&card=Basalt+Monolith")
    assert r.status_code == 200
    assert engine.last_conn is not None
    # The combos SQL must bind `oid_*` params (resolved uuids) and no
    # `card_*` params (raw name binds).
    combos_calls = [
        (sql, p) for sql, p in engine.last_conn.calls if "combos" in sql
    ]
    assert combos_calls
    for sql, params in combos_calls:
        # No correlated subquery over the cards table.
        assert "FROM cards cd" not in sql
        assert params.get("oid_0") == "oid-sol"
        assert params.get("oid_1") == "oid-basalt"
        assert "card_0" not in params
        assert "card_1" not in params


@pytest.mark.asyncio
async def test_bug23_search_unresolved_card_returns_404(app: FastAPI) -> None:
    """Bug.23: `card=` name that doesn't resolve → 404, not empty envelope.

    Aligns with `/cards/search`'s §6.1 rule: `total == 0` → 404. Combos
    are keyed on `oracle_id`, so an unresolved name can never match any
    combo — the short-circuit path now surfaces 404 instead of a 200
    with an empty `data` list.
    """
    engine = _FakeEngine([], total=0, name_to_oid={})
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search?card=Nonexistent+Card")
    assert r.status_code == 404
    body = r.json()
    assert body["object"] == "error"
    assert body["code"] == "not_found"


@pytest.mark.asyncio
async def test_bug23_search_zero_matches_returns_404(app: FastAPI) -> None:
    """Bug.23: normal search with `total == 0` → 404 (mirrors /cards/search)."""
    engine = _FakeEngine([], total=0)
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search?q=nothingmatches")
    assert r.status_code == 404
    body = r.json()
    assert body["object"] == "error"
    assert body["code"] == "not_found"


@pytest.mark.asyncio
async def test_search_pagination_has_more(app: FastAPI) -> None:
    # Page 1 of 30 with page_size=25 → has_more true.
    rows = [_row(combo_id=f"c-{i}") for i in range(25)]
    engine = _FakeEngine(rows, total=30)
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search?page_size=25")
    body = r.json()
    assert body["has_more"] is True
    assert body["total"] == 30


@pytest.mark.asyncio
async def test_get_combo_by_id(app: FastAPI) -> None:
    engine = _FakeEngine([_row(combo_id="spellbook-99")])
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/spellbook-99")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "combo"
    assert body["id"] == "spellbook-99"
    assert body["cards"] == ["Isochron Scepter", "Dramatic Reversal"]


@pytest.mark.asyncio
async def test_get_combo_404_when_missing(app: FastAPI) -> None:
    engine = _FakeEngine([])
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/nope")
    assert r.status_code == 404
    body = r.json()
    assert body["object"] == "error"
    assert body["code"] == "not_found"


@pytest.mark.asyncio
async def test_search_ci_c_means_colorless_only(app: FastAPI) -> None:
    """`?ci=C` keeps the `<@` filter with an empty array: only `{}` identities fit."""
    engine = _FakeEngine([_row()], total=1)
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search?ci=c")
    assert r.status_code == 200
    assert engine.last_conn is not None
    for sql, params in engine.last_conn.calls:
        assert "color_identity <@" in sql
        assert params.get("ci") == []


@pytest.mark.asyncio
async def test_search_ci_c_with_colors_is_redundant(app: FastAPI) -> None:
    """`C` next to colors adds nothing: colorless combos already fit within `U`."""
    engine = _FakeEngine([_row()], total=1)
    app.dependency_overrides[get_read_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/combos/search?ci=CU")
    assert r.status_code == 200
    assert engine.last_conn is not None
    for _sql, params in engine.last_conn.calls:
        assert params.get("ci") == ["U"]
