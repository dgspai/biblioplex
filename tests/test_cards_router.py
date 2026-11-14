"""Tests for ``GET /cards/:id`` (§6.2) and ``GET /cards/named`` (§6.4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine


class _FakeRow:
    """Stand-in for a SQLAlchemy ``Row`` with attribute access."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.oracle_id = "00000000-0000-0000-0000-000000000000"
        # Step 5: every card-fetching SELECT pulls these alongside `raw`.
        self.predicted_labels: list[str] = []
        self.label_scores: dict[str, float] = {}
        self.label_reasons: dict[str, Any] | None = None
        self.is_commander = raw.get("name") == "Atraxa" if isinstance(raw, dict) else False


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def one_or_none(self) -> Any:
        if self._value is None:
            return None
        return _FakeRow(self._value)


class _FakeConn:
    def __init__(self, row: Any) -> None:
        self._row = row
        self.last_stmt: Any = None
        self.last_params: Any = None

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        self.last_stmt = stmt
        self.last_params = params
        return _FakeResult(self._row)


class _FakeConnCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeEngine:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self) -> _FakeConnCtx:
        return _FakeConnCtx(self._conn)


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


SCRY_UUID = "0000ec70-1c4e-4ad5-8d77-d3963fa1fa42"


async def test_card_by_id_returns_raw(app: FastAPI) -> None:
    """Existing card: 200, body is the raw Scryfall payload + Step-5 fields."""
    raw = {
        "object": "card",
        "id": SCRY_UUID,
        "name": "Llanowar Elves",
        "type_line": "Creature — Elf Druid",
        "oracle_text": "{T}: Add {G}.",
    }
    conn = _FakeConn(row=raw)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}")

    assert resp.status_code == 200
    assert resp.json() == {
        **raw, "predicted_labels": [], "label_scores": {},
        "labels_overridden": False, "is_commander": False,
    }


async def test_card_by_id_says_whether_it_can_lead_a_deck(app: FastAPI) -> None:
    """The detail pane offers "start deck" only for commander-eligible cards."""
    conn = _FakeConn(row={"object": "card", "id": SCRY_UUID, "name": "Atraxa"})
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}")

    assert resp.json()["is_commander"] is True


async def test_card_by_id_is_no_cache_and_ignores_if_none_match(app: FastAPI) -> None:
    """Card payload carries label overrides, so no ETag / 304: always fresh."""
    raw = {"object": "card", "id": SCRY_UUID, "name": "Llanowar Elves"}
    conn = _FakeConn(row=raw)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.get(
            f"/cards/{SCRY_UUID}", headers={"If-None-Match": 'W/"x"'}
        )
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert "etag" not in resp.headers


async def test_card_by_id_missing_omits_cache_headers(app: FastAPI) -> None:
    """404 must not be cached — no Cache-Control on the error envelope."""
    conn = _FakeConn(row=None)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}")
    assert resp.status_code == 404
    assert "cache-control" not in {k.lower() for k in resp.headers}


async def test_card_by_id_missing_returns_404(app: FastAPI) -> None:
    """No row: 404 with Scryfall-shaped error body."""
    conn = _FakeConn(row=None)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get(f"/cards/{SCRY_UUID}")

    assert resp.status_code == 404
    body = resp.json()
    assert body["object"] == "error"
    assert body["status"] == 404
    assert body["code"] == "not_found"
    assert "details" in body and isinstance(body["details"], str)


async def test_card_by_id_invalid_uuid_returns_422(app: FastAPI) -> None:
    """Malformed UUID path param: FastAPI auto-validates → 422."""
    # No dependency override — request rejected before DB layer touched.
    async for client in _client(app):
        resp = await client.get("/cards/not-a-uuid")

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# §6.4 — GET /cards/named?exact=...
# ---------------------------------------------------------------------------


async def test_named_exact_returns_raw(app: FastAPI) -> None:
    """exact=<name>: 200 with the matched card's raw payload."""
    raw = {
        "object": "card",
        "id": SCRY_UUID,
        "name": "Llanowar Elves",
        "type_line": "Creature — Elf Druid",
    }
    conn = _FakeConn(row=raw)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/cards/named", params={"exact": "Llanowar Elves"})

    assert resp.status_code == 200
    assert resp.json() == {**raw, "predicted_labels": [], "label_scores": {}}
    # Payload carries label overrides: never cached, no validator.
    assert resp.headers["cache-control"] == "no-cache"
    assert "etag" not in resp.headers


async def test_named_exact_is_case_insensitive(app: FastAPI) -> None:
    """exact match uses LOWER(name) = LOWER($1) — casing must not matter.

    We assert the endpoint hits the DB (matched row returned) regardless
    of input casing. The actual case-fold happens in SQL; the fake just
    proves the request reached the query layer with the user's input.
    """
    raw = {"object": "card", "id": SCRY_UUID, "name": "Llanowar Elves"}
    conn = _FakeConn(row=raw)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/cards/named", params={"exact": "LLANOWAR ELVES"})

    assert resp.status_code == 200
    assert resp.json() == {**raw, "predicted_labels": [], "label_scores": {}}
    # Confirm the user's raw query string was bound — case-folding is the
    # DB's job (LOWER on both sides), not Python's.
    assert conn.last_params is not None
    assert "LLANOWAR ELVES" in conn.last_params.values()


