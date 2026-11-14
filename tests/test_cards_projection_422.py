"""422 contract for ``?projection=`` invalid input (D7.4, §7).

The Scryfall-shaped error envelope agents already speak::

    {"object":"error","status":422,"code":"invalid_projection",
     "details":"<msg>"}

Triggered on:

* Unknown top-level key (whitelist miss).
* Oversize path depth (>6 segments).
* Empty / malformed paths (leading/trailing comma, empty segment).

The body must be identical across all four wired endpoints — agents
shouldn't have to learn three error shapes for one validation rule.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine

from ._search_helpers import FakeConn, FakeEngine, make_row, settings

SCRY_UUID = "0000ec70-1c4e-4ad5-8d77-d3963fa1fa42"
RAW: dict[str, Any] = {"object": "card", "id": SCRY_UUID, "name": "Llanowar Elves"}


class _ScalarRow:
    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.oracle_id = "00000000-0000-0000-0000-000000000000"
        self.predicted_labels: list[str] = []
        self.label_scores: dict[str, float] = {}
        self.label_reasons: dict[str, Any] | None = None
        self.is_commander = False


class _Scalar:
    def __init__(self, v: Any) -> None:
        self._v = v

    def scalar_one_or_none(self) -> Any:
        return self._v

    def one_or_none(self) -> Any:
        if self._v is None:
            return None
        return _ScalarRow(self._v)


class _Conn:
    def __init__(self, row: Any) -> None:
        self._row = row

    async def execute(self, *_: Any, **__: Any) -> _Scalar:
        return _Scalar(self._row)


class _Ctx:
    def __init__(self, c: _Conn) -> None:
        self._c = c

    async def __aenter__(self) -> _Conn:
        return self._c

    async def __aexit__(self, *_: object) -> None:
        return None


class _Engine:
    def __init__(self, c: _Conn) -> None:
        self._c = c

    def connect(self) -> _Ctx:
        return _Ctx(self._c)


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=settings())


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _assert_invalid_projection(body: dict[str, Any]) -> None:
    assert body["object"] == "error"
    assert body["status"] == 422
    assert body["code"] == "invalid_projection"
    assert isinstance(body.get("details"), str) and body["details"]


# ---- unknown top-level key ----


async def test_card_by_id_unknown_top_level_key_422(app: FastAPI) -> None:
    app.dependency_overrides[get_read_engine] = lambda: _Engine(_Conn(RAW))
    async for c in _client(app):
        resp = await c.get(
            f"/cards/{SCRY_UUID}", params={"projection": "not_a_real_key"}
        )
    assert resp.status_code == 422
    _assert_invalid_projection(resp.json())


async def test_named_exact_unknown_top_level_key_422(app: FastAPI) -> None:
    app.dependency_overrides[get_read_engine] = lambda: _Engine(_Conn(RAW))
    async for c in _client(app):
        resp = await c.get(
            "/cards/named", params={"exact": "Llanowar", "projection": "nope"}
        )
    assert resp.status_code == 422
    _assert_invalid_projection(resp.json())


async def test_named_fuzzy_unknown_top_level_key_422(app: FastAPI) -> None:
    app.dependency_overrides[get_read_engine] = lambda: _Engine(_Conn(RAW))
    async for c in _client(app):
        resp = await c.get(
            "/cards/named", params={"fuzzy": "llanowar", "projection": "nope"}
        )
    assert resp.status_code == 422
    _assert_invalid_projection(resp.json())


async def test_search_unknown_top_level_key_422(app: FastAPI) -> None:
    rows = [make_row(RAW, total=1)]
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(FakeConn(rows=rows))
    async for c in _client(app):
        resp = await c.get(
            "/cards/search", params={"q": "elf", "projection": "nope"}
        )
    assert resp.status_code == 422
    _assert_invalid_projection(resp.json())


# ---- oversize depth ----


async def test_card_by_id_oversize_depth_422(app: FastAPI) -> None:
    app.dependency_overrides[get_read_engine] = lambda: _Engine(_Conn(RAW))
    async for c in _client(app):
        # 7 segments — exceeds cap of 6.
        resp = await c.get(
            f"/cards/{SCRY_UUID}",
            params={"projection": "legalities.a.b.c.d.e.f"},
        )
    assert resp.status_code == 422
    _assert_invalid_projection(resp.json())


async def test_search_oversize_depth_422(app: FastAPI) -> None:
    rows = [make_row(RAW, total=1)]
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(FakeConn(rows=rows))
    async for c in _client(app):
        resp = await c.get(
            "/cards/search",
            params={"q": "elf", "projection": "legalities.a.b.c.d.e.f"},
        )
    assert resp.status_code == 422
    _assert_invalid_projection(resp.json())


# ---- empty / malformed paths ----


async def test_card_by_id_empty_segment_422(app: FastAPI) -> None:
    app.dependency_overrides[get_read_engine] = lambda: _Engine(_Conn(RAW))
    async for c in _client(app):
        resp = await c.get(
            f"/cards/{SCRY_UUID}", params={"projection": "card_faces..name"}
        )
    assert resp.status_code == 422
    _assert_invalid_projection(resp.json())


async def test_card_by_id_trailing_comma_422(app: FastAPI) -> None:
    app.dependency_overrides[get_read_engine] = lambda: _Engine(_Conn(RAW))
    async for c in _client(app):
        resp = await c.get(
            f"/cards/{SCRY_UUID}", params={"projection": "name,"}
        )
    assert resp.status_code == 422
    _assert_invalid_projection(resp.json())
