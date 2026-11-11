"""Unit tests for /decks/analyze inventory integration (D18.5)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app


def _placeholder_settings() -> Settings:
    return Settings.model_construct(
        database_url="postgresql+psycopg://placeholder/placeholder",
        hf_embed_model="sentence-transformers/all-MiniLM-L6-v2",
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
    return create_app(settings=_placeholder_settings())


def _make_mock_engine(conn: AsyncMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = ctx
    # Write endpoints go through `engine.begin()` — `connect()` would open a
    # transaction that rolls back on close, silently discarding the sync.
    engine.begin.return_value = ctx
    return engine


@pytest.mark.asyncio
async def test_deck_analyze_includes_inventory_coverage(app: FastAPI) -> None:
    fake_conn = AsyncMock()

    # resolve_and_fetch_deck_rows query output
    # SimpleNamespace, not MagicMock: `MagicMock(name=...)` sets the mock's
    # own repr name, so `row.name` would come back as a child mock.
    mock_row_sol_ring = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        oracle_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        name="Sol Ring",
        input_name="Sol Ring",
        type_line="Artifact",
        layout="normal",
        card_faces=None,
        predicted_labels=["ramp"],
        cmc=1.0,
        mana_cost="{1}",
        color_identity=[],
        legalities={"commander": "legal"},
        has_unlimited_copies=False,
        price_usd="1.50",
        similarity=None,
    )
    mock_row_command_tower = SimpleNamespace(
        id="22222222-2222-2222-2222-222222222222",
        oracle_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        name="Command Tower",
        input_name="Command Tower",
        type_line="Land",
        layout="normal",
        card_faces=None,
        predicted_labels=[],
        cmc=0.0,
        mana_cost="",
        color_identity=[],
        legalities={"commander": "legal"},
        has_unlimited_copies=False,
        price_usd="0.50",
        similarity=None,
    )

    fake_conn.execute.side_effect = [
        # 1. resolve_and_fetch_deck_rows query
        AsyncMock(all=lambda: [mock_row_sol_ring, mock_row_command_tower]),
        # 2. SELECT FROM inventory for deck's oracle_ids query
        AsyncMock(all=lambda: [
            # Sol Ring owned: 1 (required: 1 -> owned: 1)
            # Command Tower owned: 0 (required: 2 -> missing: 2)
            SimpleNamespace(oracle_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", owned=1),
        ]),
        # 3. SELECT DISTINCT FROM wishlist for deck's oracle_ids (D18.10)
        AsyncMock(all=lambda: []),
    ]

    app.state.read_engine = _make_mock_engine(fake_conn)

    decklist_text = "1 Sol Ring\n2 Command Tower\n"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/decks/analyze", json={"decklist": decklist_text})

    assert resp.status_code == 200
    data = resp.json()
    assert "inventory_summary" in data
    assert "missing_cards" in data

    summary = data["inventory_summary"]
    assert summary["total_cards_in_deck"] == 3
    assert summary["owned_cards_in_deck"] == 1
    assert summary["total_missing_count"] == 2
    assert summary["coverage_pct"] == 33.33
    assert summary["total_distinct_cards"] == 2
    assert summary["owned_distinct_cards"] == 1

    missing = data["missing_cards"]
    assert len(missing) == 1
    assert missing[0]["name"] == "Command Tower"
    assert missing[0]["qty_required"] == 2
    assert missing[0]["qty_owned"] == 0
    assert missing[0]["qty_missing"] == 2


@pytest.mark.asyncio
async def test_inventory_lookup_sums_across_printings(app: FastAPI) -> None:
    """`inventory` holds one row per printing — a row-per-oracle read would
    keep only the last edition's quantity."""
    fake_conn = AsyncMock()
    row = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        oracle_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        name="Sol Ring",
        input_name="Sol Ring",
        type_line="Artifact",
        layout="normal",
        card_faces=None,
        predicted_labels=["ramp"],
        cmc=1.0,
        mana_cost="{1}",
        color_identity=[],
        legalities={"commander": "legal"},
        has_unlimited_copies=False,
        price_usd="1.50",
        similarity=None,
    )
    fake_conn.execute.side_effect = [
        AsyncMock(all=lambda: [row]),
        AsyncMock(all=lambda: [
            SimpleNamespace(oracle_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", owned=2),
        ]),
        AsyncMock(all=lambda: []),
    ]
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/decks/analyze", json={"decklist": "1 Sol Ring\n"})

    assert resp.status_code == 200
    inv_sql = " ".join(str(fake_conn.execute.call_args_list[1].args[0]).split())
    assert "SUM(quantity)" in inv_sql
    assert "GROUP BY oracle_id" in inv_sql
    assert resp.json()["missing_cards"] == []


@pytest.mark.asyncio
async def test_deck_with_no_resolved_rows_skips_inventory_query(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = [AsyncMock(all=lambda: [])]
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/decks/analyze", json={"decklist": "1 Nonexistent Card\n"})

    assert resp.status_code == 200
    assert fake_conn.execute.call_count == 1
    assert resp.json()["inventory_summary"]["coverage_pct"] == 100.0


@pytest.mark.asyncio
async def test_missing_cards_flag_wishlist_membership(app: FastAPI) -> None:
    """D18.10 — of what the deck still needs, which is already flagged to buy."""
    fake_conn = AsyncMock()
    row = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        oracle_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        name="Mana Crypt",
        input_name="Mana Crypt",
        type_line="Artifact",
        layout="normal",
        card_faces=None,
        predicted_labels=["ramp"],
        cmc=0.0,
        mana_cost="{0}",
        color_identity=[],
        legalities={"commander": "legal"},
        has_unlimited_copies=False,
        price_usd="120.00",
        similarity=None,
    )
    fake_conn.execute.side_effect = [
        AsyncMock(all=lambda: [row]),
        AsyncMock(all=lambda: []),  # own nothing
        AsyncMock(all=lambda: [
            SimpleNamespace(oracle_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        ]),
    ]
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/decks/analyze", json={"decklist": "1 Mana Crypt\n"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["missing_cards"][0]["on_wishlist"] is True
    assert data["inventory_summary"]["missing_on_wishlist"] == 1
    wl_sql = " ".join(str(fake_conn.execute.call_args_list[2].args[0]).split())
    assert "wishlist" in wl_sql
