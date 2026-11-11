"""D4.2 / D4.6 — ``order`` and ``dir`` query params for ``GET /cards/search`` (§6.1).

Nine ``order`` modes + three ``dir`` modes (``auto``/``asc``/``desc``).
Secondary tie-break is always ``id ASC`` for stable pagination.

``dir=auto`` resolution:
* ``name`` / ``rarity`` / ``color`` → asc
* ``cmc`` / ``power`` / ``toughness`` / ``edhrec`` → asc
* ``released`` / ``usd`` → desc

``edhrec`` sorts on ``cards.edhrec_rank`` (D1.9 ingest column); ``usd`` sorts
on ``cards.price_usd`` (MIN over printings). Both use NULLS LAST so
unranked/unpriced cards always sink. D4.6 resolved the §6.1 edhrec drift
and opened a new §6.1 drift entry for the ``usd`` enum extension.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine

from ._search_helpers import (
    FakeConn,
    FakeEngine,
    client,
    make_row,
    settings,
)

SAMPLE_RAW: dict[str, Any] = {"object": "card", "id": "a", "name": "Llanowar Elves"}


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=settings())


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


async def _hit(app: FastAPI, conn: FakeConn, **params: str) -> str:
    """Issue a /cards/search request and return the rendered SQL (lower)."""
    _override(app, conn)
    qs = {"q": "elf", **params}
    async for c in client(app):
        resp = await c.get("/cards/search", params=qs)
    assert resp.status_code == 200, resp.json()
    return str(conn.executed[-1][0]).lower()


# ---- default + dir auto ----


async def test_default_order_is_name_sort_key_asc(app: FastAPI) -> None:
    """Missing ``order=`` → ``name`` (asc) on ``name_sort_key`` + id ASC tie-break."""
    sql = await _hit(app, FakeConn(rows=[make_row(SAMPLE_RAW, 1)]))
    assert "order by" in sql
    assert "name_sort_key asc" in sql
    assert "cards.id asc" in sql


async def test_id_is_secondary_tie_break_for_all_orders(app: FastAPI) -> None:
    """Every order mode appends ``id ASC`` as the tie-breaker."""
    for order in (
        "name", "cmc", "power", "toughness", "released",
        "color", "edhrec", "rarity", "usd", "relevance",
    ):
        sql = await _hit(app, FakeConn(rows=[make_row(SAMPLE_RAW, 1)]), order=order)
        assert "cards.id asc" in sql, f"order={order} missing id-asc tie-break"


# ---- order column routing ----


@pytest.mark.parametrize(
    "order,expected_snippet",
    [
        ("cmc", "cards.cmc"),
        ("power", "power"),  # cast/case expression — substring is enough
        ("toughness", "toughness"),
        ("released", "released_at"),
        ("color", "cards.color_sort_key"),
        ("rarity", "rarity"),
        ("edhrec", "edhrec_rank"),
        ("usd", "price_usd"),
    ],
)
async def test_order_column_routing(
    app: FastAPI, order: str, expected_snippet: str
) -> None:
    """Each ``order=`` selects the documented column / expression."""
    sql = await _hit(
        app, FakeConn(rows=[make_row(SAMPLE_RAW, 1)]), order=order
    )
    assert expected_snippet in sql, sql


async def test_order_edhrec_sorts_by_edhrec_rank(app: FastAPI) -> None:
    """``order=edhrec`` sorts on the D1.9 ``cards.edhrec_rank`` column, ASC by default."""
    sql = await _hit(
        app, FakeConn(rows=[make_row(SAMPLE_RAW, 1)]), order="edhrec"
    )
    primary, _, _ = sql.partition("cards.id asc")
    assert "edhrec_rank" in primary
    assert "name_sort_key" not in primary
    assert " asc" in primary


async def test_order_usd_sorts_by_price_usd(app: FastAPI) -> None:
    """``order=usd`` sorts on the D1.9 ``cards.price_usd`` column, DESC by default."""
    sql = await _hit(
        app, FakeConn(rows=[make_row(SAMPLE_RAW, 1)]), order="usd"
    )
    primary, _, _ = sql.partition("cards.id asc")
    assert "price_usd" in primary
    assert " desc" in primary


# ---- dir resolution ----


@pytest.mark.parametrize(
    "order,expected_dir",
    [
        ("name", "asc"),
        ("rarity", "asc"),
        ("color", "asc"),
        ("cmc", "asc"),
        ("power", "asc"),
        ("toughness", "asc"),
        ("released", "desc"),
        ("edhrec", "asc"),
        ("usd", "desc"),
        ("relevance", "desc"),
    ],
)
async def test_dir_auto_per_order_mode(
    app: FastAPI, order: str, expected_dir: str
) -> None:
    """``dir=auto`` (default) resolves per the §6.1 / prompt mapping."""
    sql = await _hit(
        app, FakeConn(rows=[make_row(SAMPLE_RAW, 1)]), order=order, dir="auto"
    )
    # The primary order expression always appears before the tie-break.
    primary, _, rest = sql.partition("cards.id asc")
    # Direction keyword must be present in the primary clause.
    assert f" {expected_dir}" in primary, f"order={order} -> {primary!r}"


async def test_dir_asc_explicit(app: FastAPI) -> None:
    """``dir=asc`` forces ASC even for orders whose auto would be DESC."""
    sql = await _hit(
        app,
        FakeConn(rows=[make_row(SAMPLE_RAW, 1)]),
        order="released",
        dir="asc",
    )
    primary, _, _ = sql.partition("cards.id asc")
    assert "released_at asc" in primary


async def test_dir_desc_explicit(app: FastAPI) -> None:
    """``dir=desc`` flips an asc-default order to DESC."""
    sql = await _hit(
        app,
        FakeConn(rows=[make_row(SAMPLE_RAW, 1)]),
        order="cmc",
        dir="desc",
    )
    primary, _, _ = sql.partition("cards.id asc")
    assert "cmc desc" in primary


# ---- NULLS LAST on new orders ----


@pytest.mark.parametrize("direction", ["auto", "asc", "desc"])
async def test_order_edhrec_uses_nulls_last(app: FastAPI, direction: str) -> None:
    """``order=edhrec`` always sinks NULL ranks regardless of direction."""
    sql = await _hit(
        app,
        FakeConn(rows=[make_row(SAMPLE_RAW, 1)]),
        order="edhrec",
        dir=direction,
    )
    primary, _, _ = sql.partition("cards.id asc")
    assert "nulls last" in primary, f"dir={direction}: {primary!r}"


@pytest.mark.parametrize("direction", ["auto", "asc", "desc"])
async def test_order_usd_uses_nulls_last(app: FastAPI, direction: str) -> None:
    """``order=usd`` always sinks NULL prices regardless of direction."""
    sql = await _hit(
        app,
        FakeConn(rows=[make_row(SAMPLE_RAW, 1)]),
        order="usd",
        dir=direction,
    )
    primary, _, _ = sql.partition("cards.id asc")
    assert "nulls last" in primary, f"dir={direction}: {primary!r}"


# ---- validation ----


async def test_invalid_order_returns_422(app: FastAPI) -> None:
    """Unknown ``order=`` value → 422 from the enum validator."""
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get(
            "/cards/search", params={"q": "elf", "order": "wat"}
        )
    assert resp.status_code == 422


async def test_invalid_dir_returns_422(app: FastAPI) -> None:
    """Unknown ``dir=`` value → 422."""
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get(
            "/cards/search", params={"q": "elf", "dir": "sideways"}
        )
    assert resp.status_code == 422


# ---- D11.1 relevance order ----


async def test_order_relevance_sorts_by_word_similarity(app: FastAPI) -> None:
    """``order=relevance`` sorts on `word_similarity(<bare-terms>, name_search) DESC`."""
    sql = await _hit(
        app, FakeConn(rows=[make_row(SAMPLE_RAW, 1)]), order="relevance"
    )
    primary, _, _ = sql.partition("cards.id asc")
    assert "word_similarity" in primary
    assert "name_search" in primary
    # Default dir for relevance is desc — best match first.
    assert " desc" in primary


async def test_order_relevance_filter_only_falls_back_to_name(app: FastAPI) -> None:
    """`order=relevance` with no bare-words falls back to `name_sort_key asc` (no 500)."""
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, 1)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get(
            "/cards/search",
            params={"q": "t:elf", "order": "relevance"},
        )
    assert resp.status_code == 200, resp.json()
    sql = str(conn.executed[-1][0]).lower()
    primary, _, _ = sql.partition("cards.id asc")
    assert "name_sort_key asc" in primary
    assert "word_similarity" not in primary
