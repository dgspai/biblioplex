"""D19.20 — `GET /decks/{id}/cards`, the deck as a table."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.decks import service as deck_service
from biblioplex.main import create_app


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


def _sqls(conn: AsyncMock) -> list[str]:
    return [str(c.args[0]) for c in conn.execute.await_args_list]


DECK_ID = "11111111-1111-1111-1111-111111111111"


def _row(**kw: Any) -> dict[str, Any]:
    base = {
        "section": "main",
        "card_name": "Sol Ring",
        "qty": 1,
        "oracle_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "name": "Sol Ring",
        "type_line": "Artifact",
        "mana_cost": "{1}",
        "cmc": 1,
        "price_usd": None,
        "qty_owned": 0,
        "on_wishlist": False,
    }
    base.update(kw)
    # `name` follows `card_name` unless explicitly overridden — otherwise
    # two rows with different card names collapse to one key in the tests.
    if "card_name" in kw and "name" not in kw:
        base["name"] = kw["card_name"]
    return base


# ---- shape ----


@pytest.mark.asyncio
async def test_unknown_deck_returns_none() -> None:
    conn = _conn(_result([]))
    assert await deck_service.list_deck_cards(conn, DECK_ID) is None


@pytest.mark.asyncio
async def test_reads_the_derived_index_not_the_decklist_text() -> None:
    """The whole point of D19.19 — no re-parse per request."""
    conn = _conn(_result([{"id": DECK_ID}]), _result([_row()]))
    await deck_service.list_deck_cards(conn, DECK_ID)
    assert any("deck_cards" in s for s in _sqls(conn))
    assert not any("deck_versions" in s for s in _sqls(conn))


@pytest.mark.asyncio
async def test_sideboard_rows_are_included() -> None:
    """`/decks/analyze` reads mainboard only (D19.9); a table must not."""
    conn = _conn(
        _result([{"id": DECK_ID}]),
        _result([_row(section="main"), _row(section="sideboard", card_name="Grapeshot")]),
    )
    out = await deck_service.list_deck_cards(conn, DECK_ID)
    assert {r["section"] for r in out["data"]} == {"main", "sideboard"}


@pytest.mark.asyncio
async def test_rows_are_flat_with_a_section_field() -> None:
    """Flat, not grouped: sorting the whole deck by ownership is the ask,
    and a grouped payload would fight it."""
    conn = _conn(_result([{"id": DECK_ID}]), _result([_row()]))
    out = await deck_service.list_deck_cards(conn, DECK_ID)
    assert isinstance(out["data"], list)
    assert "section" in out["data"][0]


# ---- ownership arithmetic ----


@pytest.mark.asyncio
async def test_ownership_sums_across_printings() -> None:
    """D18.5's lesson: `inventory` is one row PER PRINTING.

    A row-read instead of a SUM keeps only the last edition's quantity and
    under-reports a card owned in two sets.
    """
    conn = _conn(_result([{"id": DECK_ID}]), _result([_row()]))
    await deck_service.list_deck_cards(conn, DECK_ID)
    sql = " ".join(_sqls(conn))
    assert "SUM(quantity)" in sql or "sum(quantity)" in sql


@pytest.mark.asyncio
async def test_missing_is_the_shortfall_never_negative() -> None:
    conn = _conn(
        _result([{"id": DECK_ID}]),
        _result([
            _row(card_name="Bolt", qty=4, qty_owned=2),
            _row(card_name="Ring", qty=1, qty_owned=3),
        ]),
    )
    out = await deck_service.list_deck_cards(conn, DECK_ID)
    by = {r["name"]: r for r in out["data"]}
    assert (by["Bolt"]["qty_owned"], by["Bolt"]["qty_missing"]) == (2, 2)
    assert by["Ring"]["qty_missing"] == 0, "owning extras must not go negative"


@pytest.mark.asyncio
async def test_unresolved_cards_still_appear() -> None:
    """A name that resolves to nothing is still a card in the deck.

    Dropping it would make the table disagree with the deck's own card
    count and silently inflate coverage.
    """
    conn = _conn(
        _result([{"id": DECK_ID}]),
        _result([_row(card_name="Nonesuch", oracle_id=None, id=None, name=None)]),
    )
    out = await deck_service.list_deck_cards(conn, DECK_ID)
    row = out["data"][0]
    assert row["name"] == "Nonesuch"
    assert row["id"] is None
    assert row["qty_owned"] == 0


@pytest.mark.asyncio
async def test_summary_totals_match_the_rows() -> None:
    conn = _conn(
        _result([{"id": DECK_ID}]),
        _result([
            _row(card_name="Bolt", qty=4, qty_owned=2),
            _row(card_name="Ring", qty=1, qty_owned=1),
        ]),
    )
    out = await deck_service.list_deck_cards(conn, DECK_ID)
    s = out["summary"]
    assert s["total_cards"] == 5
    assert s["owned_cards"] == 3
    assert s["missing_cards"] == 2
    assert s["coverage_pct"] == 60.0


@pytest.mark.asyncio
async def test_empty_deck_reports_full_coverage_not_a_zero_divide() -> None:
    conn = _conn(_result([{"id": DECK_ID}]), _result([]))
    out = await deck_service.list_deck_cards(conn, DECK_ID)
    assert out["summary"]["coverage_pct"] == 100.0
    assert out["data"] == []


@pytest.mark.asyncio
async def test_uses_cast_not_double_colon() -> None:
    conn = _conn(_result([{"id": DECK_ID}]), _result([_row()]))
    await deck_service.list_deck_cards(conn, DECK_ID)
    for sql in _sqls(conn):
        assert "::uuid" not in sql


# ---- router ----


def _placeholder_settings() -> Settings:
    return Settings.model_construct(
        database_url="postgresql+psycopg://placeholder/placeholder",
        hf_embed_model="sentence-transformers/all-MiniLM-L6-v2",
        hf_embed_dim=384, hf_embed_device="cpu", embed_batch=256,
        bulk_source_type="default_cards", web_concurrency=1,
        cors_origins="*", log_level="INFO",
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


async def _get(app: FastAPI, url: str) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(url)


@pytest.mark.asyncio
async def test_endpoint_returns_the_table(app: FastAPI) -> None:
    payload = {"object": "deck_cards", "data": [], "summary": {}}
    with patch("biblioplex.routers.deck_library.deck_service.list_deck_cards",
               new=AsyncMock(return_value=payload)):
        resp = await _get(app, f"/decks/{DECK_ID}/cards")
    assert resp.status_code == 200
    assert resp.json() == payload


@pytest.mark.asyncio
async def test_endpoint_404s_on_unknown_deck(app: FastAPI) -> None:
    with patch("biblioplex.routers.deck_library.deck_service.list_deck_cards",
               new=AsyncMock(return_value=None)):
        resp = await _get(app, f"/decks/{DECK_ID}/cards")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_endpoint_404s_on_a_malformed_id(app: FastAPI) -> None:
    resp = await _get(app, "/decks/not-a-uuid/cards")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_is_a_read(app: FastAPI) -> None:
    engine = app.state.read_engine
    with patch("biblioplex.routers.deck_library.deck_service.list_deck_cards",
               new=AsyncMock(return_value={"object": "deck_cards", "data": [], "summary": {}})):
        await _get(app, f"/decks/{DECK_ID}/cards")
    engine.begin.assert_not_called()


# ---- D19.35: `?version=` reads a non-HEAD version live ----


def _version_row(**kw: Any) -> dict[str, Any]:
    base = {
        "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "version": 2,
        "branch": "deckbox",
        "message": None,
        "decklist": "1 Sol Ring\n",
        "source": "upload",
        "filename": None,
        "size_bytes": 10,
        "total_cards": 1,
        "distinct_cards": 1,
        "unresolved_count": 0,
        "created_at": None,
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_version_param_reads_live_not_the_derived_index() -> None:
    """`deck_cards` only ever holds HEAD (D19.34) — a non-HEAD version must
    never be read from it, or a branch's own content would silently show
    whatever HEAD happens to be."""
    conn = _conn(
        _result([{"id": DECK_ID}]),
        _result([_version_row()]),
        _result([_row()]),
    )
    out = await deck_service.list_deck_cards(conn, DECK_ID, version=2)
    assert out is not None
    sqls = _sqls(conn)
    assert any("deck_versions" in s for s in sqls), "must read the version's own decklist"
    assert not any("FROM deck_cards" in s for s in sqls), "must not read the HEAD index"


@pytest.mark.asyncio
async def test_version_param_unknown_version_returns_none() -> None:
    conn = _conn(_result([{"id": DECK_ID}]), _result([]))
    out = await deck_service.list_deck_cards(conn, DECK_ID, version=99)
    assert out is None


@pytest.mark.asyncio
async def test_version_param_never_writes() -> None:
    """Viewing a branch's head must never touch `deck_cards` — that would
    silently turn "view" into "promote"."""
    conn = _conn(
        _result([{"id": DECK_ID}]),
        _result([_version_row()]),
        _result([_row()]),
    )
    await deck_service.list_deck_cards(conn, DECK_ID, version=2)
    sqls = _sqls(conn)
    assert not any(
        s.strip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in sqls
    )


@pytest.mark.asyncio
async def test_version_param_shapes_the_same_summary() -> None:
    conn = _conn(
        _result([{"id": DECK_ID}]),
        _result([_version_row()]),
        _result([_row(card_name="Bolt", qty=4, qty_owned=2)]),
    )
    out = await deck_service.list_deck_cards(conn, DECK_ID, version=2)
    assert out["summary"]["total_cards"] == 4
    assert out["summary"]["owned_cards"] == 2


@pytest.mark.asyncio
async def test_omitted_version_still_reads_the_derived_index() -> None:
    """No-`version` behaviour is unchanged by this addition."""
    conn = _conn(_result([{"id": DECK_ID}]), _result([_row()]))
    await deck_service.list_deck_cards(conn, DECK_ID)
    sqls = _sqls(conn)
    assert any("FROM deck_cards" in s for s in sqls)
    assert not any("deck_versions" in s for s in sqls)


@pytest.mark.asyncio
async def test_endpoint_forwards_version_query_param(app: FastAPI) -> None:
    payload = {"object": "deck_cards", "data": [], "summary": {}}
    mocked = AsyncMock(return_value=payload)
    with patch("biblioplex.routers.deck_library.deck_service.list_deck_cards", new=mocked):
        resp = await _get(app, f"/decks/{DECK_ID}/cards?version=2")
    assert resp.status_code == 200
    mocked.assert_awaited_once()
    assert mocked.await_args.kwargs.get("version") == 2


@pytest.mark.asyncio
async def test_endpoint_version_defaults_to_none(app: FastAPI) -> None:
    payload = {"object": "deck_cards", "data": [], "summary": {}}
    mocked = AsyncMock(return_value=payload)
    with patch("biblioplex.routers.deck_library.deck_service.list_deck_cards", new=mocked):
        await _get(app, f"/decks/{DECK_ID}/cards")
    assert mocked.await_args.kwargs.get("version") is None


# ---- D19.39: commander/companion follow the VIEWED version -----------------


@pytest.mark.asyncio
async def test_version_param_derives_commander_from_that_versions_decklist() -> None:
    """No override on the deck — a branch whose decklist names a different
    commander must report THAT commander, not head's."""
    conn = _conn(
        _result([{"id": DECK_ID, "commander": None, "companion": None}]),
        _result([_version_row(decklist="1 Commander\n1 Sol Ring\n\nCommander\nAtraxa, Praetors' Voice\n")]),
        _result([_row()]),
    )
    out = await deck_service.list_deck_cards(conn, DECK_ID, version=2)
    assert out["commander"] == "Atraxa, Praetors' Voice"
    assert out["companion"] is None


@pytest.mark.asyncio
async def test_version_param_override_wins_regardless_of_branch() -> None:
    """`decks.commander` is an OVERRIDE — a deck-level property, not a
    per-version one — so it reports identically no matter which branch's
    decklist is being viewed, even when that decklist names someone else."""
    conn = _conn(
        _result([{"id": DECK_ID, "commander": "The Ur-Dragon", "companion": "Yorion, Sky Nomad"}]),
        _result([_version_row(decklist="Commander\nAtraxa, Praetors' Voice\n\n1 Sol Ring\n")]),
        _result([_row()]),
    )
    out = await deck_service.list_deck_cards(conn, DECK_ID, version=2)
    assert out["commander"] == "The Ur-Dragon"
    assert out["companion"] == "Yorion, Sky Nomad"


@pytest.mark.asyncio
async def test_omitted_version_carries_no_commander_field() -> None:
    """HEAD's commander/companion are already on `GET /decks/{id}` — the
    indexed-HEAD path does not duplicate them."""
    conn = _conn(_result([{"id": DECK_ID, "commander": None, "companion": None}]), _result([_row()]))
    out = await deck_service.list_deck_cards(conn, DECK_ID)
    assert "commander" not in out
    assert "companion" not in out
