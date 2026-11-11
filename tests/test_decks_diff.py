"""D14.15 — ``POST /decks/diff`` line-level decklist diff (§6.18).

Pure-Python — no SQL, no DB engine dependency. Tests use the app
without overriding get_read_engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.main import create_app
from biblioplex.routers.decks._common import DECKLIST_MAX_LEN

from .test_decks_analyze import _settings


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=_settings())


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_diff_envelope_and_shape(app: FastAPI) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/diff", json={
            "a": "1 Sol Ring\n1 Lightning Bolt\n1 Forest",
            "b": "1 Sol Ring\n2 Lightning Bolt\n1 Cultivate",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "deck_diff"
    assert {"added", "removed", "qty_changed",
            "unchanged_count", "a_total", "b_total"} <= set(body)


async def test_diff_added_removed_and_qty_changed(app: FastAPI) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/diff", json={
            "a": "1 Sol Ring\n1 Lightning Bolt\n1 Forest",
            "b": "1 Sol Ring\n2 Lightning Bolt\n1 Cultivate",
        })
    body = resp.json()
    # Cultivate added; Forest removed; Bolt qty changed 1→2; Sol Ring unchanged.
    added_names = {r["name"] for r in body["added"]}
    removed_names = {r["name"] for r in body["removed"]}
    changed = {r["name"]: r for r in body["qty_changed"]}
    assert added_names == {"Cultivate"}
    assert removed_names == {"Forest"}
    assert "Lightning Bolt" in changed
    assert changed["Lightning Bolt"]["from"] == 1
    assert changed["Lightning Bolt"]["to"] == 2
    assert changed["Lightning Bolt"]["delta"] == 1
    assert body["unchanged_count"] == 1


async def test_diff_totals(app: FastAPI) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/diff", json={
            "a": "4 Lightning Bolt\n1 Sol Ring",
            "b": "2 Lightning Bolt\n1 Cultivate",
        })
    body = resp.json()
    assert body["a_total"] == 5
    assert body["b_total"] == 3


async def test_diff_identical_decklists(app: FastAPI) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/diff", json={
            "a": "1 Sol Ring\n1 Lightning Bolt",
            "b": "1 Sol Ring\n1 Lightning Bolt",
        })
    body = resp.json()
    assert body["added"] == []
    assert body["removed"] == []
    assert body["qty_changed"] == []
    assert body["unchanged_count"] == 2


async def test_diff_negative_delta(app: FastAPI) -> None:
    # `b - a`: qty went from 4 → 1, delta = -3.
    async for client in _client(app):
        resp = await client.post("/decks/diff", json={
            "a": "4 Lightning Bolt",
            "b": "1 Lightning Bolt",
        })
    body = resp.json()
    assert body["qty_changed"] == [
        {"name": "Lightning Bolt", "from": 4, "to": 1, "delta": -3},
    ]


async def test_diff_empty_a_side_rejected(app: FastAPI) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/diff", json={"a": "", "b": "1 Sol Ring"})
    assert resp.status_code == 422
    assert "'a'" in resp.json()["details"]


async def test_diff_empty_b_side_rejected(app: FastAPI) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/diff", json={"a": "1 Sol Ring", "b": ""})
    assert resp.status_code == 422
    assert "'b'" in resp.json()["details"]


async def test_diff_oversize_rejected(app: FastAPI) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/diff", json={
            "a": "1 Sol Ring" + "\n" * (DECKLIST_MAX_LEN + 1),
            "b": "1 Sol Ring",
        })
    assert resp.status_code == 422
    assert "too long" in resp.json()["details"]


async def test_diff_case_insensitive_name_matching(app: FastAPI) -> None:
    # Bolt vs bolt should collapse to the same key.
    async for client in _client(app):
        resp = await client.post("/decks/diff", json={
            "a": "1 lightning bolt",
            "b": "1 Lightning Bolt",
        })
    body = resp.json()
    assert body["added"] == []
    assert body["removed"] == []
    assert body["qty_changed"] == []
    assert body["unchanged_count"] == 1
