"""Tests for ``GET /health`` per tech_spec.md §6.6."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex import __version__
from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.health import get_read_engine


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def one(self) -> Any:
        return self._value


class _FakeConn:
    """SQL-dispatched fake: routes on statement text + bound params.

    Supports the four queries /health issues: ping, cards+embeddings
    count, and two pg_class reltuples lookups (oracle_tags, combos).
    Per-table estimate outcomes can be an int, ``None`` (row absent),
    or an ``Exception`` (raise it — helper should catch, rollback, and
    yield ``None`` without cascading to the next call).
    """

    def __init__(
        self,
        *,
        ping: Any = 1,
        fail_ping: bool = False,
        cards_count: int = 0,
        embeddings_count: int = 0,
        estimates: dict[str, Any] | None = None,
        cards_floor: int = 0,
    ) -> None:
        self._cards_floor = cards_floor
        self._ping = ping
        self._fail_ping = fail_ping
        self._cards_count = cards_count
        self._embeddings_count = embeddings_count
        # Missing entry -> row absent -> None. Present entry may be int,
        # None (explicit "row absent"), a negative int (pre-ANALYZE), or
        # an Exception to raise.
        self._estimates = estimates or {}
        self.estimate_calls: list[str] = []
        self.rollbacks = 0

    async def execute(
        self, stmt: Any, params: dict[str, Any] | None = None
    ) -> _FakeResult:
        sql = str(stmt)
        if "pg_class" in sql:
            assert params is not None
            table = params["t"]
            self.estimate_calls.append(table)
            outcome = self._estimates.get(table)
            if isinstance(outcome, Exception):
                raise outcome
            return _FakeResult(outcome)
        if "LIMIT" in sql and "count(*)" in sql:
            return _FakeResult(self._cards_floor)
        if "FROM cards" in sql:
            return _FakeResult((self._cards_count, self._embeddings_count))
        # ping
        if self._fail_ping:
            raise RuntimeError("db down")
        return _FakeResult(self._ping)

    async def rollback(self) -> None:
        self.rollbacks += 1


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


async def test_health_ok(app: FastAPI) -> None:
    """DB up: 200, body matches §6.6 shape, refresh placeholder all null/false.

    D16.4: cards_count now sourced from pg_class.reltuples; embeddings_count
    is null on the reltuples path (dropped to avoid a full scan).
    """
    conn = _FakeConn(
        estimates={"cards": 42, "oracle_tags": 128, "combos": 7},
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "db": "up",
        "version": __version__,
        "cards_count": 42,
        "oracle_tags_count": 128,
        "embeddings_count": None,
        "combos_count": 7,
        "refresh": {
            "running": False,
            "started_at": None,
            "job_id": None,
            "stages": [],
            "planned_stages": [],
        },
    }


async def test_health_db_down(app: FastAPI) -> None:
    """DB ping fails: 503, status=error, db=down, counts=null."""
    conn = _FakeConn(fail_ping=True)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "error"
    assert body["db"] == "down"
    assert body["cards_count"] is None
    assert body["oracle_tags_count"] is None
    assert body["embeddings_count"] is None
    assert body["combos_count"] is None
    # Version is static, so it still answers when the DB is down.
    assert body["version"] == __version__


async def test_health_table_absent_yields_null(app: FastAPI) -> None:
    """Table missing from pg_class → estimate is null, /health stays 200."""
    # No entry in `estimates` → scalar_one_or_none returns None → helper -> None.
    conn = _FakeConn(cards_count=42, estimates={"combos": 5})
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["oracle_tags_count"] is None
    assert body["combos_count"] == 5


async def test_health_reltuples_negative_yields_null(app: FastAPI) -> None:
    """Pre-ANALYZE tables report reltuples=-1; helper must surface null."""
    conn = _FakeConn(
        cards_count=42, estimates={"oracle_tags": -1, "combos": -1}
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["oracle_tags_count"] is None
    assert body["combos_count"] is None


async def test_health_cards_count_unanalysed_empty_is_zero(app: FastAPI) -> None:
    """F2: reltuples=-1 on an empty cards table must read 0, not null."""
    conn = _FakeConn(estimates={"cards": -1}, cards_floor=0)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health")

    assert resp.json()["cards_count"] == 0


async def test_health_cards_count_unanalysed_populated_is_nonzero(
    app: FastAPI,
) -> None:
    """F2: reltuples=-1 right after ingest (before autovacuum) must not read null/0."""
    conn = _FakeConn(estimates={"cards": -1}, cards_floor=1000)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health")

    assert resp.json()["cards_count"] == 1000


async def test_health_cards_count_uses_reltuples_estimate(app: FastAPI) -> None:
    """D16.4: /health must not seq-scan `cards` — cards_count comes from
    the same `pg_class.reltuples` estimate path used for tags/combos.
    """
    conn = _FakeConn(
        estimates={"cards": 30000, "oracle_tags": 128, "combos": 7},
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["cards_count"] == 30000
    # embeddings_count dropped from the reltuples path — agents rarely
    # poll it and the FILTER would force a full scan.
    assert body["embeddings_count"] is None
    assert "cards" in conn.estimate_calls


async def test_health_embeddings_count_from_embed_stage(app: FastAPI) -> None:
    """D16.8: /health `embeddings_count` sourced from the last embed stage's rows.

    D16.4 dropped the `count(*) FILTER (WHERE embedding IS NOT NULL)`
    scan and left `embeddings_count` permanently null. The refresh
    telemetry already records `stages[embed].rows` from the embed body
    — piggy-back on it so ops still get a live count without paying
    for a full scan on every readiness probe.
    """
    import biblioplex.ingest.refresh as refresh_mod

    status: refresh_mod.RefreshStatus = app.state.refresh_status
    status.mark_started("job-abc")
    with status.stage("embed") as st:
        st.rows = 12345
    status.mark_finished()

    conn = _FakeConn(estimates={"cards": 30000, "oracle_tags": 128, "combos": 7})
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["embeddings_count"] == 12345


async def test_health_embeddings_count_null_before_first_embed_stage(
    app: FastAPI,
) -> None:
    """No completed embed stage yet → `embeddings_count` stays null.

    Fresh boot, or a refresh that ran a subset of stages excluding
    `embed`, must not surface stale or synthetic embedding counts.
    """
    conn = _FakeConn(estimates={"cards": 30000, "oracle_tags": 128, "combos": 7})
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["embeddings_count"] is None


async def test_health_estimate_failure_does_not_cascade(app: FastAPI) -> None:
    """First estimate raising must roll back and let the second still run.

    Regression: pre-fix, a statement_timeout on the oracle_tags count
    aborted the txn and the combos count fell over with
    ``InFailedSqlTransaction``. With rollback in the helper, the combos
    estimate still returns cleanly.
    """
    conn = _FakeConn(
        cards_count=42,
        estimates={"oracle_tags": RuntimeError("boom"), "combos": 7},
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["oracle_tags_count"] is None
    assert body["combos_count"] == 7
    assert conn.rollbacks == 1
    # D16.4: `cards` is now the first estimate in the chain.
    assert conn.estimate_calls == ["cards", "oracle_tags", "combos"]


async def test_health_exposes_planned_stages(app: FastAPI) -> None:
    """The UI needs the run's selected stages to show "N of M steps"."""
    conn = _FakeConn(estimates={"cards": 1, "oracle_tags": 1, "combos": 1})
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    app.state.refresh_status.mark_started("job-1")
    app.state.refresh_status.set_planned(("ingest", "embed"))

    async for client in _client(app):
        resp = await client.get("/health")

    assert resp.json()["refresh"]["planned_stages"] == ["ingest", "embed"]
