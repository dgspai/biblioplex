"""Tests for ``POST /decks/manabase`` (D14.6)."""

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

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Routes execute() by SQL substring: commander lookup vs nonbasic pick."""

    def __init__(
        self,
        *,
        commander_row: Any = None,
        nonbasic_rows: list[Any] | None = None,
    ) -> None:
        self._commander = commander_row
        self._nonbasics = nonbasic_rows or []
        self.executed_params: list[Any] = []

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed_params.append(params)
        if "color_identity" in sql and "LOWER(name)" in sql:
            return _FakeResult(
                [self._commander] if self._commander is not None else []
            )
        if "array_position" in sql:
            return _FakeResult(self._nonbasics)
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


def _land_row(name: str, ci: list[str], price: float | None = None) -> Any:
    return SimpleNamespace(
        name=name,
        raw={"name": name, "type_line": "Land"},
        color_identity=ci,
        price_usd=price,
    )


@pytest.mark.asyncio
async def test_returns_manabase_envelope(app: FastAPI) -> None:
    """Mono-red commander → nonbasics returned + Mountains as basics."""
    conn = _FakeConn(
        commander_row=SimpleNamespace(color_identity=["R"]),
        nonbasic_rows=[
            _land_row("Command Tower", []),
            _land_row("Blood Crypt", ["B", "R"]),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/manabase", json={"commander": "Krenko, Mob Boss"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "deck_manabase"
    assert body["commander_color_identity"] == ["R"]
    assert len(body["nonbasics"]) == 2
    # 37 target - 2 nonbasics = 35 basics, all Mountains for mono-red.
    assert body["basics"] == {"Mountain": 35}


@pytest.mark.asyncio
async def test_ci_passed_to_land_query(app: FastAPI) -> None:
    """Commander CI is passed as `:ci` param on the nonbasic query."""
    conn = _FakeConn(
        commander_row=SimpleNamespace(color_identity=["W", "U"]),
        nonbasic_rows=[],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/manabase", json={"commander": "Ephara, God of the Polis"}
        )
    assert r.status_code == 200
    ci_params = [p for p in conn.executed_params if p and "ci" in (p or {})]
    assert ci_params
    assert ci_params[0]["ci"] == ["W", "U"]


@pytest.mark.asyncio
async def test_budget_forwarded(app: FastAPI) -> None:
    conn = _FakeConn(
        commander_row=SimpleNamespace(color_identity=["G"]),
        nonbasic_rows=[],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/manabase",
            json={"commander": "Ghalta, Primal Hunger", "budget_usd": 5.0},
        )
    assert r.status_code == 200
    budget_params = [
        p for p in conn.executed_params if p and "budget" in (p or {})
    ]
    assert budget_params
    assert budget_params[0]["budget"] == 5.0


@pytest.mark.asyncio
async def test_pip_weights_override_default(app: FastAPI) -> None:
    """Body `pip_weights` override the default-even distribution."""
    conn = _FakeConn(
        commander_row=SimpleNamespace(color_identity=["W", "U", "B"]),
        nonbasic_rows=[],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/manabase",
            json={
                "commander": "Kambal, Consul of Allocation",
                "pip_weights": {"W": 1, "U": 0, "B": 9},
            },
        )
    assert r.status_code == 200
    body = r.json()
    # 37 basics on 1:0:9 weights → W=4, B=33.
    assert body["basics"].get("Swamp", 0) > body["basics"].get("Plains", 0)
    assert body["basics"].get("Island", 0) == 0


@pytest.mark.asyncio
async def test_bug22_bad_pip_weights_key_rejected(app: FastAPI) -> None:
    """Bug.22: pip_weights key outside {W,U,B,R,G,C} → 422 bad_pip_weights.

    Prior behavior silently ignored non-WUBRGC keys — `{"X": 5}` produced
    `total_weight=0`, `distribute_basics` short-circuited to an empty
    dict, and the response returned zero basics with no error. User
    reads that as "manabase incomplete" instead of "your keys were bad".
    """
    conn = _FakeConn(
        commander_row=SimpleNamespace(color_identity=["W", "U", "B"]),
        nonbasic_rows=[],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/manabase",
            json={
                "commander": "Kambal, Consul of Allocation",
                "pip_weights": {"X": 5, "W": 1},
            },
        )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "bad_pip_weights"
    assert "X" in body["details"]


@pytest.mark.asyncio
async def test_bug22_bad_pip_weights_lowercase_key_rejected(app: FastAPI) -> None:
    """Lower-case color letters also rejected — schema uses WUBRGC upper."""
    conn = _FakeConn(
        commander_row=SimpleNamespace(color_identity=["W"]),
        nonbasic_rows=[],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/manabase",
            json={
                "commander": "Kambal, Consul of Allocation",
                "pip_weights": {"w": 3},
            },
        )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "bad_pip_weights"


@pytest.mark.asyncio
async def test_unknown_commander_422(app: FastAPI) -> None:
    conn = _FakeConn(commander_row=None)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/manabase", json={"commander": "Nonexistent Legend"}
        )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "commander_not_found"


@pytest.mark.asyncio
async def test_land_count_clamped(app: FastAPI) -> None:
    """`land_count` outside [20, 50] → 422 (FastAPI validator)."""
    conn = _FakeConn(
        commander_row=SimpleNamespace(color_identity=["R"]),
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r_low = await ac.post(
            "/decks/manabase?land_count=5", json={"commander": "Krenko, Mob Boss"}
        )
        r_high = await ac.post(
            "/decks/manabase?land_count=200",
            json={"commander": "Krenko, Mob Boss"},
        )
    assert r_low.status_code == 422
    assert r_high.status_code == 422


@pytest.mark.asyncio
async def test_land_count_forwarded(app: FastAPI) -> None:
    """`land_count=25` reduces the basics total."""
    conn = _FakeConn(
        commander_row=SimpleNamespace(color_identity=["R"]),
        nonbasic_rows=[_land_row("Command Tower", [])],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/manabase?land_count=25",
            json={"commander": "Krenko, Mob Boss"},
        )
    body = r.json()
    total = len(body["nonbasics"]) + sum(body["basics"].values())
    assert total == 25


@pytest.mark.asyncio
async def test_colorless_commander_distributes_wastes(app: FastAPI) -> None:
    """D16.5: Colorless commander → remaining slots filled with Wastes.

    Prior behavior returned a note stating Wastes were "out of scope";
    now we distribute them so the manabase reaches `land_count`.
    """
    conn = _FakeConn(
        commander_row=SimpleNamespace(color_identity=[]),
        nonbasic_rows=[_land_row("Reliquary Tower", [])],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/manabase", json={"commander": "Karn, Silver Golem"})
    assert r.status_code == 200
    body = r.json()
    # 1 nonbasic + Wastes fill the rest of the default 37-land manabase.
    assert body["basics"] == {"Wastes": 36}
    # The "out of scope" note is gone — Wastes is now handled.
    assert not any("out of scope" in n.lower() for n in body["notes"])


@pytest.mark.asyncio
async def test_colorless_min_basics_does_not_cap_nonbasics(app: FastAPI) -> None:
    """D10.15: colorless commander must not reserve 5 basic slots artificially.

    Prior `_min_basics([]) == 5` capped `nb_limit` at `land_count - 5`
    for a colorless commander, even though every curated priority-list
    land has a non-empty color_identity and would be excluded by the
    `<@` filter anyway. The floor makes non-colorless deployments
    lose 5 land slots to a hardcoded reserve. Fix: colorless → 0.
    """
    conn = _FakeConn(
        commander_row=SimpleNamespace(color_identity=[]),
        nonbasic_rows=[],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/manabase", json={"commander": "Karn, Silver Golem"})
    assert r.status_code == 200
    nb_params = [
        p for p in conn.executed_params if p and "nb_limit" in (p or {})
    ]
    assert nb_params
    # Default land_count is 37, priority list has 36 entries — colorless
    # commander should get the full priority-list slot budget, not 32.
    assert nb_params[0]["nb_limit"] == 36
