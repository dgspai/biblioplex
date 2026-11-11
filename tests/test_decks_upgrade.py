"""Tests for ``POST /decks/upgrade`` (D14.5).

Endpoint proposes swap candidates for each card in the deck, ranked by
``edhrec_rank`` improvement — a lower rank means "more played" in the
EDHRec corpus, so a candidate whose rank is below the current card's is
by definition a more-played substitute for the same predicted-label
slot. Optional commander color-identity filter mirrors /decks/suggest
(``color_identity <@ :ci``). Optional budget filter drops candidates
whose ``price_usd > :budget``.

Fakes: two-shot SQL — the resolve fold, then the upgrade lookup. Route
by SQL substring: `unnest(:ords)` for resolve, `LATERAL` (upgrade CTE)
for the candidate join.
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


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Routes execute() by SQL substring: resolve fold vs upgrade / commander."""

    def __init__(
        self,
        *,
        resolve_rows: list[Any] | None = None,
        upgrade_rows: list[Any] | None = None,
        commander_row: Any = None,
    ) -> None:
        self._resolve = resolve_rows or []
        self._upgrades = upgrade_rows or []
        self._commander = commander_row
        self.executed_params: list[Any] = []
        self.executed_sql: list[str] = []

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed_params.append(params)
        self.executed_sql.append(sql)
        if "unnest(" in sql and ":ords" in sql:
            return _FakeResult(self._resolve)
        if "LATERAL" in sql:
            return _FakeResult(self._upgrades)
        if "color_identity" in sql and "LOWER(name)" in sql:
            return _FakeResult(
                [self._commander] if self._commander is not None else []
            )
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
        type_line="Sorcery",
        layout="normal",
        card_faces=None,
        predicted_labels=["ramp"],
        cmc=3,
        mana_cost="{2}{G}",
        color_identity=["G"],
    )


def _upgrade_row(
    *,
    out_id: str = "00000000-0000-0000-0000-00000000000a",
    out_name: str = "Cultivate",
    out_rank: int = 200,
    in_id: str = "00000000-0000-0000-0000-00000000000b",
    in_name: str = "Farseek",
    in_rank: int = 50,
    matched_label: str = "ramp",
    predicted_labels: list[str] | None = None,
    label_scores: dict[str, float] | None = None,
) -> Any:
    """Row shape matching the upgrade endpoint's SELECT list."""
    return SimpleNamespace(
        out_id=UUID(out_id),
        out_name=out_name,
        out_rank=out_rank,
        in_id=UUID(in_id),
        in_name=in_name,
        in_rank=in_rank,
        in_raw={"name": in_name, "id": in_id, "type_line": "Sorcery"},
        matched_label=matched_label,
        score=out_rank - in_rank,
        predicted_labels=predicted_labels or [matched_label],
        label_scores=label_scores or {matched_label: 0.9},
    )


