"""D19.17 — subtree folder rename.

The `WHERE` shape is the whole deliverable: the obvious
`LIKE :old || '%'` also matches `CommanderX`, silently re-filing an
unrelated sibling folder.
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
from biblioplex.routers import deck_library


def _result(rowcount: int = 0) -> MagicMock:
    res = MagicMock()
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


def _sql(conn: AsyncMock) -> str:
    return str(conn.execute.await_args_list[0].args[0])


def _params(conn: AsyncMock) -> dict[str, Any]:
    return conn.execute.await_args_list[0].args[1]


# ---- the WHERE shape ----


@pytest.mark.asyncio
async def test_rename_matches_exact_folder_or_slash_anchored_descendants() -> None:
    conn = _conn(_result(3))
    await deck_service.rename_folder(conn, "Commander", "Legacy")
    sql = _sql(conn)
    assert "folder = :old" in sql
    assert "LIKE :old_prefix" in sql
    assert "ESCAPE" in sql


@pytest.mark.asyncio
async def test_rename_never_uses_a_bare_prefix_match() -> None:
    """`LIKE 'Commander%'` would re-file `CommanderX` too.

    Pinned on the emitted parameter rather than the SQL text: the prefix
    must carry the separator, so a sibling sharing a name prefix cannot
    be swept in.
    """
    conn = _conn(_result(0))
    await deck_service.rename_folder(conn, "Commander", "Legacy")
    prefix = _params(conn)["old_prefix"]
    assert prefix == "Commander/%"
    assert prefix != "Commander%"


@pytest.mark.asyncio
async def test_rename_escapes_like_metacharacters_in_the_folder_name() -> None:
    """`50%_budget` is a legal folder name and a catastrophic LIKE pattern."""
    conn = _conn(_result(0))
    await deck_service.rename_folder(conn, "50%_budget", "cheap")
    assert _params(conn)["old_prefix"] == r"50\%\_budget/%"


@pytest.mark.asyncio
async def test_rename_rewrites_descendants_by_offset_not_by_replace() -> None:
    """A blind string replace would also rewrite a later occurrence.

    `Commander/x/Commander` must become `Legacy/x/Commander`, not
    `Legacy/x/Legacy`, so the new path is prepended to the remainder
    measured from the old path's length.
    """
    conn = _conn(_result(1))
    await deck_service.rename_folder(conn, "Commander", "Legacy")
    sql = _sql(conn)
    assert "substr(folder" in sql.replace(" ", "").replace("SUBSTR(folder", "substr(folder")
    assert _params(conn)["old_len"] == len("Commander")


# ---- unfiling and no-ops ----


@pytest.mark.asyncio
async def test_rename_to_null_unfiles_the_subtree() -> None:
    """There is no folder row to delete — unfiling IS deletion."""
    conn = _conn(_result(4))
    moved = await deck_service.rename_folder(conn, "Commander", None)
    assert "folder = NULL" in _sql(conn)
    assert moved == 4


@pytest.mark.asyncio
async def test_rename_returns_rows_moved() -> None:
    conn = _conn(_result(7))
    assert await deck_service.rename_folder(conn, "a", "b") == 7


@pytest.mark.asyncio
async def test_rename_of_an_unknown_folder_moves_nothing() -> None:
    conn = _conn(_result(0))
    assert await deck_service.rename_folder(conn, "nope", "other") == 0


@pytest.mark.asyncio
async def test_rename_normalizes_both_sides() -> None:
    conn = _conn(_result(1))
    await deck_service.rename_folder(conn, " /Commander/ ", "//Legacy//Old//")
    params = _params(conn)
    assert params["old"] == "Commander"
    assert params["new"] == "Legacy/Old"


@pytest.mark.asyncio
async def test_rename_does_not_touch_updated_at() -> None:
    """A bulk reorganisation is not per-deck activity.

    `update_deck` moves `updated_at` so the library's newest-touched-first
    ordering reflects real edits. Renaming a folder would otherwise
    reshuffle the entire library for a purely organisational change.
    """
    conn = _conn(_result(1))
    await deck_service.rename_folder(conn, "a", "b")
    assert "updated_at" not in _sql(conn)


@pytest.mark.asyncio
async def test_rename_rejects_an_invalid_source() -> None:
    conn = _conn(_result(0))
    with pytest.raises(ValueError):
        await deck_service.rename_folder(conn, "a\nb", "c")


@pytest.mark.asyncio
async def test_rename_requires_a_source_folder() -> None:
    """Renaming "nothing" would match every unfiled deck."""
    conn = _conn(_result(0))
    with pytest.raises(ValueError):
        await deck_service.rename_folder(conn, "", "c")


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


async def _post(app: FastAPI, url: str, payload: dict[str, Any]) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        return await c.post(url, json=payload)


@pytest.mark.asyncio
async def test_rename_endpoint_returns_the_move_count(app: FastAPI) -> None:
    with patch(
        "biblioplex.routers.deck_library.deck_service.rename_folder",
        new=AsyncMock(return_value=3),
    ):
        resp = await _post(app, "/decks/folders/rename", {"from": "a", "to": "b"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "folder_renamed"
    assert (body["from"], body["to"], body["decks_moved"]) == ("a", "b", 3)


@pytest.mark.asyncio
async def test_rename_endpoint_uses_a_write_transaction(app: FastAPI) -> None:
    """D18.3: `connect()` opens a transaction that is ROLLED BACK on close."""
    engine = app.state.read_engine
    with patch(
        "biblioplex.routers.deck_library.deck_service.rename_folder",
        new=AsyncMock(return_value=1),
    ):
        await _post(app, "/decks/folders/rename", {"from": "a", "to": "b"})
    engine.begin.assert_called_once()
    engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_rename_endpoint_422s_on_a_bad_path(app: FastAPI) -> None:
    resp = await _post(app, "/decks/folders/rename", {"from": "a\nb", "to": "c"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"


@pytest.mark.asyncio
async def test_rename_endpoint_unfiles_when_to_is_omitted(app: FastAPI) -> None:
    spy = AsyncMock(return_value=2)
    with patch("biblioplex.routers.deck_library.deck_service.rename_folder", new=spy):
        resp = await _post(app, "/decks/folders/rename", {"from": "a"})
    assert resp.status_code == 200
    assert resp.json()["to"] is None
    assert spy.await_args.args[2] is None


def test_rename_route_declared_before_the_deck_id_route() -> None:
    paths = [r.path for r in deck_library.router.routes if hasattr(r, "path")]
    assert paths.index("/decks/folders/rename") < paths.index("/decks/{deck_id}")
