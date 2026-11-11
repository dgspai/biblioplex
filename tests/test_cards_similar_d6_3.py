"""D6.3 — List envelope + ``similarity = 1 - distance`` annotation (§6.3).

Final response shape::

    {
      "object": "list",
      "total_cards": <len(data)>,
      "has_more": false,
      "next_page": null,
      "data": [
        { ...raw card payload..., "similarity": <float in [0, 1]> },
        ...
      ]
    }

Similar (M6) has no pagination — ``has_more`` is always ``false`` and
``next_page`` always ``null``. ``total_cards`` mirrors ``len(data)``
because the SQL ``LIMIT`` already bounds the result (drift §6.3:
spec doesn't explicitly say which definition to use, we pick the one
that doesn't lie to the agent — if 20 rows came back, total is 20).

Similarity is injected into each row's ``raw`` JSON before send,
matching Scryfall's own convention of decorating list entries with
endpoint-specific fields. ``similarity = 1 - distance`` because
pgvector's ``<=>`` returns cosine *distance* (0 = identical, 2 =
opposite) while agents reason about *similarity* (1 = identical,
-1 = opposite, but bounded to [0,1] for normalised embeddings).
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
A_UUID = "1111ec70-1c4e-4ad5-8d77-d3963fa1fa42"
B_UUID = "2222ec70-1c4e-4ad5-8d77-d3963fa1fa42"


def _embedding() -> list[float]:
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
# envelope
# ---------------------------------------------------------------------------


async def test_envelope_has_list_object(app: FastAPI) -> None:
    """Top-level ``object`` is ``"list"`` (Scryfall List envelope)."""
    conn = FakeConn(
        target_embedding=_embedding(),
        neighbours=[_neighbour(A_UUID, "Elvish Mystic", 0.05)],
    )
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    body = resp.json()
    assert body["object"] == "list"


async def test_envelope_total_cards_matches_data_length(app: FastAPI) -> None:
    """``total_cards`` equals ``len(data)`` (drift §6.3: LIMIT bounds it)."""
    conn = FakeConn(
        target_embedding=_embedding(),
        neighbours=[
            _neighbour(A_UUID, "Elvish Mystic", 0.05),
            _neighbour(B_UUID, "Fyndhorn Elves", 0.07),
        ],
    )
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    body = resp.json()
    assert body["total_cards"] == 2
    assert len(body["data"]) == 2


async def test_envelope_has_more_always_false(app: FastAPI) -> None:
    """No pagination on /similar → ``has_more`` is always ``false``."""
    conn = FakeConn(
        target_embedding=_embedding(),
        neighbours=[_neighbour(A_UUID, "Elvish Mystic", 0.05)],
    )
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    body = resp.json()
    assert body["has_more"] is False


async def test_envelope_next_page_always_null(app: FastAPI) -> None:
    """No pagination → ``next_page`` is always ``null``."""
    conn = FakeConn(
        target_embedding=_embedding(),
        neighbours=[_neighbour(A_UUID, "Elvish Mystic", 0.05)],
    )
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    body = resp.json()
    assert body["next_page"] is None


async def test_envelope_with_empty_neighbours(app: FastAPI) -> None:
    """Target exists but nothing else embedded → empty list, not 404.

    A real 404 only fires when the *target* row is missing or has a NULL
    embedding (D6.2). If the table genuinely has only one embedded row,
    the answer is "no neighbours" — empty list, 200 OK.
    """
    conn = FakeConn(target_embedding=_embedding(), neighbours=[])
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["total_cards"] == 0
    assert body["data"] == []
    assert body["has_more"] is False
    assert body["next_page"] is None


# ---------------------------------------------------------------------------
# similarity annotation
# ---------------------------------------------------------------------------


async def test_similarity_injected_into_each_row(app: FastAPI) -> None:
    """Every data entry carries ``similarity = 1 - distance``."""
    conn = FakeConn(
        target_embedding=_embedding(),
        neighbours=[
            _neighbour(A_UUID, "Elvish Mystic", 0.05),
            _neighbour(B_UUID, "Fyndhorn Elves", 0.20),
        ],
    )
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    body = resp.json()
    assert len(body["data"]) == 2
    # 1 - 0.05 = 0.95, 1 - 0.20 = 0.80
    assert body["data"][0]["similarity"] == pytest.approx(0.95)
    assert body["data"][1]["similarity"] == pytest.approx(0.80)


async def test_similarity_preserves_raw_payload(app: FastAPI) -> None:
    """The original raw fields are still present alongside ``similarity``."""
    raw = {
        "object": "card",
        "id": A_UUID,
        "name": "Llanowar Elves",
        "type_line": "Creature — Elf Druid",
    }
    conn = FakeConn(
        target_embedding=_embedding(),
        neighbours=[{
            "id": A_UUID,
            "raw": raw,
            "distance": 0.10,
            "predicted_labels": [],
            "label_scores": {},
        }],
    )
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    entry = resp.json()["data"][0]
    # raw fields survive
    assert entry["name"] == "Llanowar Elves"
    assert entry["type_line"] == "Creature — Elf Druid"
    assert entry["id"] == A_UUID
    assert entry["object"] == "card"
    # similarity annotation injected
    assert "similarity" in entry
    assert entry["similarity"] == pytest.approx(0.90)


async def test_similarity_for_identical_vectors_is_one(app: FastAPI) -> None:
    """``distance == 0`` → ``similarity == 1`` (sanity check on the formula)."""
    conn = FakeConn(
        target_embedding=_embedding(),
        neighbours=[_neighbour(A_UUID, "Twin", 0.0)],
    )
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    assert resp.json()["data"][0]["similarity"] == pytest.approx(1.0)


async def test_similarity_does_not_leak_distance(app: FastAPI) -> None:
    """``distance`` is an internal SQL column — it must not appear in the body.

    Agents see ``similarity`` only. Exposing both would invite confusion
    (which is the canonical signal? are they consistent?) and lock us
    into the cosine-distance choice if we ever swap metrics.
    """
    conn = FakeConn(
        target_embedding=_embedding(),
        neighbours=[_neighbour(A_UUID, "Elvish Mystic", 0.05)],
    )
    _override(app, conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}/similar")

    entry = resp.json()["data"][0]
    assert "distance" not in entry