async def test_named_exact_missing_returns_404(app: FastAPI) -> None:
    """No match: 404 with Scryfall-shaped error body."""
    conn = _FakeConn(row=None)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/cards/named", params={"exact": "Nonesuch"})

    assert resp.status_code == 404
    body = resp.json()
    assert body["object"] == "error"
    assert body["status"] == 404
    assert body["code"] == "not_found"


# ---------------------------------------------------------------------------
# §6.4 — GET /cards/named?fuzzy=...
# ---------------------------------------------------------------------------


async def test_named_fuzzy_returns_best_match(app: FastAPI) -> None:
    """fuzzy=<name>: 200 with best-match raw above the 0.4 threshold.

    The DB picks the row, ORDER BY similarity DESC LIMIT 1. The router
    just unwraps the raw column from the single returned row.
    """
    raw = {"object": "card", "id": SCRY_UUID, "name": "Llanowar Elves"}
    conn = _FakeConn(row=raw)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/cards/named", params={"fuzzy": "llanowar elf"})

    assert resp.status_code == 200
    assert resp.json() == {**raw, "predicted_labels": [], "label_scores": {}}
    # Confirm the user's query and threshold both reached the DB.
    assert conn.last_params is not None
    assert "llanowar elf" in conn.last_params.values()
    # Spec-mandated threshold (0.4) must be hard-wired into the query
    # plan, not negotiated client-side.
    stmt_text = str(conn.last_stmt)
    assert "0.4" in stmt_text or ":threshold" in stmt_text


async def test_named_fuzzy_below_threshold_returns_404(app: FastAPI) -> None:
    """Nothing above 0.4: empty result set → 404."""
    conn = _FakeConn(row=None)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/cards/named", params={"fuzzy": "qzxqzx"})

    assert resp.status_code == 404
    body = resp.json()
    assert body["object"] == "error"
    assert body["code"] == "not_found"


async def test_named_fuzzy_uses_name_sort_key_and_similarity(app: FastAPI) -> None:
    """Query must use pg_trgm similarity() against name_sort_key per §6.4."""
    raw = {"object": "card", "id": SCRY_UUID, "name": "Llanowar Elves"}
    conn = _FakeConn(row=raw)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        await client.get("/cards/named", params={"fuzzy": "llanowar"})

    stmt_text = str(conn.last_stmt).lower()
    assert "similarity" in stmt_text
    assert "name_sort_key" in stmt_text


async def test_named_fuzzy_lowers_input_for_trigram(app: FastAPI) -> None:
    """Bug.5: fuzzy query wraps `:name` in `LOWER(...)`.

    pg_trgm is case-sensitive; `name_sort_key` is lower-cased at ingest.
    Mixed-case decklist entries otherwise score lower than case-folded
    twins. Router must lower the query side in SQL.
    """
    raw = {"object": "card", "id": SCRY_UUID, "name": "Llanowar Elves"}
    conn = _FakeConn(row=raw)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        await client.get("/cards/named", params={"fuzzy": "LLANOWAR"})

    stmt_text = str(conn.last_stmt)
    assert "LOWER(:name)" in stmt_text


# ---------------------------------------------------------------------------
# §6.4 — exactly-one-of(exact, fuzzy) validation
# ---------------------------------------------------------------------------


def _expect_bad_request_body(body: dict[str, Any]) -> None:
    """Shared assertion for the §6.4 400 envelope."""
    assert body["object"] == "error"
    assert body["status"] == 400
    assert body["code"] == "bad_request"
    assert body["details"] == "exactly one of exact|fuzzy required"


async def test_named_neither_param_returns_400(app: FastAPI) -> None:
    """No exact, no fuzzy → 400 with the spec-mandated error body.

    A bare /cards/named call has no way to disambiguate intent. The DB
    must not be hit — wire a fake that explodes if execute() runs.
    """

    class _Exploder:
        async def execute(self, *_: Any, **__: Any) -> Any:
            raise AssertionError("DB must not be hit when validation rejects")

    class _ExplodingEngine:
        def connect(self) -> _FakeConnCtx:
            return _FakeConnCtx(_Exploder())  # type: ignore[arg-type]

    app.dependency_overrides[get_read_engine] = lambda: _ExplodingEngine()

    async for client in _client(app):
        resp = await client.get("/cards/named")

    assert resp.status_code == 400
    _expect_bad_request_body(resp.json())


async def test_named_both_params_returns_400(app: FastAPI) -> None:
    """Both exact and fuzzy → 400 (ambiguous intent, spec rejects)."""

    class _Exploder:
        async def execute(self, *_: Any, **__: Any) -> Any:
            raise AssertionError("DB must not be hit when validation rejects")

    class _ExplodingEngine:
        def connect(self) -> _FakeConnCtx:
            return _FakeConnCtx(_Exploder())  # type: ignore[arg-type]

    app.dependency_overrides[get_read_engine] = lambda: _ExplodingEngine()

    async for client in _client(app):
        resp = await client.get(
            "/cards/named", params={"exact": "Foo", "fuzzy": "Bar"}
        )

    assert resp.status_code == 400
    _expect_bad_request_body(resp.json())
