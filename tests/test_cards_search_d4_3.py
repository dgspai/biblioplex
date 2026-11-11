"""D4.3 — edge-case validation for ``GET /cards/search`` (§6.1).

* Empty / missing ``q`` → 422 with the spec-mandated ``q is required``.
* ``q`` longer than 1000 chars → 422 ``q too long``.
* ``format=csv`` or ``format=text`` → 400 ``unsupported format`` (JSON-only).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine

from ._search_helpers import FakeConn, FakeEngine, client, settings


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=settings())


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


# ---- q is required ----


async def test_missing_q_returns_422(app: FastAPI) -> None:
    """No ``q`` at all → 422 with `q is required`."""
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get("/cards/search")

    assert resp.status_code == 422
    body = resp.json()
    assert body["object"] == "error"
    assert body["status"] == 422
    assert body["details"] == "q is required"


async def test_empty_q_returns_422(app: FastAPI) -> None:
    """``q=`` (empty string) → 422 with `q is required`."""
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": ""})

    assert resp.status_code == 422
    body = resp.json()
    assert body["details"] == "q is required"


async def test_whitespace_only_q_returns_422(app: FastAPI) -> None:
    """``q="   "`` is logically empty too → 422 `q is required`."""
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "   "})

    assert resp.status_code == 422
    body = resp.json()
    assert body["details"] == "q is required"


# ---- length bound ----


async def test_q_too_long_returns_422(app: FastAPI) -> None:
    """``q`` longer than 1000 chars → 422 `q too long`."""
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "a" * 1001})

    assert resp.status_code == 422
    body = resp.json()
    assert body["object"] == "error"
    assert body["details"] == "q too long"


async def test_q_exactly_1000_chars_passes_validation(app: FastAPI) -> None:
    """1000 chars is the upper bound — must still be accepted."""
    _override(app, FakeConn(rows=[]))
    # Use only `a` chars — a single bare-word token after tokenization.
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "a" * 1000})

    # 404 (total=0 from FakeConn) or 200 — either proves validation passed.
    assert resp.status_code in (200, 404)


# ---- format param ----


@pytest.mark.parametrize("fmt", ["csv", "text"])
async def test_format_unsupported_returns_400(app: FastAPI, fmt: str) -> None:
    """`format=csv` / `format=text` → 400 `unsupported format`."""
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get(
            "/cards/search", params={"q": "elf", "format": fmt}
        )

    assert resp.status_code == 400
    body = resp.json()
    assert body["object"] == "error"
    assert body["status"] == 400
    assert body["details"] == "unsupported format"


async def test_format_json_is_accepted(app: FastAPI) -> None:
    """``format=json`` is the implicit default and must be accepted."""
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get(
            "/cards/search", params={"q": "elf", "format": "json"}
        )

    # 404 (no rows) or 200 — either proves validation passed.
    assert resp.status_code in (200, 404)
