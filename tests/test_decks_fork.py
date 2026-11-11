"""D19.41 — `POST /decks/{id}/fork`, variants as decks, not branches.

A fork creates a NEW deck seeded at version 1 from the source's HEAD,
reusing `create_deck` + `append_version` for the write — `fork_deck` never
opens a second insert path, it only decides WHAT to hand `create_deck`.
Service-level tests exercise `deck_service.fork_deck` with `create_deck`
mocked out, so each pins exactly what fork_deck decided to seed with,
without re-testing `create_deck`/`append_version`'s own machinery (already
covered by `test_decks_library_service.py`). Router tests pin the HTTP
wiring the way `test_decks_branches.py` already does for its sibling route.
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


def _deck_row(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": DECK_ID,
        "name": "Iroh gift",
        "format": "commander",
        "commander": "Iroh, Grand Lotus",
        "companion": None,
        "notes": "gift deck",
        "deckbox_url": "https://deckbox.org/sets/1",
        "moxfield_url": "https://moxfield.com/decks/abc",
        "archidekt_url": None,
        "folder": "Commander/Budget",
        "trunk_branch": "main",
        "head_version": 2,
        "head_pinned": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return base


def _version_row(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "22222222-2222-2222-2222-222222222222",
        "version": 2,
        "branch": "main",
        "message": None,
        "decklist": "1 Sol Ring\n1 Lightning Bolt\n",
        "source": "upload",
        "filename": "deck.txt",
        "size_bytes": 10,
        "total_cards": 2,
        "distinct_cards": 2,
        "unresolved_count": 0,
        "created_at": NOW,
    }
    base.update(overrides)
    return base


def _fake_create_deck(captured: dict[str, Any], out: dict[str, Any] | None = None) -> Any:
    async def _impl(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return out if out is not None else {"object": "deck", "id": "new-id", **kwargs}

    return _impl


# ---- service ----


@pytest.mark.asyncio
async def test_fork_seeds_from_head_not_newest_version() -> None:
    """The sharpest case: a pinned head on an OLD version must be what the
    fork's content matches — not the deck's newest version. `head_version`
    is pinned to 2; `_head_version_row` is what resolves that, so a mocked
    connection returning version 2's row (not the newest) proves fork_deck
    reads HEAD, never `MAX(version)` directly."""
    head_text = "1 Sol Ring\n"
    conn = _conn(
        _result([_deck_row(head_version=2)]),
        _result([_version_row(version=2, decklist=head_text)]),
    )
    captured: dict[str, Any] = {}
    with patch.object(deck_service, "create_deck", new=_fake_create_deck(captured)):
        await deck_service.fork_deck(conn, DECK_ID, name=None)
    assert captured["decklist"] == head_text


@pytest.mark.asyncio
async def test_fork_inherits_metadata_not_urls() -> None:
    conn = _conn(
        _result([_deck_row()]),
        _result([_version_row()]),
    )
    captured: dict[str, Any] = {}
    with patch.object(deck_service, "create_deck", new=_fake_create_deck(captured)):
        await deck_service.fork_deck(conn, DECK_ID, name=None)
    assert captured["deck_format"] == "commander"
    assert captured["commander"] == "Iroh, Grand Lotus"
    assert captured["companion"] is None
    assert captured["folder"] == "Commander/Budget"
    # Service URLs, notes: absent from the call entirely (create_deck's own
    # defaults are None) — never copied from the source deck.
    assert "deckbox_url" not in captured
    assert "moxfield_url" not in captured
    assert "archidekt_url" not in captured
    assert "notes" not in captured


@pytest.mark.asyncio
async def test_fork_source_is_fork() -> None:
    conn = _conn(
        _result([_deck_row()]),
        _result([_version_row()]),
    )
    captured: dict[str, Any] = {}
    with patch.object(deck_service, "create_deck", new=_fake_create_deck(captured)):
        await deck_service.fork_deck(conn, DECK_ID, name=None)
    assert captured["source"] == "fork"


@pytest.mark.asyncio
async def test_fork_defaults_name_with_suffix() -> None:
    conn = _conn(
        _result([_deck_row(name="Iroh gift")]),
        _result([_version_row()]),
    )
    captured: dict[str, Any] = {}
    with patch.object(deck_service, "create_deck", new=_fake_create_deck(captured)):
        out = await deck_service.fork_deck(conn, DECK_ID, name=None)
    assert captured["name"] == "Iroh gift (fork)"
    assert out["name"] == "Iroh gift (fork)"


@pytest.mark.asyncio
async def test_fork_uses_explicit_name_when_given() -> None:
    conn = _conn(
        _result([_deck_row(name="Iroh gift")]),
        _result([_version_row()]),
    )
    captured: dict[str, Any] = {}
    with patch.object(deck_service, "create_deck", new=_fake_create_deck(captured)):
        await deck_service.fork_deck(conn, DECK_ID, name="Budget Iroh")
    assert captured["name"] == "Budget Iroh"


@pytest.mark.asyncio
async def test_fork_calls_create_deck_exactly_once() -> None:
    """No loop over the source's version history — a fork is one write."""
    calls: list[dict[str, Any]] = []

    async def _impl(conn: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"object": "deck", "id": "new-id", "version": 1, **kwargs}

    conn = _conn(
        _result([_deck_row()]),
        _result([_version_row()]),
    )
    with patch.object(deck_service, "create_deck", new=_impl):
        await deck_service.fork_deck(conn, DECK_ID, name=None)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fork_unknown_deck_returns_none() -> None:
    conn = _conn(_result([]))
    assert await deck_service.fork_deck(conn, DECK_ID, name=None) is None


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
async def test_router_forks_on_a_write_transaction(app: FastAPI) -> None:
    payload = {"object": "deck", "id": "new-id", "name": "Iroh gift (fork)"}
    with _svc("fork_deck", new=AsyncMock(return_value=payload)) as mocked:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(f"/decks/{DECK_ID}/fork", json={})
    assert resp.status_code == 200
    assert resp.json() == payload
    app.state.read_engine.begin.assert_called_once()
    app.state.read_engine.connect.assert_not_called()
    mocked.assert_awaited_once()
    assert mocked.await_args.kwargs["name"] is None


@pytest.mark.asyncio
async def test_router_passes_explicit_name(app: FastAPI) -> None:
    payload = {"object": "deck", "id": "new-id", "name": "Budget Iroh"}
    with _svc("fork_deck", new=AsyncMock(return_value=payload)) as mocked:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(f"/decks/{DECK_ID}/fork", json={"name": "Budget Iroh"})
    assert resp.status_code == 200
    assert mocked.await_args.kwargs["name"] == "Budget Iroh"


@pytest.mark.asyncio
async def test_router_404s_on_unknown_deck(app: FastAPI) -> None:
    with _svc("fork_deck", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(f"/decks/{DECK_ID}/fork", json={})
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_router_404s_on_a_malformed_id_without_touching_the_db(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/decks/not-a-uuid/fork", json={})
    assert resp.status_code == 404
    app.state.read_engine.begin.assert_not_called()
