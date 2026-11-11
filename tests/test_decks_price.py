"""D14.11 — ``POST /decks/price`` sum + breakdown (§6.16)."""

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
from biblioplex.routers.decks._common import DECKLIST_MAX_LEN

from .test_decks_analyze import _settings


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=_settings())


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _row(
    name: str,
    *,
    price_usd: float | None = 1.0,
    labels: list[str] | None = None,
    cmc: float = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        name=name,
        type_line="Artifact",
        layout="normal",
        card_faces=None,
        predicted_labels=labels or [],
        cmc=cmc,
        mana_cost="{1}",
        color_identity=[],
        legalities={},
        has_unlimited_copies=False,
        price_usd=price_usd,
    )


class _FakeEngine:
    def __init__(self, resolved: list[ResolvedDeckRow], unresolved: list[str]) -> None:
        self._resolved = resolved
        self._unresolved = unresolved

    def connect(self) -> Any:  # noqa: ANN401
        return self  # ctx-mgr shim

    async def __aenter__(self) -> _FakeEngine:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


async def _override(
    app: FastAPI,
    resolved: list,
    unresolved: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine(resolved, unresolved)

    async def _fake_resolve(conn: object, names: list[str]) -> tuple:
        return resolved, unresolved

    import biblioplex.routers.decks.price as decks_mod
    app.dependency_overrides[get_read_engine] = lambda: engine
    monkeypatch.setattr(
        decks_mod, "resolve_and_fetch_deck_rows", _fake_resolve
    )


async def test_price_envelope_and_total(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = [
        ResolvedDeckRow(input_name="Sol Ring", row=_row("Sol Ring", price_usd=2.0)),
        ResolvedDeckRow(input_name="Bolt", row=_row("Lightning Bolt", price_usd=1.5, cmc=1)),
    ]
    await _override(app, resolved, [], monkeypatch)
    async for client in _client(app):
        resp = await client.post("/decks/price", json={"decklist": "1 Sol Ring\n1 Bolt"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "deck_price"
    assert body["total_usd"] == 3.5


async def test_price_missing_price_surfaced_not_zeroed(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = [
        ResolvedDeckRow(input_name="Black Lotus", row=_row("Black Lotus", price_usd=None)),
        ResolvedDeckRow(input_name="Sol Ring", row=_row("Sol Ring", price_usd=2.0)),
    ]
    await _override(app, resolved, [], monkeypatch)
    async for client in _client(app):
        resp = await client.post(
            "/decks/price", json={"decklist": "1 Black Lotus\n1 Sol Ring"}
        )
    body = resp.json()
    assert body["missing_price"] == ["Black Lotus"]
    assert body["total_usd"] == 2.0  # NOT 2.0 + 0; Lotus excluded from sum


async def test_price_qty_multiplies_line(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = [
        ResolvedDeckRow(input_name="Bolt", row=_row("Lightning Bolt", price_usd=1.5)),
    ]
    await _override(app, resolved, [], monkeypatch)
    async for client in _client(app):
        resp = await client.post("/decks/price", json={"decklist": "4 Bolt"})
    body = resp.json()
    assert body["total_usd"] == 6.0
    assert body["top"][0]["qty"] == 4
    assert body["top"][0]["unit_usd"] == 1.5
    assert body["top"][0]["line_usd"] == 6.0


async def test_price_by_cmc_bucketing(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = [
        ResolvedDeckRow(input_name="Bolt", row=_row("Bolt", price_usd=1.0, cmc=1)),
        ResolvedDeckRow(input_name="Cultivate", row=_row("Cultivate", price_usd=2.0, cmc=3)),
        ResolvedDeckRow(input_name="Craterhoof", row=_row("Craterhoof", price_usd=25.0, cmc=8)),
    ]
    await _override(app, resolved, [], monkeypatch)
    async for client in _client(app):
        resp = await client.post("/decks/price", json={
            "decklist": "1 Bolt\n1 Cultivate\n1 Craterhoof"
        })
    body = resp.json()
    assert body["by_cmc"] == {"0-1": 1.0, "2-3": 2.0, "4+": 25.0}


async def test_price_by_slot_uses_first_predicted_label(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = [
        ResolvedDeckRow(
            input_name="Cultivate",
            row=_row("Cultivate", price_usd=2.0, labels=["ramp"]),
        ),
        ResolvedDeckRow(
            input_name="Sol Ring",
            row=_row("Sol Ring", price_usd=2.0, labels=["ramp"]),
        ),
        ResolvedDeckRow(
            input_name="Rest in Peace", row=_row("Rest in Peace", price_usd=3.0, labels=[]),
        ),
    ]
    await _override(app, resolved, [], monkeypatch)
    async for client in _client(app):
        resp = await client.post("/decks/price", json={
            "decklist": "1 Cultivate\n1 Sol Ring\n1 Rest in Peace"
        })
    body = resp.json()
    assert body["by_slot"]["ramp"] == 4.0
    assert body["by_slot"]["other"] == 3.0


async def test_price_top_sorted_desc(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = [
        ResolvedDeckRow(input_name="Cheap", row=_row("Cheap", price_usd=0.10)),
        ResolvedDeckRow(input_name="Expensive", row=_row("Expensive", price_usd=100.0)),
        ResolvedDeckRow(input_name="Mid", row=_row("Mid", price_usd=5.0)),
    ]
    await _override(app, resolved, [], monkeypatch)
    async for client in _client(app):
        resp = await client.post("/decks/price", json={
            "decklist": "1 Cheap\n1 Expensive\n1 Mid"
        })
    body = resp.json()
    assert [t["name"] for t in body["top"]] == ["Expensive", "Mid", "Cheap"]


async def test_price_empty_deck_after_resolve_is_ok(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    await _override(app, [], ["Made Up"], monkeypatch)
    async for client in _client(app):
        resp = await client.post("/decks/price", json={"decklist": "1 Made Up"})
    body = resp.json()
    assert body["total_usd"] == 0.0
    assert body["missing_price"] == []
    assert body["unresolved"] == ["Made Up"]


async def test_price_empty_body_rejected(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/price", json={"decklist": ""})
    assert resp.status_code == 422


async def test_price_oversize_rejected(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/price", json={
            "decklist": "1 X\n" * (DECKLIST_MAX_LEN // 3)
        })
    assert resp.status_code == 422
