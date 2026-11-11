"""Unit tests for /uploads import history (D18.17)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app

_UID = "33333333-3333-3333-3333-333333333333"


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


@pytest.mark.asyncio
async def test_list_uploads_excludes_content(app: FastAPI) -> None:
    """Browsing history must not stream megabytes of CSV per row."""
    fake_conn = AsyncMock()
    fake_conn.execute.return_value = AsyncMock(
        mappings=lambda: AsyncMock(all=lambda: [
            {
                "id": _UID,
                "kind": "inventory",
                "filename": "Inventory_2026.csv",
                "size_bytes": 685000,
                "mode": "replace",
                "total_cards": 3416,
                "distinct_cards": 2300,
                "unmatched_count": 0,
                "uploaded_at": datetime(2026, 8, 17, tzinfo=UTC),
            }
        ])
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/uploads")

    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["filename"] == "Inventory_2026.csv"
    assert row["total_cards"] == 3416
    assert "content" not in row
    sql = " ".join(str(fake_conn.execute.call_args.args[0]).split())
    assert "content" not in sql


@pytest.mark.asyncio
async def test_download_returns_original_bytes(app: FastAPI) -> None:
    csv_bytes = b"Count,Name\n2,Sol Ring\n"
    fake_conn = AsyncMock()
    fake_conn.execute.return_value = AsyncMock(
        mappings=lambda: AsyncMock(one_or_none=lambda: {
            "id": _UID,
            "kind": "inventory",
            "filename": "Inventory_2026.csv",
            "content": csv_bytes,
            "uploaded_at": datetime(2026, 8, 17, tzinfo=UTC),
        })
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/uploads/{_UID}/download")

    assert resp.status_code == 200
    assert resp.content == csv_bytes
    assert resp.headers["content-type"].startswith("text/csv")
    assert 'filename="Inventory_2026.csv"' in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_unknown_id_is_404(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.return_value = AsyncMock(
        mappings=lambda: AsyncMock(one_or_none=lambda: None)
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/uploads/{_UID}/download")

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_delete_upload_uses_write_transaction(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.return_value = MagicMock(rowcount=1)
    engine = _make_mock_engine(fake_conn)
    app.state.read_engine = engine

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete(f"/uploads/{_UID}")

    assert resp.status_code == 200
    engine.begin.assert_called_once()
    engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_kind_filter_is_passed_through(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.return_value = AsyncMock(
        mappings=lambda: AsyncMock(all=lambda: [])
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/uploads?kind=wishlist")

    assert resp.status_code == 200
    sql = " ".join(str(fake_conn.execute.call_args.args[0]).split())
    assert "WHERE kind = :kind" in sql
    assert fake_conn.execute.call_args.args[1]["kind"] == "wishlist"


@pytest.mark.asyncio
async def test_invalid_kind_is_422(app: FastAPI) -> None:
    app.state.read_engine = _make_mock_engine(AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/uploads?kind=bogus")
    assert resp.status_code == 422
