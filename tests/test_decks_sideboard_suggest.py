"""D14.12 — ``POST /decks/sideboard-suggest`` fixed meta-hate slots (§6.17)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.decklist.resolve import ResolvedDeckRow
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine
from biblioplex.routers.decks import SIDEBOARD_SLUGS

from .test_decks_analyze import _settings


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(
        self,
        sideboard_rows: list[Any] | None = None,
        commander_row: Any = None,
    ) -> None:
        self._sideboard = sideboard_rows or []
        self._commander = commander_row
        self.executed_sql: list[str] = []
        self.executed_params: list[Any] = []

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed_sql.append(sql)
        self.executed_params.append(params)
        if "LATERAL" in sql and "oracle_tags" in sql:
            return _FakeResult(self._sideboard)
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


def _row(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        oracle_id=UUID("aaaaaaaa-0000-0000-0000-000000000001"),
        name=name,
        type_line="Sorcery",
        layout="normal",
        card_faces=None,
        predicted_labels=[],
        cmc=1,
        mana_cost="{1}",
        color_identity=[],
        legalities={},
        has_unlimited_copies=False,
        price_usd=None,
    )


async def _override(app: FastAPI, monkeypatch: pytest.MonkeyPatch,
    *,
    resolved: list[ResolvedDeckRow] | None = None,
    sideboard_rows: list[Any] | None = None,
    commander_row: Any = None,
) -> _FakeConn:
    conn = _FakeConn(
        sideboard_rows=sideboard_rows, commander_row=commander_row
    )

    async def _fake_resolve(c: object, names: list[str]) -> tuple:
        return resolved or [], []

    import biblioplex.routers.decks.sideboard as decks_mod
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    monkeypatch.setattr(decks_mod, "resolve_and_fetch_deck_rows", _fake_resolve)
    return conn


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=_settings())


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _side_row(label: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        label=label,
        raw={"name": name, "id": "x", "type_line": "Instant"},
        predicted_labels=[],
        label_scores={},
    )


async def test_sideboard_envelope_and_slugs(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Envelope: `deck_sideboard`; suggestions keys = every configured slug."""
    await _override(app, monkeypatch)
    async for client in _client(app):
        resp = await client.post(
            "/decks/sideboard-suggest", json={"decklist": "1 X"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "deck_sideboard"
    # Every configured slug should surface as a key (empty list allowed).
    for slug in SIDEBOARD_SLUGS:
        assert slug in body["suggestions"]


async def test_sideboard_rows_bucket_by_label(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _side_row("graveyard-hate", "Rest in Peace"),
        _side_row("graveyard-hate", "Leyline of the Void"),
        _side_row("counterspell", "Force of Will"),
    ]
    await _override(app, monkeypatch, sideboard_rows=rows)
    async for client in _client(app):
        resp = await client.post(
            "/decks/sideboard-suggest", json={"decklist": "1 X"}
        )
    body = resp.json()
    assert len(body["suggestions"]["graveyard-hate"]) == 2
    assert len(body["suggestions"]["counterspell"]) == 1
    assert len(body["suggestions"]["board-wipe"]) == 0  # empty slot preserved


async def test_sideboard_sql_binds_slug_list_and_seen(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = [ResolvedDeckRow(input_name="A", row=_row("A"))]
    conn = await _override(app, monkeypatch, resolved=resolved)
    async for client in _client(app):
        await client.post("/decks/sideboard-suggest", json={"decklist": "1 A"})
    sideboard_sqls = [
        (s, p) for s, p in zip(conn.executed_sql, conn.executed_params, strict=False)
        if "LATERAL" in s and "oracle_tags" in s
    ]
    assert sideboard_sqls
    sql, params = sideboard_sqls[0]
    assert params["slugs"] == list(SIDEBOARD_SLUGS)
    assert isinstance(params["seen"], list)
    assert isinstance(params["limit"], int)


async def test_sideboard_bug27_uses_cast(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = await _override(app, monkeypatch)
    async for client in _client(app):
        await client.post("/decks/sideboard-suggest", json={"decklist": "1 X"})
    for s in conn.executed_sql:
        if "LATERAL" in s and "oracle_tags" in s:
            assert ":slugs::text[]" not in s
            assert ":seen::uuid[]" not in s
            assert "CAST(:slugs AS text[])" in s
            assert "CAST(:seen AS uuid[])" in s


async def test_sideboard_commander_filter_binds_ci(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = await _override(
        app, monkeypatch, commander_row=SimpleNamespace(color_identity=["W", "U"])
    )
    async for client in _client(app):
        await client.post(
            "/decks/sideboard-suggest",
            json={"decklist": "1 X", "commander": "Baral"},
        )
    sideboard_params = [
        p for p, s in zip(conn.executed_params, conn.executed_sql, strict=False)
        if "LATERAL" in s and "oracle_tags" in s
    ]
    assert sideboard_params[0]["ci"] == ["W", "U"]


async def test_sideboard_limit_clamp(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async for client in _client(app):
        resp = await client.post(
            "/decks/sideboard-suggest?limit=999", json={"decklist": "1 X"}
        )
    assert resp.status_code == 422


async def test_sideboard_empty_body_rejected(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async for client in _client(app):
        resp = await client.post(
            "/decks/sideboard-suggest", json={"decklist": ""}
        )
    assert resp.status_code == 422


async def test_sideboard_unknown_commander_returns_422(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    await _override(app, monkeypatch, commander_row=None)  # commander lookup miss
    async for client in _client(app):
        resp = await client.post(
            "/decks/sideboard-suggest",
            json={"decklist": "1 X", "commander": "Made Up"},
        )
    assert resp.status_code == 422
