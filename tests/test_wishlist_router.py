"""Unit tests for /wishlist HTTP API endpoints (D18.8)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app

_SOL = "11111111-1111-1111-1111-111111111111"


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
    engine.begin.return_value = ctx
    return engine


def _responses(*canned: Any) -> Any:
    """Canned responses in order, then a generic mock for anything after.

    Sync endpoints keep gaining bookkeeping queries (the D18.17 upload
    record was the third), and a bare `side_effect` list raises
    StopAsyncIteration the moment one is added. Ordering assertions still
    hold — only the tail is open-ended.
    """
    it = iter(canned)

    def _next(*_a: Any, **_k: Any) -> Any:
        try:
            return next(it)
        except StopIteration:
            return AsyncMock()

    return _next



@pytest.mark.asyncio
async def test_sync_wishlist_from_csv_text(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [
            {"oracle_id": _SOL, "name": "Mana Crypt", "lower_name": "mana crypt"}
        ])),
        AsyncMock(),  # DELETE
        AsyncMock(),  # INSERT
    )
    engine = _make_mock_engine(fake_conn)
    app.state.read_engine = engine

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/wishlist/sync",
            json={"csv_text": "Count,Name,Edition\n2,Mana Crypt,Eternal Masters\n"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "wishlist_sync"
    assert body["total_cards"] == 2
    assert body["distinct_cards"] == 1
    assert body["unmatched"] == []
    engine.begin.assert_called_once()
    engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_upload_wishlist_csv_file(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [
            {"oracle_id": _SOL, "name": "Mana Crypt", "lower_name": "mana crypt"}
        ])),
        AsyncMock(),
        AsyncMock(),
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/wishlist/sync/upload",
            files={"file": ("wishlist.csv", b"Count,Name\n1,Mana Crypt\n", "text/csv")},
            data={"mode": "replace"},
        )

    assert resp.status_code == 200
    assert resp.json()["total_cards"] == 1


@pytest.mark.asyncio
async def test_unparseable_upload_does_not_wipe_wishlist(app: FastAPI) -> None:
    """Same Bug.32 shape as the inventory sync: a `mode: replace` on an
    unparseable body would delete everything and report success."""
    fake_conn = AsyncMock()
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/wishlist/sync/upload",
            files={"file": ("login.html", b"<!DOCTYPE html>\n<html></html>", "text/html")},
            data={"mode": "replace"},
        )

    assert resp.status_code == 422
    assert "not a usable deckbox csv" in resp.json()["details"].lower()
    fake_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_wishlist_summary(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.return_value = AsyncMock(
        mappings=lambda: AsyncMock(one=lambda: {
            "total_cards": 12,
            "distinct_cards": 9,
            "total_value_usd": 130.25,
            "last_synced_at": None,
            "owned_distinct_cards": 2,
        })
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/wishlist/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "wishlist_summary"
    assert body["total_cards"] == 12
    assert body["owned_distinct_cards"] == 2
    assert body["last_synced_at"] is None


@pytest.mark.asyncio
async def test_list_wishlist_reports_owned_quantity(app: FastAPI) -> None:
    """`owned_quantity` lets the UI strike through cards already acquired."""
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(one=lambda: {"total": 1})),
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [
            {
                "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "oracle_id": _SOL,
                "card_name": "Mana Crypt",
                "quantity": 1,
                "owned_quantity": 1,
                "set_name": "Eternal Masters",
                "price_usd": "120.00",
                "type_line": "Artifact",
                "raw": {"name": "Mana Crypt"},
            }
        ])),
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/wishlist?q=crypt")

    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["card_name"] == "Mana Crypt"
    assert row["owned_quantity"] == 1


@pytest.mark.asyncio
async def test_wanted_oracle_ids(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.return_value = AsyncMock(all=lambda: [
        MagicMock(oracle_id=_SOL),
    ])
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/wishlist/oracle-ids")

    assert resp.status_code == 200
    assert resp.json()["data"] == [_SOL]
    sql = " ".join(str(fake_conn.execute.call_args.args[0]).split())
    assert "DISTINCT oracle_id" in sql
    assert "wishlist" in sql


@pytest.mark.asyncio
async def test_upsert_wishlist_card_and_404(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(one_or_none=lambda: {"name": "Mana Crypt"})),
        AsyncMock(),
        AsyncMock(),
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/wishlist/cards", json={"oracle_id": _SOL, "quantity": 2})
    assert resp.status_code == 200
    assert resp.json()["quantity"] == 2

    fake_conn2 = AsyncMock()
    fake_conn2.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(one_or_none=lambda: None)),
    )
    app.state.read_engine = _make_mock_engine(fake_conn2)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/wishlist/cards", json={"oracle_id": _SOL, "quantity": 2})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_clear_wishlist_uses_write_transaction(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    engine = _make_mock_engine(fake_conn)
    app.state.read_engine = engine

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/wishlist")

    assert resp.status_code == 200
    assert resp.json()["object"] == "wishlist_cleared"
    engine.begin.assert_called_once()
    engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_wishlist_sync_does_not_touch_inventory(app: FastAPI) -> None:
    """The whole reason wishlist is its own table: a wishlist sync must never
    write to, or delete from, `inventory`."""
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [
            {"oracle_id": _SOL, "name": "Mana Crypt", "lower_name": "mana crypt"}
        ])),
        AsyncMock(),
        AsyncMock(),
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/wishlist/sync", json={"csv_text": "Count,Name\n1,Mana Crypt\n"}
        )

    assert resp.status_code == 200
    statements = [
        " ".join(str(call.args[0]).split())
        for call in fake_conn.execute.call_args_list
    ]
    assert not any("inventory" in sql for sql in statements), statements
    assert any("wishlist" in sql for sql in statements)
