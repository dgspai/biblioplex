"""D14.19 — ``POST /decks/synergy`` pairwise co-occurrence (§6.22)."""

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

from .test_decks_analyze import _settings


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeConn:
    def __init__(self, synergy_rows: list[Any] | None = None) -> None:
        self._synergy = synergy_rows or []
        self.executed_sql: list[str] = []
        self.executed_params: list[Any] = []

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed_sql.append(sql)
        self.executed_params.append(params)
        if "tag_pairs" in sql or "combo_pairs" in sql:
            return _FakeResult(self._synergy)
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


async def _override(app: FastAPI, monkeypatch: pytest.MonkeyPatch, resolved: list[ResolvedDeckRow], synergy_rows: list[Any]
) -> _FakeConn:
    conn = _FakeConn(synergy_rows=synergy_rows)

    async def _fake_resolve(c: object, names: list[str]) -> tuple:
        return resolved, []

    import biblioplex.routers.decks.synergy as decks_mod
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


def _synergy_row(
    *,
    a_name: str,
    b_name: str,
    n_tags: int,
    n_combos: int,
    shared_tags: list[str],
    shared_combos: list[str],
) -> SimpleNamespace:
    return SimpleNamespace(
        a_oid=UUID("aaaaaaaa-0000-0000-0000-000000000001"),
        b_oid=UUID("bbbbbbbb-0000-0000-0000-000000000001"),
        a_id=UUID("00000000-0000-0000-0000-00000000000a"),
        b_id=UUID("00000000-0000-0000-0000-00000000000b"),
        a_name=a_name,
        b_name=b_name,
        n_tags=n_tags,
        n_combos=n_combos,
        shared_tags=shared_tags,
        shared_combos=shared_combos,
        score=n_tags + 5 * n_combos,
    )


async def test_synergy_envelope(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = [ResolvedDeckRow(input_name="A", row=_row("A"))]
    await _override(app, monkeypatch, resolved, [])
    async for client in _client(app):
        resp = await client.post("/decks/synergy", json={"decklist": "1 A"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "deck_synergy"
    assert body["pairs"] == []


async def test_synergy_pair_shape(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = [
        ResolvedDeckRow(input_name="A", row=_row("A")),
        ResolvedDeckRow(input_name="B", row=_row("B")),
    ]
    rows = [_synergy_row(
        a_name="A", b_name="B", n_tags=2, n_combos=1,
        shared_tags=["ramp", "draw"], shared_combos=["combo-1"],
    )]
    await _override(app, monkeypatch, resolved, rows)
    async for client in _client(app):
        resp = await client.post("/decks/synergy", json={"decklist": "1 A\n1 B"})
    body = resp.json()
    pair = body["pairs"][0]
    assert pair["a"]["name"] == "A"
    assert pair["b"]["name"] == "B"
    assert pair["score"] == 7  # 2 + 5*1
    assert pair["shared_tags"] == ["ramp", "draw"]
    assert pair["shared_combos"] == ["combo-1"]


async def test_synergy_combo_signal_weighted_5x(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the score formula: n_tags + 5 * n_combos.
    resolved = [
        ResolvedDeckRow(input_name="A", row=_row("A")),
        ResolvedDeckRow(input_name="B", row=_row("B")),
    ]
    rows = [_synergy_row(
        a_name="A", b_name="B", n_tags=0, n_combos=2,
        shared_tags=[], shared_combos=["c1", "c2"],
    )]
    await _override(app, monkeypatch, resolved, rows)
    async for client in _client(app):
        resp = await client.post("/decks/synergy", json={"decklist": "1 A\n1 B"})
    assert resp.json()["pairs"][0]["score"] == 10


async def test_synergy_sql_binds_deck_oids_and_limit(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = [
        ResolvedDeckRow(input_name="A", row=_row("A")),
        ResolvedDeckRow(input_name="B", row=_row("B")),
    ]
    conn = await _override(app, monkeypatch, resolved, [])
    async for client in _client(app):
        await client.post(
            "/decks/synergy?limit=50", json={"decklist": "1 A\n1 B"}
        )
    # Assert the synergy SQL fired with deck_oids + limit binds.
    synergy_sqls = [s for s in conn.executed_sql if "tag_pairs" in s]
    assert synergy_sqls, "synergy SQL not fired"
    synergy_params = [
        p for p, s in zip(conn.executed_params, conn.executed_sql, strict=False)
        if "tag_pairs" in s
    ]
    assert synergy_params[0]["limit"] == 50
    assert isinstance(synergy_params[0]["deck_oids"], list)


async def test_synergy_bug27_uses_cast_not_double_colon(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard: SQL must use `CAST(:X AS TYPE)` not `:X::TYPE` per Bug.27.
    resolved = [ResolvedDeckRow(input_name="A", row=_row("A"))]
    conn = await _override(app, monkeypatch, resolved, [])
    async for client in _client(app):
        await client.post("/decks/synergy", json={"decklist": "1 A"})
    for s in conn.executed_sql:
        if "tag_pairs" in s:
            assert ":deck_oids::uuid[]" not in s, (
                "Bug.27: must use CAST(:deck_oids AS uuid[]), not "
                ":deck_oids::uuid[] — text() strips the `::` and drops "
                "the preceding bind."
            )
            assert "CAST(:deck_oids AS uuid[])" in s


async def test_synergy_empty_deck_after_resolve_skips_sql(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = await _override(app, monkeypatch, [], [])
    async for client in _client(app):
        resp = await client.post("/decks/synergy", json={"decklist": "1 Made Up"})
    body = resp.json()
    assert body["pairs"] == []
    # No synergy SQL should have fired.
    assert not any("tag_pairs" in s for s in conn.executed_sql)


async def test_synergy_limit_clamp_too_high(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/synergy?limit=999", json={"decklist": "1 A"})
    assert resp.status_code == 422


async def test_synergy_empty_body_rejected(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/synergy", json={"decklist": ""})
    assert resp.status_code == 422
