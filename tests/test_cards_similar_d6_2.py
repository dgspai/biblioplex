"""D6.2 — ``limit`` bounds + 404 on missing target / NULL embedding (§6.3).

* ``limit`` is an integer in ``[1, 100]`` with default ``20``. Out-of-
  range values are rejected by FastAPI's ``Query`` validator with the
  standard 422 envelope.
* The base-card lookup returns 404 (Scryfall-shaped) when either:
  - no row matches ``:id``, or
  - the row exists but its ``embedding`` is NULL (un-embedded yet).
  Both surface as the same ``not_found`` body — the spec doesn't
  differentiate, and the agent contract is "no neighbours available".
* When the target is absent we must **not** issue the second neighbour
  query — short-circuit before doing extra DB work.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine


class _TargetRow:
    def __init__(
        self,
        embedding: Any,
        oracle_search: str = "",
        cmc: Any = None,
        color_identity: list[str] | None = None,
        type_search: str = "",
        oracle_tags: list[str] | None = None,
    ) -> None:
        self.embedding = embedding
        self.oracle_search = oracle_search
        self.cmc = cmc
        self.color_identity = color_identity
        self.type_search = type_search
        self.oracle_tags = oracle_tags


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def one_or_none(self) -> Any:
        return None if self._value is None else _TargetRow(self._value)


class _MappingsResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingsResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class FakeConn:
    """Two-call fake: first execute() → scalar target; subsequent → rows."""

    def __init__(
        self,
        *,
        target_embedding: Any,
        neighbours: list[dict[str, Any]] | None = None,
    ) -> None:
        self.target = target_embedding
        self.neighbours = neighbours or []
        self.executed: list[tuple[Any, Any]] = []
        self._call = 0

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.executed.append((stmt, params))
        self._call += 1
        if self._call == 1:
            return _ScalarResult(self.target)
        return _MappingsResult(self.neighbours)


class _Ctx:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeEngine:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    def connect(self) -> _Ctx:
        return _Ctx(self._conn)


def _settings() -> Settings:
    return Settings.model_construct(
        database_url="postgresql+psycopg://x/x",
        hf_embed_model="x",
        hf_embed_dim=384,
        hf_embed_device="cpu",
        embed_batch=256,
        bulk_source_type="default_cards",
        web_concurrency=1,
        cors_origins="*",
        log_level="INFO",
    )


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=_settings())


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


SCRY_UUID = "0000ec70-1c4e-4ad5-8d77-d3963fa1fa42"
OTHER_UUID = "1111ec70-1c4e-4ad5-8d77-d3963fa1fa42"


def _embedding() -> list[float]:
    return [0.1] * 384


# ---------------------------------------------------------------------------
# 404 — missing base card
# ---------------------------------------------------------------------------


async def test_missing_target_returns_404(app: FastAPI) -> None:
    """No row for :id → 404 Scryfall envelope, no second query."""
    conn = FakeConn(target_embedding=None)
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    assert resp.status_code == 404
    body = resp.json()
    assert body["object"] == "error"
    assert body["status"] == 404
    assert body["code"] == "not_found"
    # Short-circuit: no neighbour query should have been issued.
    assert len(conn.executed) == 1


async def test_null_embedding_returns_404(app: FastAPI) -> None:
    """Row exists but ``embedding IS NULL`` → 404 same envelope, no neighbours fetched.

    ``scalar_one_or_none()`` returns the NULL value as Python ``None``,
    indistinguishable from "no row" — both legitimately mean "no vector
    to query with". Spec collapses them onto the same status.
    """
    conn = FakeConn(target_embedding=None)
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    assert resp.status_code == 404
    assert len(conn.executed) == 1


# ---------------------------------------------------------------------------
# limit bounds
# ---------------------------------------------------------------------------


async def test_limit_default_is_20(app: FastAPI) -> None:
    """No ``?limit=`` → bind 20 (§6.3 default)."""
    conn = FakeConn(target_embedding=_embedding(), neighbours=[])
    _override(app, conn)

    async for client in _client(app):
        await client.get(f"/cards/{SCRY_UUID}/similar")

    neighbour_params = conn.executed[1][1]
    assert neighbour_params is not None
    assert neighbour_params.get("limit") == 20


async def test_limit_explicit_value_passed_through(app: FastAPI) -> None:
    """``?limit=50`` reaches the bound :limit param of the neighbour query."""
    conn = FakeConn(target_embedding=_embedding(), neighbours=[])
    _override(app, conn)

    async for client in _client(app):
        await client.get(f"/cards/{SCRY_UUID}/similar?limit=50")

    neighbour_params = conn.executed[1][1]
    assert neighbour_params is not None
    assert neighbour_params.get("limit") == 50


async def test_limit_boundary_1_accepted(app: FastAPI) -> None:
    """``?limit=1`` is the lower bound — accepted."""
    conn = FakeConn(target_embedding=_embedding(), neighbours=[])
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar?limit=1")

    assert resp.status_code == 200


async def test_limit_boundary_100_accepted(app: FastAPI) -> None:
    """``?limit=100`` is the upper bound — accepted."""
    conn = FakeConn(target_embedding=_embedding(), neighbours=[])
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar?limit=100")

    assert resp.status_code == 200


async def test_limit_zero_rejected_with_422(app: FastAPI) -> None:
    """``?limit=0`` is below the floor → 422."""
    conn = FakeConn(target_embedding=_embedding(), neighbours=[])
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar?limit=0")

    assert resp.status_code == 422


async def test_limit_negative_rejected_with_422(app: FastAPI) -> None:
    """Negative ``?limit=`` → 422."""
    conn = FakeConn(target_embedding=_embedding(), neighbours=[])
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar?limit=-3")

    assert resp.status_code == 422


async def test_limit_over_100_rejected_with_422(app: FastAPI) -> None:
    """``?limit=101`` exceeds the ceiling → 422."""
    conn = FakeConn(target_embedding=_embedding(), neighbours=[])
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar?limit=101")

    assert resp.status_code == 422


async def test_limit_non_integer_rejected_with_422(app: FastAPI) -> None:
    """Non-integer ``?limit=`` (FastAPI int validator) → 422."""
    conn = FakeConn(target_embedding=_embedding(), neighbours=[])
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar?limit=abc")

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# invalid UUID — FastAPI path validator stops the request before we run
# ---------------------------------------------------------------------------


async def test_invalid_uuid_returns_422(app: FastAPI) -> None:
    """Malformed UUID in the path → FastAPI 422, no DB hit."""
    conn = FakeConn(target_embedding=_embedding(), neighbours=[])
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get("/cards/not-a-uuid/similar")

    assert resp.status_code == 422
    assert conn.executed == []
