"""Router tests for ``POST /decks/legality`` (D14.7).

Verifies the decklist parse → resolve → check_legality pipeline plus
request-envelope contract: `commander` name resolves to CI via the shared
`_resolve_commander_ci` helper (same SQL substring the /suggest / /upgrade
/ /manabase / /bracket tests key on), and the response body carries
`{object, legal, format, violations, deck_size, expected_deck_size,
commander_color_identity, unresolved}`.
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
    """Routes execute() by SQL substring: resolve fold vs commander CI."""

    def __init__(
        self,
        *,
        resolve_rows: list[Any] | None = None,
        commander_row: Any = None,
    ) -> None:
        self._resolve = resolve_rows or []
        self._commander = commander_row
        self.executed_sql: list[str] = []
        self.executed_params: list[Any] = []

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed_sql.append(sql)
        self.executed_params.append(params)
        if "unnest(" in sql and ":ords" in sql:
            return _FakeResult(self._resolve)
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


def _resolve_row(
    *,
    name: str,
    color_identity: list[str] | None = None,
    legalities: dict[str, str] | None = None,
    type_line: str = "Artifact",
    input_name: str | None = None,
) -> Any:
    return SimpleNamespace(
        ord=0,
        input_name=input_name or name,
        similarity=None,
        id=UUID("00000000-0000-0000-0000-000000000001"),
        oracle_id=UUID("00000000-0000-0000-0000-000000000002"),
        name=name,
        type_line=type_line,
        layout="normal",
        card_faces=None,
        predicted_labels=[],
        cmc=1,
        mana_cost="{1}",
        color_identity=color_identity or [],
        legalities=legalities or {"commander": "legal"},
    )


@pytest.mark.asyncio
async def test_legality_envelope_default_commander(app: FastAPI) -> None:
    """Default format is commander; envelope carries the expected keys."""
    conn = _FakeConn(
        resolve_rows=[_resolve_row(name="Sol Ring")],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/legality", json={"decklist": "1 Sol Ring"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "deck_legality"
    assert body["format"] == "commander"
    assert set(body.keys()) >= {
        "object",
        "format",
        "legal",
        "violations",
        "deck_size",
        "expected_deck_size",
        "commander_color_identity",
        "unresolved",
    }


@pytest.mark.asyncio
async def test_legality_flags_banned_card(app: FastAPI) -> None:
    """`legalities[commander] == 'banned'` surfaces as `not_legal` violation."""
    conn = _FakeConn(
        resolve_rows=[
            _resolve_row(
                name="Black Lotus",
                legalities={"commander": "banned"},
            )
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/legality", json={"decklist": "1 Black Lotus"})
    body = r.json()
    assert body["legal"] is False
    reasons = [v["reason"] for v in body["violations"]]
    assert "not_legal" in reasons


@pytest.mark.asyncio
async def test_legality_commander_ci_enforced(app: FastAPI) -> None:
    """Card CI outside commander CI → color_identity violation."""
    conn = _FakeConn(
        resolve_rows=[
            _resolve_row(
                name="Lightning Bolt",
                color_identity=["R"],
                legalities={"commander": "legal"},
            )
        ],
        commander_row=SimpleNamespace(color_identity=["W", "U"]),
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/legality",
            json={"decklist": "1 Lightning Bolt", "commander": "Grand Arbiter"},
        )
    body = r.json()
    reasons = [v["reason"] for v in body["violations"]]
    assert "color_identity" in reasons
    assert body["commander_color_identity"] == ["W", "U"]


@pytest.mark.asyncio
async def test_legality_unknown_commander_returns_422(app: FastAPI) -> None:
    """Commander name that doesn't resolve → 422 commander_not_found."""
    conn = _FakeConn(commander_row=None)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/legality",
            json={"decklist": "1 Sol Ring", "commander": "Nobody"},
        )
    assert r.status_code == 422
    assert r.json()["code"] == "commander_not_found"


@pytest.mark.asyncio
async def test_legality_modern_max_4_rule(app: FastAPI) -> None:
    """`format=modern`, qty=5 non-basic → over_max_copies."""
    conn = _FakeConn(
        resolve_rows=[
            _resolve_row(
                name="Lightning Bolt",
                legalities={"modern": "legal"},
                color_identity=["R"],
            )
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/legality",
            json={"decklist": "5 Lightning Bolt", "format": "modern"},
        )
    body = r.json()
    assert body["format"] == "modern"
    reasons = [v["reason"] for v in body["violations"]]
    assert "over_max_copies" in reasons


@pytest.mark.asyncio
async def test_legality_empty_decklist_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/decks/legality", json={"decklist": ""})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_legality_oversize_decklist_422(app: FastAPI) -> None:
    conn = _FakeConn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/legality", json={"decklist": "1 X\n" * (DECKLIST_MAX_LEN // 3)}
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_legality_unresolved_surfaces_as_violation(app: FastAPI) -> None:
    """Names that don't resolve get an `unresolved` violation reason."""
    conn = _FakeConn(resolve_rows=[])  # nothing resolves
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/decks/legality", json={"decklist": "1 Frobnicator"}
        )
    body = r.json()
    reasons = [v["reason"] for v in body["violations"]]
    assert "unresolved" in reasons
    assert "Frobnicator" in body["unresolved"]
