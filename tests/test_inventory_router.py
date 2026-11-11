"""Unit tests for /inventory HTTP API endpoints (D18.3)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
async def test_sync_deckbox_inventory_with_csv_text(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        # SELECT cards lower(name) match
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [
            {"oracle_id": "11111111-1111-1111-1111-111111111111", "name": "Sol Ring", "lower_name": "sol ring"}
        ])),
        # DELETE FROM inventory
        AsyncMock(),
        # INSERT INTO inventory
        AsyncMock(),
        # INSERT/UPDATE inventory_sync_settings
        AsyncMock(),
    )

    app.state.read_engine = _make_mock_engine(fake_conn)

    csv_data = "Count,Name,Edition\n2,Sol Ring,Commander 2021\n"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/inventory/sync/deckbox",
            json={"csv_text": csv_data, "mode": "replace"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "inventory_sync"
    assert data["mode"] == "replace"
    assert data["total_cards"] == 2
    assert data["distinct_cards"] == 1
    assert data["unmatched"] == []


@pytest.mark.asyncio
async def test_sync_deckbox_inventory_with_url(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [
            {"oracle_id": "11111111-1111-1111-1111-111111111111", "name": "Sol Ring", "lower_name": "sol ring"}
        ])),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )

    app.state.read_engine = _make_mock_engine(fake_conn)

    csv_data = "Count,Name,Edition\n1,Sol Ring,Alpha\n"

    with patch("biblioplex.routers.inventory.fetch_deckbox_csv", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = ("https://deckbox.org/sets/3293466/export?format=csv", csv_data)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/inventory/sync/deckbox",
                json={"url": "https://deckbox.org/sets/3293466"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_cards"] == 1
    mock_fetch.assert_called_once_with("https://deckbox.org/sets/3293466")


@pytest.mark.asyncio
async def test_get_inventory_summary_endpoint(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(one=lambda: {
            "total_cards": 10, "distinct_cards": 5, "total_value_usd": 42.50
        })),
        AsyncMock(mappings=lambda: AsyncMock(one_or_none=lambda: {
            "deckbox_url": "https://deckbox.org/sets/3293466/export?format=csv",
            "last_synced_at": None,
        })),
    )

    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/inventory/summary")

    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "inventory_summary"
    assert data["total_cards"] == 10
    assert data["distinct_cards"] == 5
    assert data["total_value_usd"] == 42.50


@pytest.mark.asyncio
async def test_sync_uses_write_transaction(app: FastAPI) -> None:
    """Regression: `engine.connect()` rolls back on close — the whole sync
    would be silently discarded. Writes must go through `engine.begin()`."""
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [])),  # exact match
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [])),  # squash fallback
        AsyncMock(),  # DELETE
        AsyncMock(),  # settings upsert
    )
    engine = _make_mock_engine(fake_conn)
    app.state.read_engine = engine

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/inventory/sync/deckbox",
            json={"csv_text": "Count,Name\n1,Sol Ring\n"},
        )

    assert resp.status_code == 200
    engine.begin.assert_called_once()
    engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_clear_inventory_uses_write_transaction(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    engine = _make_mock_engine(fake_conn)
    app.state.read_engine = engine

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/inventory")

    assert resp.status_code == 200
    assert resp.json()["object"] == "inventory_cleared"
    engine.begin.assert_called_once()
    engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_sync_without_params_and_no_saved_url_is_422(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(one_or_none=lambda: None)),
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/inventory/sync/deckbox", json={})

    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"


@pytest.mark.asyncio
async def test_upsert_inventory_card(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(one_or_none=lambda: {"name": "Sol Ring"})),
        AsyncMock(),
        AsyncMock(),
    )
    engine = _make_mock_engine(fake_conn)
    app.state.read_engine = engine

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/inventory/cards",
            json={"oracle_id": "11111111-1111-1111-1111-111111111111", "quantity": 3},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "inventory_card"
    assert body["card_name"] == "Sol Ring"
    assert body["quantity"] == 3
    engine.begin.assert_called_once()


@pytest.mark.asyncio
async def test_upsert_inventory_card_unknown_oracle_is_404(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(one_or_none=lambda: None)),
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/inventory/cards",
            json={"oracle_id": "11111111-1111-1111-1111-111111111111", "quantity": 1},
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_list_inventory_cards_endpoint(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(one=lambda: {"total": 1})),
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [
            {
                "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "oracle_id": "11111111-1111-1111-1111-111111111111",
                "card_name": "Sol Ring",
                "quantity": 2,
                "foil_quantity": 0,
                "set_name": "Commander 2021",
                "collector_number": "263",
                "price_usd": "1.50",
                "type_line": "Artifact",
                "raw": {"name": "Sol Ring"},
            }
        ])),
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/inventory?q=sol&page=1&limit=50")

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["total_cards"] == 1
    assert body["data"][0]["card_name"] == "Sol Ring"
    assert body["has_more"] is False


@pytest.mark.asyncio
async def test_list_owned_oracle_ids(app: FastAPI) -> None:
    """Flat id list backs the web UI's owned badges — one request per page
    of search results instead of one per card."""
    fake_conn = AsyncMock()
    fake_conn.execute.return_value = AsyncMock(all=lambda: [
        MagicMock(oracle_id="11111111-1111-1111-1111-111111111111"),
        MagicMock(oracle_id="22222222-2222-2222-2222-222222222222"),
    ])
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/inventory/oracle-ids")

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"] == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    sql = " ".join(str(fake_conn.execute.call_args.args[0]).split())
    assert "DISTINCT oracle_id" in sql
    assert "quantity > 0" in sql


@pytest.mark.asyncio
async def test_html_login_page_does_not_wipe_inventory(app: FastAPI) -> None:
    """Regression: an anonymous Deckbox /export fetch returns a 200 HTML login
    page. It must 422 without issuing a single statement — a `mode: replace`
    on that body would DELETE the whole inventory and report success."""
    from biblioplex.inventory.deckbox import DeckboxFetchError

    fake_conn = AsyncMock()
    app.state.read_engine = _make_mock_engine(fake_conn)

    with patch(
        "biblioplex.routers.inventory.fetch_deckbox_csv", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.side_effect = DeckboxFetchError("returned HTML, not CSV")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/inventory/sync/deckbox",
                json={"url": "https://deckbox.org/sets/3293466", "mode": "replace"},
            )

    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"
    fake_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_unparseable_csv_does_not_wipe_inventory(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/inventory/sync/deckbox",
            json={"csv_text": "Foo,Bar\n1,2\n", "mode": "replace"},
        )

    assert resp.status_code == 422
    assert "not a usable deckbox csv" in resp.json()["details"].lower()
    fake_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_deckbox_network_failure_is_502(app: FastAPI) -> None:
    import httpx as _httpx

    fake_conn = AsyncMock()
    app.state.read_engine = _make_mock_engine(fake_conn)

    with patch(
        "biblioplex.routers.inventory.fetch_deckbox_csv", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.side_effect = _httpx.ConnectTimeout("timed out")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/inventory/sync/deckbox", json={"url": "3293466"}
            )

    assert resp.status_code == 502
    assert resp.json()["code"] == "bad_gateway"
    fake_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_upload_deckbox_csv_file(app: FastAPI) -> None:
    """Multipart upload is the automation path now that Deckbox's URL export
    requires a logged-in session (Bug.32)."""
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [
            {"oracle_id": "11111111-1111-1111-1111-111111111111", "name": "Sol Ring", "lower_name": "sol ring"}
        ])),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )
    engine = _make_mock_engine(fake_conn)
    app.state.read_engine = engine

    csv_bytes = b"Count,Name,Edition\n2,Sol Ring,Commander 2021\n"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/inventory/sync/upload",
            files={"file": ("collection.csv", csv_bytes, "text/csv")},
            data={"mode": "replace"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "inventory_sync"
    assert body["total_cards"] == 2
    assert body["mode"] == "replace"
    engine.begin.assert_called_once()
    engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_upload_defaults_to_replace_mode(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [])),  # exact match
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [])),  # squash fallback
        AsyncMock(),  # DELETE
        AsyncMock(),  # settings upsert
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/inventory/sync/upload",
            files={"file": ("c.csv", b"Count,Name\n1,Sol Ring\n", "text/csv")},
        )

    assert resp.status_code == 200
    assert resp.json()["mode"] == "replace"


@pytest.mark.asyncio
async def test_upload_of_unparseable_file_does_not_wipe_inventory(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/inventory/sync/upload",
            files={"file": ("login.html", b"<!DOCTYPE html>\n<html></html>", "text/html")},
            data={"mode": "replace"},
        )

    assert resp.status_code == 422
    assert "not a usable deckbox csv" in resp.json()["details"].lower()
    fake_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_upload_tolerates_non_utf8_bytes(app: FastAPI) -> None:
    """One stray byte must not 500 a 3000-line collection."""
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [])),  # exact match
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [])),  # squash fallback
        AsyncMock(),  # DELETE
        AsyncMock(),  # settings upsert
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/inventory/sync/upload",
            files={"file": ("c.csv", b"Count,Name\n1,Jac\xffo\n", "text/csv")},
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_sync_records_the_upload_in_history(app: FastAPI) -> None:
    """D18.17 — the import is recorded in the SAME transaction as the sync,
    so history can never show a file whose import was rolled back."""
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [])),
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [])),
        AsyncMock(),
        AsyncMock(),
    )
    engine = _make_mock_engine(fake_conn)
    app.state.read_engine = engine

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/inventory/sync/upload",
            files={"file": ("MyExport.csv", b"Count,Name\n1,Sol Ring\n", "text/csv")},
        )

    assert resp.status_code == 200
    inserts = [
        c for c in fake_conn.execute.call_args_list
        if "INSERT INTO uploads" in " ".join(str(c.args[0]).split())
    ]
    assert len(inserts) == 1
    params = inserts[0].args[1]
    assert params["kind"] == "inventory"
    assert params["filename"] == "MyExport.csv"
    assert params["content"] == b"Count,Name\n1,Sol Ring\n"
    assert params["mode"] == "replace"
    engine.begin.assert_called_once()


@pytest.mark.asyncio
async def test_rejected_csv_is_not_recorded_in_history(app: FastAPI) -> None:
    """A 422 must leave no trace — the file was never applied."""
    fake_conn = AsyncMock()
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/inventory/sync/upload",
            files={"file": ("login.html", b"<!DOCTYPE html>\n<html></html>", "text/html")},
        )

    assert resp.status_code == 422
    fake_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_pasted_csv_records_a_synthetic_filename(app: FastAPI) -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = _responses(
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [])),
        AsyncMock(mappings=lambda: AsyncMock(all=lambda: [])),
        AsyncMock(),
        AsyncMock(),
    )
    app.state.read_engine = _make_mock_engine(fake_conn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/inventory/sync/deckbox",
            json={"csv_text": "Count,Name\n1,Sol Ring\n"},
        )

    assert resp.status_code == 200
    inserts = [
        c for c in fake_conn.execute.call_args_list
        if "INSERT INTO uploads" in " ".join(str(c.args[0]).split())
    ]
    assert inserts[0].args[1]["filename"] == "pasted.csv"
