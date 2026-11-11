"""D19.16 — deck folders as a normalised path column."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.decks import service as deck_service
from biblioplex.decks.service import (
    MAX_FOLDER_LEN,
    folder_like_prefix,
    normalize_folder,
)
from biblioplex.main import create_app
from biblioplex.routers import deck_library

# ---- normalisation ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("/", None),
        ("///", None),
        (" / / ", None),
        ("Commander", "Commander"),
        ("Commander/Budget", "Commander/Budget"),
        ("  Commander / Budget  ", "Commander/Budget"),
        ("/Commander/Budget/", "Commander/Budget"),
        ("Commander//Budget", "Commander/Budget"),
        ("a/b/c/d/e", "a/b/c/d/e"),
    ],
)
def test_normalize_folder(raw: str | None, expected: str | None) -> None:
    assert normalize_folder(raw) == expected


def test_normalize_folder_preserves_case() -> None:
    """Stored as typed. `Commander` and `commander` are two folders.

    Case-folding would surprise anyone who capitalises deliberately; the
    priced cost is phantom folders, and D19.17's subtree rename is the fix.
    """
    assert normalize_folder("commander") == "commander"
    assert normalize_folder("Commander") == "Commander"


def test_normalize_folder_rejects_overlong_path() -> None:
    with pytest.raises(ValueError, match="too long"):
        normalize_folder("x" * (MAX_FOLDER_LEN + 1))


@pytest.mark.parametrize("bad", ["a\nb", "a\tb", "a\x00b", "a\rb"])
def test_normalize_folder_rejects_control_characters(bad: str) -> None:
    """A newline in a folder name breaks any tree the UI tries to render."""
    with pytest.raises(ValueError):
        normalize_folder(bad)


# ---- LIKE escaping ----


def test_folder_like_prefix_anchors_on_a_slash() -> None:
    """`Commander` must not match `CommanderX` — the trap D19.17 inherits."""
    assert folder_like_prefix("Commander") == "Commander/%"


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("50%budget", r"50\%budget/%"),
        ("a_b", r"a\_b/%"),
        (r"back\slash", "back" + "\\\\" + "slash/%"),
    ],
)
def test_folder_like_prefix_escapes_metacharacters(folder: str, expected: str) -> None:
    """`%` and `_` in a real folder name would otherwise match arbitrarily."""
    assert folder_like_prefix(folder) == expected


# ---- service ----


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


def _sql(conn: AsyncMock, index: int) -> str:
    return str(conn.execute.await_args_list[index].args[0])


def _params(conn: AsyncMock, index: int) -> dict[str, Any]:
    return conn.execute.await_args_list[index].args[1]


@pytest.mark.asyncio
async def test_list_folders_derives_missing_ancestors() -> None:
    """A deck in `Commander/Budget` implies a `Commander` node.

    Without the derived ancestor the UI cannot render a tree — it would
    have a leaf whose parent does not exist in the list.
    """
    conn = _conn(
        _result([
            {"folder": "Commander/Budget", "deck_count": 2},
            {"folder": "Standard", "deck_count": 1},
        ]),
        _result([{"unfiled": 3}]),
    )
    out = await deck_service.list_folders(conn)
    by_path = {row["path"]: row for row in out["data"]}

    assert "Commander" in by_path, "ancestor was not derived"
    assert by_path["Commander"]["deck_count"] == 0
    assert by_path["Commander"]["total_deck_count"] == 2
    assert by_path["Commander/Budget"]["deck_count"] == 2
    assert by_path["Standard"]["deck_count"] == 1
    assert out["unfiled_count"] == 3


@pytest.mark.asyncio
async def test_list_folders_reports_depth_and_name() -> None:
    conn = _conn(_result([{"folder": "a/b/c", "deck_count": 1}]), _result([{"unfiled": 0}]))
    by_path = {r["path"]: r for r in (await deck_service.list_folders(conn))["data"]}
    assert (by_path["a"]["depth"], by_path["a"]["name"]) == (0, "a")
    assert (by_path["a/b/c"]["depth"], by_path["a/b/c"]["name"]) == (2, "c")


@pytest.mark.asyncio
async def test_list_decks_filters_on_exact_folder() -> None:
    conn = _conn(_result([{"total": 0}]), _result([]))
    await deck_service.list_decks(conn, folder="Commander")
    assert "d.folder = :folder" in _sql(conn, 0)
    assert _params(conn, 0)["folder"] == "Commander"


@pytest.mark.asyncio
async def test_list_decks_include_subfolders_uses_escaped_prefix() -> None:
    conn = _conn(_result([{"total": 0}]), _result([]))
    await deck_service.list_decks(conn, folder="Commander", include_subfolders=True)
    sql = _sql(conn, 0)
    assert "d.folder = :folder" in sql
    assert "LIKE :folder_prefix" in sql
    assert "ESCAPE" in sql, "unescaped LIKE lets % in a folder name match everything"
    assert _params(conn, 0)["folder_prefix"] == "Commander/%"


@pytest.mark.asyncio
async def test_list_decks_unfiled_selects_null_folder() -> None:
    conn = _conn(_result([{"total": 0}]), _result([]))
    await deck_service.list_decks(conn, unfiled=True)
    assert "d.folder IS NULL" in _sql(conn, 0)


@pytest.mark.asyncio
async def test_list_decks_without_folder_args_does_not_filter() -> None:
    conn = _conn(_result([{"total": 0}]), _result([]))
    await deck_service.list_decks(conn)
    assert "folder" not in _sql(conn, 0)


def test_folder_is_a_mutable_field() -> None:
    """PATCH reaches it through the shared MUTABLE_FIELDS tuple."""
    assert "folder" in deck_service.MUTABLE_FIELDS


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


async def _get(app: FastAPI, url: str) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        return await c.get(url)


def test_folders_route_is_declared_before_the_deck_id_route() -> None:
    """Order-dependent and invisible in review.

    `/folders` declared after `/{deck_id}` is matched as a deck id and 422s
    on the UUID parse. FastAPI resolves in declaration order, so this is a
    real ordering constraint, not a style preference. Asserted on the
    router that owns the declarations — `app.routes` holds wrapper objects
    whose leaf paths are not exposed.
    """
    paths = [r.path for r in deck_library.router.routes if hasattr(r, "path")]
    assert paths.index("/decks/folders") < paths.index("/decks/{deck_id}")


@pytest.mark.asyncio
async def test_get_decks_folders_hits_the_folder_listing(app: FastAPI) -> None:
    payload = {"object": "list", "data": [], "unfiled_count": 0}
    with _svc("list_folders", new=AsyncMock(return_value=payload)) as spy:
        resp = await _get(app, "/decks/folders")
    assert resp.status_code == 200
    assert resp.json() == payload
    spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_decks_folders_does_not_reach_get_deck(app: FastAPI) -> None:
    """The regression the ordering pin exists to prevent."""
    with (
        _svc("list_folders", new=AsyncMock(return_value={"object": "list", "data": [], "unfiled_count": 0})),
        _svc("get_deck", new=AsyncMock(return_value=None)) as get_deck,
    ):
        resp = await _get(app, "/decks/folders")
    assert resp.status_code == 200
    get_deck.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_decks_passes_folder_filters_through(app: FastAPI) -> None:
    empty = {"object": "list", "data": [], "total": 0, "page": 1, "limit": 50}
    with _svc("list_decks", new=AsyncMock(return_value=empty)) as spy:
        await _get(app, "/decks?folder=Commander&include_subfolders=true")
    kwargs = spy.await_args.kwargs
    assert kwargs["folder"] == "Commander"
    assert kwargs["include_subfolders"] is True


@pytest.mark.asyncio
async def test_empty_folder_query_means_unfiled(app: FastAPI) -> None:
    """`?folder=` is not the same request as omitting the parameter."""
    empty = {"object": "list", "data": [], "total": 0, "page": 1, "limit": 50}
    with _svc("list_decks", new=AsyncMock(return_value=empty)) as spy:
        await _get(app, "/decks?folder=")
    kwargs = spy.await_args.kwargs
    assert kwargs["unfiled"] is True
    assert kwargs["folder"] is None


@pytest.mark.asyncio
async def test_invalid_folder_on_create_is_422(app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/decks",
            json={"name": "x", "decklist": "1 Sol Ring", "folder": "a\nb"},
        )
    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"


@pytest.mark.asyncio
async def test_list_rows_carry_the_folder() -> None:
    """The library listing builds its own summary row, not `_deck_payload`.

    Missed on the first pass and caught only against a live stack: without
    `folder` here a client can filter by folder but cannot show which one
    a deck is in, which is most of what a folder tree needs.
    """
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "Iroh gift",
        "format": "commander",
        "commander": None,
        "companion": None,
        "notes": None,
        "deckbox_url": None,
        "moxfield_url": None,
        "archidekt_url": None,
        "folder": "Commander/Budget",
        "created_at": MagicMock(isoformat=lambda: "2026-08-18T00:00:00+00:00"),
        "updated_at": MagicMock(isoformat=lambda: "2026-08-18T00:00:00+00:00"),
        "version": 1,
        "decklist": "1 Sol Ring\n",
        "total_cards": 1,
        "distinct_cards": 1,
        "unresolved_count": 0,
        "version_created_at": MagicMock(isoformat=lambda: "2026-08-18T00:00:00+00:00"),
        "cov_total": None,
        "cov_owned": None,
        "cov_missing_usd": None,
        "cov_unpriced_missing": None, "cov_unresolved": 0,
    }
    conn = _conn(_result([{"total": 1}]), _result([row]))
    out = await deck_service.list_decks(conn)
    assert out["data"][0]["folder"] == "Commander/Budget"
