"""D6.1 — ``GET /cards/{id}/similar`` core wiring per §6.3.

Vector-similarity lookup: target row's ``embedding`` drives a cosine-
distance ORDER BY against the rest of the table, ``id`` excluded so a
card never matches itself, ``embedding IS NOT NULL`` so unembedded rows
are skipped. Two-step query path:

  1. ``SELECT embedding FROM cards WHERE id = :id`` — pulls the target
     vector. (D6.2 turns "no row" / NULL into a 404.)
  2. ``SELECT id, raw, embedding <=> :target AS distance FROM cards
     WHERE id <> :id AND embedding IS NOT NULL
     ORDER BY embedding <=> :target LIMIT :limit`` — neighbour fetch
     ordered ascending by cosine distance (closer first).

D6.1's scope is just the happy path: route exists, both queries run,
results come back. D6.2 layers in validation + 404; D6.3 layers in the
List envelope with ``similarity`` annotation.
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

# ---------------------------------------------------------------------------
# Two-step fake engine: first execute() returns the target embedding
# scalar; second execute() returns the neighbour rows via mappings().all().
# Mirrors the existing FakeConn pattern in `_search_helpers` / the M2 file
# but tailored to D6.1's call shape.
# ---------------------------------------------------------------------------


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


class FakeSimilarConn:
    """Two-call execute fake.

    The router issues exactly two SQL statements per request: the target
    embedding lookup, then the neighbour fetch. We script both responses
    up-front and capture every ``(stmt, params)`` pair so tests can assert
    on the rendered SQL and bound parameters.
    """

    def __init__(
        self,
        *,
        target_embedding: Any,
        neighbours: list[dict[str, Any]],
    ) -> None:
        self.target = target_embedding
        self.neighbours = neighbours
        self.executed: list[tuple[Any, Any]] = []
        self._call = 0

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.executed.append((stmt, params))
        self._call += 1
        if self._call == 1:
            return _ScalarResult(self.target)
        return _MappingsResult(self.neighbours)


class _Ctx:
    def __init__(self, conn: FakeSimilarConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeSimilarConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeEngine:
    def __init__(self, conn: FakeSimilarConn) -> None:
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


def _override(app: FastAPI, conn: FakeSimilarConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


SCRY_UUID = "0000ec70-1c4e-4ad5-8d77-d3963fa1fa42"
OTHER_UUID = "1111ec70-1c4e-4ad5-8d77-d3963fa1fa42"


def _embedding() -> list[float]:
    """Fixed 384-dim unit vector for the target row."""
    return [0.1] * 384


def _neighbour(id_: str, name: str, distance: float) -> dict[str, Any]:
    return {
        "id": id_,
        "raw": {"object": "card", "id": id_, "name": name},
        "distance": distance,
        "predicted_labels": [],
        "label_scores": {},
    }


# ---------------------------------------------------------------------------
# happy path — route exists, query shape correct
# ---------------------------------------------------------------------------


async def test_similar_returns_200(app: FastAPI) -> None:
    """Route is registered and returns 200 for a card with neighbours."""
    conn = FakeSimilarConn(
        target_embedding=_embedding(),
        neighbours=[_neighbour(OTHER_UUID, "Elvish Mystic", 0.05)],
    )
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    assert resp.status_code == 200


async def test_similar_issues_two_queries(app: FastAPI) -> None:
    """The endpoint issues exactly two SQL round-trips (target + neighbours)."""
    conn = FakeSimilarConn(
        target_embedding=_embedding(),
        neighbours=[_neighbour(OTHER_UUID, "Elvish Mystic", 0.05)],
    )
    _override(app, conn)

    async for client in _client(app):
        await client.get(f"/cards/{SCRY_UUID}/similar")

    assert len(conn.executed) == 2


async def test_similar_target_query_uses_id_param(app: FastAPI) -> None:
    """First query selects ``embedding`` for the target ``:id`` param."""
    conn = FakeSimilarConn(
        target_embedding=_embedding(),
        neighbours=[_neighbour(OTHER_UUID, "Elvish Mystic", 0.05)],
    )
    _override(app, conn)

    async for client in _client(app):
        await client.get(f"/cards/{SCRY_UUID}/similar")

    target_sql = str(conn.executed[0][0]).lower()
    target_params = conn.executed[0][1]
    assert "embedding" in target_sql
    assert "from cards" in target_sql
    assert "where" in target_sql and "id" in target_sql
    assert target_params is not None
    assert SCRY_UUID in target_params.values()


async def test_similar_neighbour_query_uses_cosine_operator(app: FastAPI) -> None:
    """Second query uses pgvector ``<=>`` operator + filters id<> and NOT NULL."""
    conn = FakeSimilarConn(
        target_embedding=_embedding(),
        neighbours=[_neighbour(OTHER_UUID, "Elvish Mystic", 0.05)],
    )
    _override(app, conn)

    async for client in _client(app):
        await client.get(f"/cards/{SCRY_UUID}/similar")

    sql = str(conn.executed[1][0]).lower()
    # cosine distance op
    assert "<=>" in sql
    # exclude self
    assert "id <>" in sql or "id !=" in sql
    # skip unembedded
    assert "embedding is not null" in sql
    # ordered by distance ascending (closer first)
    assert "order by" in sql
    # bounded by limit
    assert "limit" in sql


async def test_similar_neighbour_query_binds_target_and_self_id(app: FastAPI) -> None:
    """Second query carries the target ``:id`` and the limit; row exclusion uses self id."""
    conn = FakeSimilarConn(
        target_embedding=_embedding(),
        neighbours=[_neighbour(OTHER_UUID, "Elvish Mystic", 0.05)],
    )
    _override(app, conn)

    async for client in _client(app):
        await client.get(f"/cards/{SCRY_UUID}/similar")

    params = conn.executed[1][1]
    assert params is not None
    # The self-id must reach the WHERE id <> binding so we don't return
    # the card itself in its own neighbour list.
    assert SCRY_UUID in params.values()
