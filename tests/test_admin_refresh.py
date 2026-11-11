"""Tests for `POST /admin/refresh` (D1.7, tech_spec.md §6.5)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.ingest.refresh import RefreshRunner, RefreshStatus
from biblioplex.main import create_app
from biblioplex.routers.admin import get_refresh_runner, get_refresh_status
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
    def __init__(self, *, lock_granted: bool = True) -> None:
        self.lock_granted = lock_granted
        self.executed: list[str] = []

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _FakeConn:
        return self

    async def execution_options(self, **_kw: Any) -> _FakeConn:
        # D13.7 concurrent-build path.
        return self

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append(sql)
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(self.lock_granted)
        if "SELECT 1" in sql:
            return _FakeResult(1)
        if "FROM cards" in sql and "FILTER" in sql:
            # cards+embeddings query — health.py unpacks via `.one()`.
            return _FakeResult((0, 0))
        if "count(*)" in sql:
            return _FakeResult(0)
        return _FakeResult(None)


class _FakeEngine:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def begin(self) -> _FakeConn:
        return self._conn

    def connect(self) -> _FakeConn:
        return self._conn


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


async def test_refresh_returns_started_with_uuid(app: FastAPI) -> None:
    """First POST returns 202 + status=started + a UUID4 job_id.

    Note: FastAPI's BackgroundTasks run AFTER the response body is sent but
    BEFORE the ASGI test transport hands control back to the caller. So by
    the time `await client.post(...)` returns, the background task has
    already completed — status is back to running=False with job_id
    retained. We assert on the persisted job_id, not on running=True.
    """
    status = RefreshStatus()

    async def quick_ingest(_engine: Any) -> int:
        return 0

    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    runner = RefreshRunner(engine=engine, status=status, ingest=quick_ingest)

    app.dependency_overrides[get_refresh_status] = lambda: status
    app.dependency_overrides[get_refresh_runner] = lambda: runner
    app.dependency_overrides[get_read_engine] = lambda: engine

    async for client in _client(app):
        resp = await client.post("/admin/refresh")

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "started"
    # Valid UUID4 string.
    uuid_obj = uuid.UUID(body["job_id"])
    assert uuid_obj.version == 4

    # After the background task drains, status carries the run's job_id.
    snap = status.snapshot()
    assert snap.job_id == body["job_id"]
    assert snap.running is False


async def test_concurrent_second_refresh_returns_already_running(app: FastAPI) -> None:
    """While a refresh is mid-flight, a second POST returns the original job_id."""
    status = RefreshStatus()
    # Pre-mark status as running with a known job_id to simulate an
    # in-flight refresh from a prior call.
    original_job_id = "00000000-0000-4000-8000-000000000abc"
    status.mark_started(original_job_id)

    async def noop_ingest(_engine: Any) -> int:
        return 0

    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    runner = RefreshRunner(engine=engine, status=status, ingest=noop_ingest)

    app.dependency_overrides[get_refresh_status] = lambda: status
    app.dependency_overrides[get_refresh_runner] = lambda: runner
    app.dependency_overrides[get_read_engine] = lambda: engine

    async for client in _client(app):
        resp = await client.post("/admin/refresh")

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "already_running"
    assert body["job_id"] == original_job_id


async def test_health_reflects_refresh_status(app: FastAPI) -> None:
    """/health surfaces real RefreshStatus snapshot (not the M0 placeholder)."""
    # Replace the app's status singleton with one we control. Both /health
    # and /admin/refresh read from app.state.refresh_status (via their own
    # `get_refresh_status` deps), so swapping it covers both call sites.
    status = RefreshStatus()
    status.mark_started("11111111-1111-4111-8111-111111111111")
    app.state.refresh_status = status

    conn = _FakeConn()
    engine = _FakeEngine(conn)

    app.dependency_overrides[get_read_engine] = lambda: engine

    async for client in _client(app):
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["refresh"]["running"] is True
    assert body["refresh"]["job_id"] == "11111111-1111-4111-8111-111111111111"
    assert body["refresh"]["started_at"] is not None


async def test_refresh_stage_subset_runs_only_selected(app: FastAPI) -> None:
    """`?stages=combos,tags` only invokes those two stage callables."""
    status = RefreshStatus()
    called: list[str] = []

    async def track(name: str, _engine: Any) -> int:
        called.append(name)
        return 0

    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    runner = RefreshRunner(
        engine=engine,
        status=status,
        ingest=lambda e: track("ingest", e),
        embed=lambda e: track("embed", e),
        tags=lambda e: track("tags", e),
        classify=lambda e: track("classify", e),
        combos=lambda e: track("combos", e),
    )

    app.dependency_overrides[get_refresh_status] = lambda: status
    app.dependency_overrides[get_refresh_runner] = lambda: runner
    app.dependency_overrides[get_read_engine] = lambda: engine

    async for client in _client(app):
        resp = await client.post("/admin/refresh?stages=combos,tags")

    assert resp.status_code == 202
    # hnsw has no injected callable but does DB work — we don't gate on
    # it here. Just assert the injected fns for skipped stages never ran.
    assert "ingest" not in called
    assert "embed" not in called
    assert "classify" not in called
    assert "tags" in called
    assert "combos" in called


async def test_refresh_unknown_stage_returns_422(app: FastAPI) -> None:
    """Bogus stage names fail validation before the run is claimed."""
    status = RefreshStatus()

    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    runner = RefreshRunner(engine=engine, status=status)

    app.dependency_overrides[get_refresh_status] = lambda: status
    app.dependency_overrides[get_refresh_runner] = lambda: runner
    app.dependency_overrides[get_read_engine] = lambda: engine

    async for client in _client(app):
        resp = await client.post("/admin/refresh?stages=combos,bogus")

    assert resp.status_code == 422
    # Slot must not be claimed on rejection.
    assert status.snapshot().running is False
