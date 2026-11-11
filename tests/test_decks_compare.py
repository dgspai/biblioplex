"""D15.5 — ``POST /decks/compare`` (§6.7 companion)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine
from biblioplex.routers.decks._common import DECKLIST_MAX_LEN

from .test_decks_analyze import _FakeConn, _FakeEngine, _Row, _settings

_SOL = UUID("11111111-1111-1111-1111-111111111111")
_BOLT = UUID("22222222-2222-2222-2222-222222222222")
_CULTIVATE = UUID("33333333-3333-3333-3333-333333333333")
_FOREST = UUID("44444444-4444-4444-4444-444444444444")


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=_settings())


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _corpus_conn() -> _FakeConn:
    """Small shared corpus so both A and B can resolve names."""
    return _FakeConn(
        exact_rows=[
            _Row(id=_SOL, lname="sol ring"),
            _Row(id=_BOLT, lname="lightning bolt"),
            _Row(id=_CULTIVATE, lname="cultivate"),
            _Row(id=_FOREST, lname="forest"),
        ],
        slot_rows=[
            _Row(id=_SOL, name="Sol Ring", type_line="Artifact",
                 predicted_labels=["ramp"], cmc=1, mana_cost="{1}"),
            _Row(id=_BOLT, name="Lightning Bolt", type_line="Instant",
                 predicted_labels=["targeted_disrupt"], cmc=1, mana_cost="{R}"),
            _Row(id=_CULTIVATE, name="Cultivate", type_line="Sorcery",
                 predicted_labels=["ramp"], cmc=3, mana_cost="{2}{G}"),
            _Row(id=_FOREST, name="Forest", type_line="Basic Land — Forest",
                 predicted_labels=[]),
        ],
    )


async def test_compare_envelope_and_diff(app: FastAPI) -> None:
    conn = _corpus_conn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post("/decks/compare", json={
            "a": "1 Sol Ring\n1 Lightning Bolt\n1 Forest",
            "b": "1 Sol Ring\n2 Lightning Bolt\n1 Cultivate",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "deck_compare"
    assert body["a"]["object"] == "deck_analysis"
    assert body["b"]["object"] == "deck_analysis"
    diff = body["diff"]
    # Cultivate added; Forest removed; Bolt qty changed 1→2.
    added_names = {c["name"] for c in diff["added"]}
    removed_names = {c["name"] for c in diff["removed"]}
    changed = {c["name"]: c for c in diff["changed_qty"]}
    assert added_names == {"Cultivate"}
    assert removed_names == {"Forest"}
    assert changed["Lightning Bolt"]["delta"] == 1
    assert changed["Lightning Bolt"]["qty_a"] == 1
    assert changed["Lightning Bolt"]["qty_b"] == 2


async def test_compare_counts_and_curve_deltas(app: FastAPI) -> None:
    conn = _corpus_conn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post("/decks/compare", json={
            "a": "1 Sol Ring",
            "b": "1 Sol Ring\n1 Cultivate",
        })
    diff = resp.json()["diff"]
    # Cultivate is ramp+3CMC+G-pip; A had ramp=1, B has ramp=2.
    assert diff["counts_delta"]["ramp"] == 1
    assert diff["curve_delta"]["3"] == 1
    assert diff["pips_delta"]["G"] == 1


async def test_compare_identical_decks_zero_diff(app: FastAPI) -> None:
    conn = _corpus_conn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post("/decks/compare", json={
            "a": "1 Sol Ring\n1 Lightning Bolt",
            "b": "1 Sol Ring\n1 Lightning Bolt",
        })
    diff = resp.json()["diff"]
    assert diff["added"] == []
    assert diff["removed"] == []
    assert diff["changed_qty"] == []
    assert all(v == 0 for v in diff["counts_delta"].values())


async def test_compare_empty_a_returns_422(app: FastAPI) -> None:
    conn = _corpus_conn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post("/decks/compare", json={"a": "", "b": "1 Sol Ring"})
    assert resp.status_code == 422
    assert "'a'" in resp.json()["details"]


async def test_compare_empty_b_returns_422(app: FastAPI) -> None:
    conn = _corpus_conn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post("/decks/compare", json={"a": "1 Sol Ring", "b": ""})
    assert resp.status_code == 422
    assert "'b'" in resp.json()["details"]


async def test_compare_opens_two_conns_for_parallel_dispatch(app: FastAPI) -> None:
    """D15.11: two connections opened so the two pipelines can gather."""
    conn = _corpus_conn()

    class _CountingEngine:
        connect_calls = 0

        def __init__(self, conn: object) -> None:
            self._conn = conn

        def connect(self) -> object:
            _CountingEngine.connect_calls += 1
            return _FakeEngine(self._conn).connect()

    counting = _CountingEngine(conn)
    app.dependency_overrides[get_read_engine] = lambda: counting
    async for client in _client(app):
        resp = await client.post("/decks/compare", json={
            "a": "1 Sol Ring",
            "b": "1 Cultivate",
        })
    assert resp.status_code == 200
    assert _CountingEngine.connect_calls == 2


async def test_compare_oversize_returns_422(app: FastAPI) -> None:
    conn = _corpus_conn()
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    big = "1 Sol Ring\n" * (DECKLIST_MAX_LEN // 10)
    async for client in _client(app):
        resp = await client.post("/decks/compare", json={"a": big, "b": "1 Sol Ring"})
    assert resp.status_code == 422
    assert "too long" in resp.json()["details"]
