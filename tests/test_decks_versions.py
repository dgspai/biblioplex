"""D19.5 — deck version history: list, fetch, download, restore.

D19.32 removed deletion; history is append-only by construction.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.decks import service as deck_service
from biblioplex.main import create_app

UUID_A = "11111111-1111-1111-1111-111111111111"


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


def _mock_engine() -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
    ctx.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = ctx
    engine.begin.return_value = ctx
    return engine


@pytest.fixture
def app() -> FastAPI:
    a = create_app(settings=_placeholder_settings())
    a.state.read_engine = _mock_engine()
    return a


def _svc(name: str, **kw: Any) -> Any:
    return patch(f"biblioplex.routers.deck_library.deck_service.{name}", **kw)


def _result(rows: list[dict[str, Any]] | None = None, rowcount: int = 0) -> MagicMock:
    rows = rows or []
    mappings = MagicMock()
    mappings.all.return_value = rows
    mappings.one.return_value = rows[0] if rows else None
    mappings.one_or_none.return_value = rows[0] if rows else None
    res = MagicMock()
    res.mappings.return_value = mappings
    res.rowcount = rowcount
    return res


def _conn(*results: MagicMock) -> AsyncMock:
    conn = AsyncMock()
    it = iter(results)

    def _next(*_a: Any, **_k: Any) -> Any:
        try:
            return next(it)
        except StopIteration:
            return _result()

    conn.execute.side_effect = _next
    return conn


def _sql(conn: AsyncMock, index: int) -> str:
    return str(conn.execute.await_args_list[index].args[0])


def _summary(version: int = 1, branch: str = "main", message: str | None = None) -> dict[str, Any]:
    return {
        "id": "22222222-2222-2222-2222-222222222222",
        "version": version,
        "branch": branch,
        "message": message,
        "source": "upload",
        "filename": "deck.csv",
        "size_bytes": 42,
        "total_cards": 100,
        "distinct_cards": 99,
        "unresolved_count": 0,
        "created_at": MagicMock(isoformat=lambda: "2026-08-18T00:00:00+00:00"),
    }


# ---- service: listing ----


@pytest.mark.asyncio
async def test_version_listing_never_selects_the_bodies() -> None:
    """Browsing history must not drag every stored file through the DB."""
    conn = _conn(_result([_summary(2), _summary(1)]))
    await deck_service.list_versions(conn, UUID_A)
    sql = _sql(conn, 0)
    assert "decklist" not in sql
    assert "raw_content" not in sql
    assert "ORDER BY version DESC" in sql


@pytest.mark.asyncio
async def test_version_listing_uses_cast_not_double_colon() -> None:
    conn = _conn(_result([_summary(1)]))
    await deck_service.list_versions(conn, UUID_A)
    assert "::" not in _sql(conn, 0)
    assert "CAST(" in _sql(conn, 0)


@pytest.mark.asyncio
async def test_version_listing_returns_newest_first() -> None:
    conn = _conn(_result([_summary(2), _summary(1)]))
    out = await deck_service.list_versions(conn, UUID_A)
    assert [v["version"] for v in out] == [2, 1]


# ---- service: restore ----


@pytest.mark.asyncio
async def test_restore_appends_a_new_version_rather_than_rewinding() -> None:
    """History is append-only — restoring must never truncate."""
    old = {**_summary(1), "decklist": "1 Sol Ring\n"}
    latest = {**_summary(3), "decklist": "9 Island\n"}
    new = {**_summary(4), "decklist": "1 Sol Ring\n"}
    conn = _conn(
        _result([old]),  # restore's own SELECT decklist, branch
        _result([{"head_version": 3, "head_pinned": False, "trunk_branch": None}]),  # head fetch
        _result([latest]),  # branch dedup lookup (differs)
        _result([{"max_version": 3}]),  # next version number
        _result([new]),  # INSERT
    )
    with patch.object(deck_service, "resolve_names", new=AsyncMock(return_value=MagicMock(unresolved=[]))):
        result = await deck_service.restore_version(conn, UUID_A, 1)
    assert result is not None
    assert result.created is True
    assert result.version["version"] == 4
    params = conn.execute.await_args_list[4].args[1]
    assert params["source"] == "restore"
    assert params["decklist"] == "1 Sol Ring\n"
    assert params["branch"] == "main"


@pytest.mark.asyncio
async def test_restoring_the_current_version_is_a_noop() -> None:
    """Same text as the latest — dedup applies here exactly as on any write."""
    latest = {**_summary(3), "decklist": "1 Sol Ring\n"}
    conn = _conn(
        _result([latest]),  # restore's own SELECT decklist, branch
        _result([{"head_version": 3, "head_pinned": False, "trunk_branch": None}]),  # head fetch
        _result([latest]),  # branch dedup lookup — identical, dedup fires
    )
    result = await deck_service.restore_version(conn, UUID_A, 3)
    assert result is not None
    assert result.created is False
    assert result.version["version"] == 3


@pytest.mark.asyncio
async def test_restore_returns_none_for_an_unknown_version() -> None:
    conn = _conn(_result([]))
    assert await deck_service.restore_version(conn, UUID_A, 99) is None


# ---- routes ----


@pytest.mark.asyncio
async def test_list_versions_route_404s_when_the_deck_has_none(app: FastAPI) -> None:
    """A real deck always has at least version 1, so empty means unknown deck."""
    with _svc("list_versions", new=AsyncMock(return_value=[])):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{UUID_A}/versions")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_versions_route_uses_a_read_connection(app: FastAPI) -> None:
    with _svc("list_versions", new=AsyncMock(return_value=[_summary(1)])):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{UUID_A}/versions")
    assert resp.status_code == 200
    assert resp.json()["object"] == "list"
    app.state.read_engine.connect.assert_called_once()
    app.state.read_engine.begin.assert_not_called()


@pytest.mark.asyncio
async def test_get_version_route_returns_the_decklist_text(app: FastAPI) -> None:
    row = {**_summary(2), "decklist": "1 Sol Ring\n"}
    with _svc("get_version", new=AsyncMock(return_value=row)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{UUID_A}/versions/2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "deck_version"
    assert body["decklist"] == "1 Sol Ring\n"


@pytest.mark.asyncio
async def test_get_version_route_404s_on_an_unknown_version(app: FastAPI) -> None:
    with _svc("get_version", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{UUID_A}/versions/9")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_returns_the_original_bytes(app: FastAPI) -> None:
    raw = b"Count,Name\n1,Sol Ring\n"
    blob = {"raw_content": raw, "filename": "Iroh gift.csv", "decklist": "1 Sol Ring\n", "version": 2}
    with _svc("get_version_blob", new=AsyncMock(return_value=blob)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{UUID_A}/versions/2/download")
    assert resp.status_code == 200
    assert resp.content == raw
    assert 'filename="Iroh gift.csv"' in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_falls_back_to_the_canonical_text(app: FastAPI) -> None:
    """Paste / edit / restore versions have no file — synthesise a .txt."""
    blob = {"raw_content": None, "filename": None, "decklist": "1 Sol Ring\n", "version": 3}
    with _svc("get_version_blob", new=AsyncMock(return_value=blob)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{UUID_A}/versions/3/download?name=Iroh%20gift")
    assert resp.content == b"1 Sol Ring\n"
    assert "Iroh gift-v3.txt" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_filename_cannot_inject_a_header(app: FastAPI) -> None:
    """A stored filename is user input; CR/LF or a quote in it must not escape."""
    blob = {
        "raw_content": b"x",
        "filename": 'evil"\r\nX-Injected: 1.csv',
        "decklist": "",
        "version": 1,
    }
    with _svc("get_version_blob", new=AsyncMock(return_value=blob)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{UUID_A}/versions/1/download")
    assert "x-injected" not in {k.lower() for k in resp.headers}
    disposition = resp.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition


@pytest.mark.asyncio
async def test_download_404s_on_an_unknown_version(app: FastAPI) -> None:
    with _svc("get_version_blob", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{UUID_A}/versions/9/download")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_restore_route_uses_a_write_transaction(app: FastAPI) -> None:
    row = {**_summary(4), "decklist": "1 Sol Ring\n"}
    result = deck_service.AppendResult(
        version=row, created=True, head_version=4, head_pinned=False,
        diff_bases={"main": 3, "branch": 3},
    )
    with _svc("restore_version", new=AsyncMock(return_value=result)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(f"/decks/{UUID_A}/versions/1/restore")
    assert resp.status_code == 200
    assert resp.json()["version"] == 4
    assert resp.json()["version_created"] is True
    app.state.read_engine.begin.assert_called_once()
    app.state.read_engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_restore_route_404s_on_an_unknown_version(app: FastAPI) -> None:
    with _svc("restore_version", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(f"/decks/{UUID_A}/versions/9/restore")
    assert resp.status_code == 404


def test_no_delete_route_matches_versions() -> None:
    """D19.32 — version history is append-only by construction.

    Deletion is refused at the router level, not by a service-layer rule:
    there is no route left that could remove a `deck_versions` row, so the
    surface cannot be reintroduced by accident. `deck_library.router.routes`
    is inspected directly because `app.routes` wraps included routers in
    `_IncludedRouter` objects that hide the leaf `path` (see
    `test_decks_folders.py`).
    """
    from biblioplex.routers import deck_library

    delete_paths = [
        r.path  # type: ignore[attr-defined]
        for r in deck_library.router.routes
        if hasattr(r, "path") and "DELETE" in getattr(r, "methods", set())
    ]
    assert not any("/versions/" in p for p in delete_paths)


@pytest.mark.asyncio
async def test_version_routes_reject_a_malformed_deck_id(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/decks/not-a-uuid/versions")
    assert resp.status_code == 404
    app.state.read_engine.connect.assert_not_called()


def test_version_tools_are_registered() -> None:
    from fastapi_mcp import FastApiMCP

    mcp = FastApiMCP(create_app(settings=_placeholder_settings()))
    names = {t.name for t in mcp.tools}
    assert {
        "list_deck_versions",
        "get_deck_version",
        "download_deck_version",
        "restore_deck_version",
    } <= names
