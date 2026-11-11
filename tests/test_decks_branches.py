"""D19.40 — `GET /decks/{id}/branches`, the local (no-fetch) sync strip.

Service-level tests exercise `deck_service.list_branches` against a mocked
connection with real fold/diff logic; router tests pin the HTTP wiring
(404s, read-not-write transaction) the way `test_decks_library_router.py`
already does for sibling routes.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.decks import service as deck_service
from biblioplex.main import create_app

DECK_ID = "11111111-1111-1111-1111-111111111111"
NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def _result(rows: list[dict[str, Any]] | None = None) -> MagicMock:
    rows = rows or []
    mappings = MagicMock()
    mappings.all.return_value = rows
    mappings.one.return_value = rows[0] if rows else None
    mappings.one_or_none.return_value = rows[0] if rows else None
    res = MagicMock()
    res.mappings.return_value = mappings
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


def _tip(branch: str, version: int, decklist: str) -> dict[str, Any]:
    return {"branch": branch, "version": version, "created_at": NOW, "decklist": decklist}


# ---- service: the trap ----


@pytest.mark.asyncio
async def test_text_differs_but_deck_identical_reports_in_sync() -> None:
    """The whole point of the deliverable — reorder, add a comment, `4x`
    vs `4 `, and a tail annotation must all still report zero divergence."""
    head_text = "1 Sol Ring\n4 Lightning Bolt\n"
    branch_text = "// exported from moxfield\n4x Lightning Bolt (MH2) 290\n1 Sol Ring\n"
    conn = _conn(
        _result([{"head_version": None}]),
        _result([{**_tip("main", 1, head_text)}]),
        _result([_tip("main", 1, head_text), _tip("moxfield", 2, branch_text)]),
    )
    out = await deck_service.list_branches(conn, DECK_ID)
    assert out is not None
    by_branch = {r["branch"]: r for r in out["data"]}
    assert by_branch["moxfield"]["differs"] == {"added": 0, "removed": 0, "changed": 0}


@pytest.mark.asyncio
async def test_real_difference_reports_the_right_counts() -> None:
    head_text = "1 Sol Ring\n1 Lightning Bolt\n1 Forest\n"
    branch_text = "1 Sol Ring\n2 Lightning Bolt\n1 Cultivate\n"
    conn = _conn(
        _result([{"head_version": None}]),
        _result([_tip("main", 1, head_text)]),
        _result([_tip("main", 1, head_text), _tip("deckbox", 2, branch_text)]),
    )
    out = await deck_service.list_branches(conn, DECK_ID)
    assert out is not None
    by_branch = {r["branch"]: r for r in out["data"]}
    # Cultivate added, Forest removed, Bolt qty changed — matches
    # `POST /decks/diff` on the same pair (see test_decks_diff.py's
    # `test_diff_added_removed_and_qty_changed`).
    assert by_branch["deckbox"]["differs"] == {"added": 1, "removed": 1, "changed": 1}


@pytest.mark.asyncio
async def test_single_branch_deck_reports_one_row_no_spurious_divergence() -> None:
    text = "1 Sol Ring\n"
    conn = _conn(
        _result([{"head_version": None}]),
        _result([_tip("main", 1, text)]),
        _result([_tip("main", 1, text)]),
    )
    out = await deck_service.list_branches(conn, DECK_ID)
    assert out is not None
    assert len(out["data"]) == 1
    assert out["data"][0]["differs"] == {"added": 0, "removed": 0, "changed": 0}


@pytest.mark.asyncio
async def test_head_is_flagged() -> None:
    text = "1 Sol Ring\n"
    conn = _conn(
        _result([{"head_version": None}]),
        _result([_tip("main", 2, text)]),
        _result([_tip("main", 2, text), _tip("moxfield", 1, "1 Sol Ring\n1 Forest\n")]),
    )
    out = await deck_service.list_branches(conn, DECK_ID)
    assert out is not None
    by_branch = {r["branch"]: r for r in out["data"]}
    assert by_branch["main"]["is_head"] is True
    assert by_branch["moxfield"]["is_head"] is False


@pytest.mark.asyncio
async def test_unknown_deck_returns_none() -> None:
    conn = _conn(_result([]))
    assert await deck_service.list_branches(conn, DECK_ID) is None


@pytest.mark.asyncio
async def test_branch_head_served_by_the_branch_index() -> None:
    """One query for every branch's own latest, not a query per branch."""
    text = "1 Sol Ring\n"
    conn = _conn(
        _result([{"head_version": None}]),
        _result([_tip("main", 1, text)]),
        _result([_tip("main", 1, text)]),
    )
    await deck_service.list_branches(conn, DECK_ID)
    assert conn.execute.await_count == 3
    sqls = [str(c.args[0]) for c in conn.execute.await_args_list]
    assert any("DISTINCT ON (branch)" in s for s in sqls)


# ---- router ----


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


@pytest.mark.asyncio
async def test_router_uses_a_read_connection(app: FastAPI) -> None:
    payload = {"object": "list", "deck_id": DECK_ID, "data": []}
    with _svc("list_branches", new=AsyncMock(return_value=payload)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{DECK_ID}/branches")
    assert resp.status_code == 200
    assert resp.json() == payload
    app.state.read_engine.connect.assert_called_once()
    app.state.read_engine.begin.assert_not_called()


@pytest.mark.asyncio
async def test_router_404s_on_unknown_deck(app: FastAPI) -> None:
    with _svc("list_branches", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{DECK_ID}/branches")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_router_404s_on_a_malformed_id_without_touching_the_db(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/decks/not-a-uuid/branches")
    assert resp.status_code == 404
    app.state.read_engine.connect.assert_not_called()
