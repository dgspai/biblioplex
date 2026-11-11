"""D14.13 — ``POST /decks/export`` canonicalising formatter (§6.14)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.decklist.format import format_decklist
from biblioplex.decklist.parser import DeckEntry
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


# ---- pure fn ----


def test_format_mtgo_shape() -> None:
    entries = [DeckEntry(qty=4, name="Lightning Bolt"), DeckEntry(qty=1, name="Sol Ring")]
    out = format_decklist(entries, style="mtgo")
    assert out == "4 Lightning Bolt\n1 Sol Ring"


def test_format_arena_without_hints_falls_back_to_qty_name() -> None:
    entries = [DeckEntry(qty=1, name="Sol Ring")]
    out = format_decklist(entries, style="arena")
    assert out == "1 Sol Ring"


def test_format_arena_with_printing_hints_emits_set_and_collector() -> None:
    entries = [{"qty": 1, "name": "Sol Ring", "set": "c21", "collector_number": "263"}]
    out = format_decklist(entries, style="arena")
    assert out == "1 Sol Ring (C21) 263"


def test_format_txt_with_sideboard_prepends_divider() -> None:
    main = [DeckEntry(qty=1, name="Sol Ring")]
    side = [DeckEntry(qty=1, name="Rest in Peace")]
    out = format_decklist(main, style="txt", sideboard=side)
    assert out == "1 Sol Ring\n\nSideboard:\n1 Rest in Peace"


def test_format_unsupported_style_raises() -> None:
    import pytest as _pt
    with _pt.raises(ValueError, match="unsupported style"):
        format_decklist([], style="xml")  # type: ignore[arg-type]


def test_roundtrip_through_parser_is_stable() -> None:
    # Canonical roundtrip: parse → format → parse ⇒ same DeckEntry list.
    from biblioplex.decklist.parser import parse_decklist

    raw = "4x Lightning Bolt (C21) 100\n1 Sol Ring *F*\n#comment\n\n2 Cultivate"
    parsed = parse_decklist(raw)
    canonical = format_decklist(parsed, style="mtgo")
    reparsed = parse_decklist(canonical)
    assert parsed == reparsed


# ---- endpoint ----


async def test_export_mtgo_canonicalises_annotations(app: FastAPI) -> None:
    async for client in _client(app):
        resp = await client.post(
            "/decks/export?style=mtgo",
            json={"decklist": "4x Lightning Bolt (C21) 100\n1 Sol Ring *F*"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "deck_export"
    assert body["style"] == "mtgo"
    assert body["decklist"] == "4 Lightning Bolt\n1 Sol Ring"
    assert body["line_count"] == 2
    assert body["total_qty"] == 5


async def test_export_arena_style_accepted(app: FastAPI) -> None:
    async for client in _client(app):
        resp = await client.post(
            "/decks/export?style=arena", json={"decklist": "1 Sol Ring"}
        )
    body = resp.json()
    assert body["style"] == "arena"
    assert body["decklist"] == "1 Sol Ring"


async def test_export_default_style_mtgo(app: FastAPI) -> None:
    # Missing `style` param defaults to mtgo per the Query default.
    async for client in _client(app):
        resp = await client.post("/decks/export", json={"decklist": "1 Sol Ring"})
    assert resp.status_code == 200
    assert resp.json()["style"] == "mtgo"


async def test_export_empty_decklist_rejected(app: FastAPI) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/export", json={"decklist": ""})
    assert resp.status_code == 422
    assert "required" in resp.json()["details"]


async def test_export_unsupported_style_rejected(app: FastAPI) -> None:
    async for client in _client(app):
        resp = await client.post(
            "/decks/export?style=xml", json={"decklist": "1 Sol Ring"}
        )
    assert resp.status_code == 422
    assert "unsupported style" in resp.json()["details"]


async def test_export_oversize_rejected(app: FastAPI) -> None:
    async for client in _client(app):
        resp = await client.post(
            "/decks/export", json={"decklist": "1 X" + "\n1 Y" * (DECKLIST_MAX_LEN // 3)}
        )
    assert resp.status_code == 422
    assert "too long" in resp.json()["details"]