@pytest.mark.asyncio
async def test_returns_upgrade_envelope(app: FastAPI) -> None:
    """Happy path: one swap surfaces with decorated `in` payload."""
    conn = _FakeConn(
        resolve_rows=[_resolve_row("Cultivate", "aaaaaaaa-0000-0000-0000-000000000001")],
        upgrade_rows=[_upgrade_row()],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/upgrade", json={"decklist": "1 Cultivate"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "deck_upgrades"
    assert len(body["swaps"]) == 1
    swap = body["swaps"][0]
    assert swap["out"]["name"] == "Cultivate"
    assert swap["out"]["edhrec_rank"] == 200
    assert swap["in"]["name"] == "Farseek"
    # `in` decorated with predicted_labels + label_scores like other endpoints.
    assert "predicted_labels" in swap["in"]
    assert swap["matched_label"] == "ramp"
    assert swap["score"] == 150


@pytest.mark.asyncio
async def test_score_ranks_bigger_wins_first(app: FastAPI) -> None:
    """Larger rank-delta ranks higher in the swaps list."""
    conn = _FakeConn(
        resolve_rows=[_resolve_row("Cultivate", "aaaaaaaa-0000-0000-0000-000000000001")],
        upgrade_rows=[
            _upgrade_row(out_rank=1000, in_rank=100, out_name="X", in_name="Y"),
            _upgrade_row(out_rank=500, in_rank=400, out_name="A", in_name="B"),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/upgrade", json={"decklist": "1 Cultivate"})
    body = r.json()
    # Router preserves the SQL's ORDER BY score DESC; we just check the score
    # values line up with our fixture ordering.
    assert body["swaps"][0]["score"] == 900
    assert body["swaps"][1]["score"] == 100


@pytest.mark.asyncio
async def test_empty_swaps_when_no_upgrades_available(app: FastAPI) -> None:
    """Deck resolves but the SQL returns 0 rows → empty swaps, 200."""
    conn = _FakeConn(
        resolve_rows=[_resolve_row("Sol Ring", "aaaaaaaa-0000-0000-0000-000000000002")],
        upgrade_rows=[],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/upgrade", json={"decklist": "1 Sol Ring"})
    assert r.status_code == 200
    body = r.json()
    assert body["swaps"] == []


@pytest.mark.asyncio
async def test_commander_ci_passed_as_param(app: FastAPI) -> None:
    """Setting `commander` resolves its CI and passes it as `:ci` param."""
    commander = SimpleNamespace(color_identity=["R"])
    conn = _FakeConn(
        resolve_rows=[_resolve_row("Sol Ring", "aaaaaaaa-0000-0000-0000-000000000002")],
        upgrade_rows=[],
        commander_row=commander,
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/upgrade",
            json={"decklist": "1 Sol Ring", "commander": "Krenko, Mob Boss"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["commander_color_identity"] == ["R"]
    # The upgrade SQL params should carry `ci`.
    ci_params = [p for p in conn.executed_params if p and "ci" in p]
    assert ci_params, "commander CI not passed to upgrade SQL"


@pytest.mark.asyncio
async def test_unknown_commander_422(app: FastAPI) -> None:
    """Commander name that doesn't resolve → 422 commander_not_found."""
    conn = _FakeConn(
        resolve_rows=[_resolve_row("Sol Ring", "aaaaaaaa-0000-0000-0000-000000000002")],
        commander_row=None,
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/upgrade",
            json={"decklist": "1 Sol Ring", "commander": "Nonexistent Legend"},
        )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "commander_not_found"


@pytest.mark.asyncio
async def test_budget_passed_as_param(app: FastAPI) -> None:
    """`budget_usd` in the body is forwarded as `:budget` SQL param."""
    conn = _FakeConn(
        resolve_rows=[_resolve_row("Cultivate", "aaaaaaaa-0000-0000-0000-000000000001")],
        upgrade_rows=[],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/upgrade",
            json={"decklist": "1 Cultivate", "budget_usd": 5.0},
        )
    assert r.status_code == 200
    budget_params = [
        p for p in conn.executed_params if p and "budget" in (p or {})
    ]
    assert budget_params
    assert budget_params[0]["budget"] == 5.0


@pytest.mark.asyncio
async def test_empty_decklist_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/upgrade", json={"decklist": ""})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_limit_clamped_to_range(app: FastAPI) -> None:
    """`limit` out of [1, 25] → 422 via FastAPI validator."""
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r_high = await ac.post(
            "/decks/upgrade?limit=100", json={"decklist": "1 Sol Ring"}
        )
        r_zero = await ac.post(
            "/decks/upgrade?limit=0", json={"decklist": "1 Sol Ring"}
        )
    assert r_high.status_code == 422
    assert r_zero.status_code == 422


@pytest.mark.asyncio
async def test_dominant_label_picked_by_score_not_array_position(app: FastAPI) -> None:
    """Bug.2: `matched_label` must come from `label_scores` argmax, not
    from `predicted_labels[1]` (which is Postgres 1-indexed first element
    and stable to LABELS tuple order, not to the actual highest score).

    Router-side pin: SQL must reference `jsonb_each_text(dc.label_scores)`
    and must NOT reference `predicted_labels[1]` for the dominant pick.
    """
    conn = _FakeConn(
        resolve_rows=[_resolve_row("Cultivate", "aaaaaaaa-0000-0000-0000-000000000001")],
        upgrade_rows=[],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/upgrade", json={"decklist": "1 Cultivate"})
    assert r.status_code == 200
    # Pin: the upgrade SQL must pick dominant label from label_scores, not
    # predicted_labels array position.
    sql_calls = [str(s) for s in conn.executed_sql]
    upgrade_sqls = [s for s in sql_calls if "LATERAL" in s and "cand" in s]
    assert upgrade_sqls, "upgrade SQL not fired"
    for s in upgrade_sqls:
        assert "jsonb_each_text" in s, (
            "dominant label must be picked via jsonb_each_text(label_scores)"
        )
        assert "predicted_labels[1]" not in s, (
            "predicted_labels[1] is alphabetically-earliest, not score-max"
        )


@pytest.mark.asyncio
async def test_d14_14_legality_gate_default_commander(app: FastAPI) -> None:
    """D14.14 — no `fmt` param + no commander → defaults to `vintage`.

    Vintage bans the fewest cards, so it's the least-restrictive default
    for non-commander decklists. Also asserts the WHERE clause fires and
    the response echoes the chosen format.
    """
    conn = _FakeConn(
        resolve_rows=[_resolve_row("Cultivate", "aaaaaaaa-0000-0000-0000-000000000001")],
        upgrade_rows=[_upgrade_row()],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/upgrade", json={"decklist": "1 Cultivate"})
    assert r.status_code == 200
    body = r.json()
    assert body["format"] == "vintage"
    # Pin: upgrade SQL must include the legality WHERE clause.
    upgrade_sqls = [s for s in conn.executed_sql if "LATERAL" in s and "cand" in s]
    for s in upgrade_sqls:
        assert "legalities" in s
        assert ":fmt" in s
    upgrade_params = [
        p for p, s in zip(conn.executed_params, conn.executed_sql, strict=False)
        if "LATERAL" in s and "cand" in s
    ]
    assert all(p.get("fmt") == "vintage" for p in upgrade_params)


@pytest.mark.asyncio
async def test_d14_14_legality_gate_defaults_commander_when_commander_supplied(
    app: FastAPI,
) -> None:
    """D14.14 — `commander` in body → default fmt = `commander`."""
    conn = _FakeConn(
        resolve_rows=[_resolve_row("Cultivate", "aaaaaaaa-0000-0000-0000-000000000001")],
        upgrade_rows=[_upgrade_row()],
        commander_row=SimpleNamespace(color_identity=["G"]),
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/upgrade",
            json={"decklist": "1 Cultivate", "commander": "Selvala"},
        )
    body = r.json()
    assert body["format"] == "commander"


@pytest.mark.asyncio
async def test_d14_14_explicit_fmt_query_param_overrides_default(
    app: FastAPI,
) -> None:
    """D14.14 — `?fmt=modern` overrides the commander/vintage default."""
    conn = _FakeConn(
        resolve_rows=[_resolve_row("Cultivate", "aaaaaaaa-0000-0000-0000-000000000001")],
        upgrade_rows=[_upgrade_row()],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/upgrade?fmt=modern", json={"decklist": "1 Cultivate"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["format"] == "modern"
    upgrade_params = [
        p for p, s in zip(conn.executed_params, conn.executed_sql, strict=False)
        if "LATERAL" in s and "cand" in s
    ]
    assert all(p.get("fmt") == "modern" for p in upgrade_params)


@pytest.mark.asyncio
async def test_empty_deck_after_resolve_skips_upgrade_sql(app: FastAPI) -> None:
    """All names miss → skip the upgrade SQL (empty swaps, still 200)."""
    conn = _FakeConn(resolve_rows=[], upgrade_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/upgrade", json={"decklist": "1 Made Up"})
    assert r.status_code == 200
    body = r.json()
    assert body["swaps"] == []
    assert "Made Up" in body["unresolved"]
