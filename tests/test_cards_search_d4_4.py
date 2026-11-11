"""D4.4 — List response shape + pagination for ``GET /cards/search`` (§6.1).

Scryfall ``List`` envelope::

    {
      "object": "list",
      "total_cards": N,
      "has_more": bool,
      "next_page": "...?page=2",   # present only when has_more
      "data": [...]                  # raw card payloads
    }

``total_cards`` is computed via ``COUNT(*) OVER ()`` in the same SELECT
(no second round-trip). ``has_more = total_cards > page * 175``.
``next_page`` echoes the request URL with ``page`` bumped. ``total == 0``
→ 404 per the §6.1 final line.
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


# ---- envelope ----


async def test_envelope_has_required_keys(app: FastAPI) -> None:
    """Top-level keys: object, total_cards, has_more, data."""
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, total=1)])
    _override(app, conn)

    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "elf"})

    body = resp.json()
    assert resp.status_code == 200
    assert body["object"] == "list"
    assert body["total_cards"] == 1
    assert body["has_more"] is False
    assert body["data"] == [
        {**SAMPLE_RAW, "predicted_labels": [], "label_scores": {}}
    ]


async def test_data_is_list_of_raw_payloads(app: FastAPI) -> None:
    """``data`` returns the raw Scryfall JSON for each matched row."""
    raws = [
        {"object": "card", "id": "a", "name": "Llanowar Elves"},
        {"object": "card", "id": "b", "name": "Elvish Mystic"},
    ]
    rows = [make_row(r, total=2) for r in raws]
    conn = FakeConn(rows=rows)
    _override(app, conn)

    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "elf"})

    body = resp.json()
    assert body["data"] == [
        {**r, "predicted_labels": [], "label_scores": {}} for r in raws
    ]


# ---- total + has_more + next_page ----


async def test_total_cards_uses_count_window(app: FastAPI) -> None:
    """The SQL must include ``count(*) over ()`` so total comes in one trip."""
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, total=42)])
    _override(app, conn)

    async for c in client(app):
        await c.get("/cards/search", params={"q": "elf"})

    sql = str(conn.executed[-1][0]).lower()
    assert "count(*) over" in sql


async def test_has_more_true_when_total_exceeds_page(app: FastAPI) -> None:
    """``has_more`` flips true when total > page * 175."""
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, total=200)])
    _override(app, conn)

    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "elf"})

    body = resp.json()
    assert body["total_cards"] == 200
    assert body["has_more"] is True


async def test_next_page_present_only_when_has_more(app: FastAPI) -> None:
    """``next_page`` echoes the request URL with ``page=N+1``."""
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, total=200)])
    _override(app, conn)

    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "elf"})

    body = resp.json()
    assert "next_page" in body
    assert "page=2" in body["next_page"]
    # Original query string is preserved.
    assert "q=elf" in body["next_page"]


async def test_next_page_absent_when_no_more(app: FastAPI) -> None:
    """``next_page`` must not appear when ``has_more`` is False."""
    conn = FakeConn(rows=[make_row(SAMPLE_RAW, total=1)])
    _override(app, conn)

    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "elf"})

    body = resp.json()
    assert body["has_more"] is False
    assert "next_page" not in body


# ---- page param ----


async def test_page_param_offsets_results(app: FastAPI) -> None:
    """``page=2`` issues OFFSET 175 (page*175 - 175)."""
    from sqlalchemy.dialects import postgresql

    conn = FakeConn(rows=[make_row(SAMPLE_RAW, total=400)])
    _override(app, conn)

    async for c in client(app):
        resp = await c.get(
            "/cards/search", params={"q": "elf", "page": "2"}
        )

    assert resp.status_code == 200
    stmt = conn.executed[-1][0]
    # Render with literal binds so OFFSET/LIMIT show their numeric values.
    rendered = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "offset 175" in rendered
    assert "limit 175" in rendered
    body = resp.json()
    assert body["has_more"] is True
    assert "page=3" in body["next_page"]


async def test_page_one_has_offset_zero(app: FastAPI) -> None:
    """``page=1`` (default) uses OFFSET 0."""
    from sqlalchemy.dialects import postgresql

    conn = FakeConn(rows=[make_row(SAMPLE_RAW, total=1)])
    _override(app, conn)

    async for c in client(app):
        await c.get("/cards/search", params={"q": "elf"})

    rendered = str(
        conn.executed[-1][0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "offset 0" in rendered


async def test_page_zero_rejected_with_422(app: FastAPI) -> None:
    """``page=0`` is invalid (must be ≥1) → FastAPI returns 422."""
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "elf", "page": "0"})

    assert resp.status_code == 422


async def test_page_negative_rejected_with_422(app: FastAPI) -> None:
    """``page=-3`` → 422."""
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "elf", "page": "-3"})

    assert resp.status_code == 422


# ---- 404 ----


async def test_total_zero_returns_404(app: FastAPI) -> None:
    """``total_cards == 0`` → 404 per §6.1 final line."""
    conn = FakeConn(rows=[])
    _override(app, conn)

    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "zzz"})

    assert resp.status_code == 404
    body = resp.json()
    assert body["object"] == "error"
    assert body["code"] == "not_found"
