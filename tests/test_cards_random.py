"""D17.3 ``GET /cards/random?q=<optional DSL>``.

Returns one random card, optionally satisfying a DSL filter. Spec §1.2
originally listed `/cards/random` as a non-goal; D17.3 overrides for MCP
agents that ask for "surprise me" / flavor-exploration prompts.

Router pipeline:
  * `q` present: compile via `parse` + `compile_ast` (same shape as
    `/cards/search`). Errors surface as 422 through the same code path.
  * `q` absent: unfiltered corpus.
  * Both branches: `SELECT ... FROM cards WHERE <where> ORDER BY random()
    LIMIT 1`. 404 when the filter matches nothing.

Not cached (random by design). Registered before `/{card_id}` so
FastAPI matches the literal path first.
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
    settings,
)


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=settings())


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


def _row(raw: dict[str, Any]) -> dict[str, Any]:
    """Single-card row shape (no `total` — /random doesn't count)."""
    return {
        "raw": raw,
        "predicted_labels": [],
        "label_scores": {},
    }


SAMPLE = {
    "object": "card",
    "id": "00000000-0000-0000-0000-000000000000",
    "name": "Llanowar Elves",
    "type_line": "Creature — Elf Druid",
}


async def test_random_no_filter_returns_card(app: FastAPI) -> None:
    """No `q` → one random card from the whole corpus."""
    conn = FakeConn(rows=[_row(SAMPLE)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/cards/random")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "card"
    assert body["name"] == "Llanowar Elves"


async def test_random_with_dsl_filter_compiles_where(app: FastAPI) -> None:
    """`q=` compiles to a WHERE clause on the random pick."""
    conn = FakeConn(rows=[_row(SAMPLE)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/cards/random", params={"q": "t:creature c:g"})
    assert resp.status_code == 200
    sql = str(conn.executed[-1][0]).lower()
    assert "from cards" in sql
    assert "where" in sql
    assert "type_search" in sql
    assert "random()" in sql
    assert "limit" in sql


async def test_random_no_match_returns_404(app: FastAPI) -> None:
    """DSL matches zero rows → 404 with Scryfall-shaped error envelope."""
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/cards/random", params={"q": "t:planeswalker c:c"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["object"] == "error"
    assert body["code"] == "not_found"


async def test_random_unfiltered_no_match_returns_404(app: FastAPI) -> None:
    """Empty corpus + no filter still 404s (defensive; DB fully empty)."""
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/cards/random")
    assert resp.status_code == 404


async def test_random_parse_error_returns_422(app: FastAPI) -> None:
    """Malformed DSL → 422 bad_request via the shared parser error path."""
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/cards/random", params={"q": "(unbalanced"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"


async def test_random_unsupported_operator_returns_422(app: FastAPI) -> None:
    """§5.2 rejected key → 422 unsupported_operator."""
    conn = FakeConn(rows=[])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/cards/random", params={"q": "lang:en"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "unsupported_operator"
    assert "lang" in body["details"]


async def test_random_projection_applied(app: FastAPI) -> None:
    """`?projection=` narrows the returned card same as /cards/:id."""
    conn = FakeConn(rows=[_row(SAMPLE)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/cards/random", params={"projection": "name,type_line"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"name", "type_line"}


async def test_random_empty_q_treated_as_unfiltered(app: FastAPI) -> None:
    """`q=` empty/whitespace behaves like no `q` at all (not 422).

    /search enforces `q required`; /random accepts unfiltered by design,
    so a whitespace-only q is not an error, just a no-op filter.
    """
    conn = FakeConn(rows=[_row(SAMPLE)])
    _override(app, conn)
    async for c in client(app):
        resp = await c.get("/cards/random", params={"q": "   "})
    assert resp.status_code == 200
    sql = str(conn.executed[-1][0]).lower()
    # Unfiltered branch: no `where` narrowing on user DSL columns.
    assert "type_search" not in sql
