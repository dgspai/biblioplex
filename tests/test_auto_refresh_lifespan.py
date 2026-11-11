"""Lifespan auto-refresh task ownership (D10.4, tech_spec.md §6.5 ops).

The auto-refresh task kicked from ``lifespan`` when ``cards`` is empty
must be strongly referenced so Python's GC can't kill it mid-ingest, and
must be cancelled+awaited on lifespan shutdown so the engine ``dispose``
calls can't race a half-finished run. These tests pin both halves.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI

from biblioplex.config import Settings
from biblioplex.ingest.refresh import RefreshRunner
from biblioplex.main import create_app


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def first(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        return self._value


class _FakeTxn:
    """`conn.begin()` ctx mgr; commit/rollback on exit clears autobegin."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        self._conn._autobegun = True
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        self._conn._autobegun = False


class _FakeConn:
    """Stand-in for an async DB connection.

    ``has_card`` controls the auto-refresh probe: when True, the
    ``SELECT 1 FROM cards LIMIT 1`` returns a row and lifespan skips the
    kick; when False, ``first()`` returns ``None`` so lifespan proceeds.

    ``lock_granted`` controls the Bug.7 advisory-lock gate: when False,
    the boot probe returns 0 and lifespan bails before minting a job.

    ``executed_sql`` records every ``execute()`` call so Bug.9's pin can
    assert the probe uses the txn-scoped lock variant and never calls
    a manual unlock (the txn end handles release).
    """

    def __init__(
        self, *, has_card: bool, lock_granted: bool = True, has_embedding: bool = True
    ) -> None:
        self.has_card = has_card
        self.has_embedding = has_embedding
        self.lock_granted = lock_granted
        self.executed_sql: list[str] = []
        self._autobegun = False

    async def __aenter__(self) -> _FakeConn:
        self._autobegun = False  # new checkout = new connection state
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Checkout ends: a real engine.begin()/connect() commits and returns
        # the connection to the pool, so no transaction state survives it.
        self._autobegun = False

    def begin(self) -> _FakeTxn:
        # Like SQLAlchemy: execute() autobegins, so begin() afterwards raises.
        if self._autobegun:
            raise RuntimeError(
                "This connection has already initialized a SQLAlchemy "
                "Transaction() object via begin() or autobegin"
            )
        return _FakeTxn(self)

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self._autobegun = True
        self.executed_sql.append(sql)
        if "SELECT 1 FROM cards LIMIT 1" in sql:
            return _FakeResult(("x",) if self.has_card else None)
        if "FROM cards WHERE embedding IS NOT NULL" in sql:
            return _FakeResult(("x",) if self.has_embedding else None)
        if "pg_try_advisory_xact_lock" in sql or "pg_try_advisory_lock" in sql:
            return _FakeResult(self.lock_granted)
        return _FakeResult(None)


class _FakeEngine:
    """Engine fake exposing both ``connect()`` and ``dispose()``.

    ``connect()`` returns an already-entered ``_FakeConn`` (same object
    via ``__aenter__``); ``dispose()`` is a no-op so lifespan teardown
    doesn't try to close a real pool.
    """

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self) -> _FakeConn:
        return self._conn

    def begin(self) -> _FakeConn:
        return self._conn

    async def dispose(self) -> None:
        return None


def _settings(*, auto_refresh: bool = True) -> Settings:
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
        auto_refresh_if_empty=auto_refresh,
    )


@pytest.fixture(autouse=True)
def _stub_model_and_catalogs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the embedding model + Scryfall keyword fetch in lifespan.

    Lifespan eagerly loads the HF model and pings Scryfall; both are
    out of scope for these tests, so we stub them with cheap no-ops.
    """

    class _StubModel:
        def get_sentence_embedding_dimension(self) -> int:
            return 384

    monkeypatch.setattr("biblioplex.main.load_model", lambda _s: _StubModel())
    monkeypatch.setattr("biblioplex.main.assert_model_dim", lambda _m, _s: None)

    async def _fake_fetch(_client: Any) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(
        "biblioplex.main.scryfall_catalogs.fetch_keyword_set", _fake_fetch
    )
    monkeypatch.setattr(
        "biblioplex.main.scryfall_catalogs.set_keyword_set", lambda _kws: None
    )


def _install_engines(
    app: FastAPI,
    *,
    has_card: bool,
    lock_granted: bool = True,
    has_embedding: bool = True,
) -> _FakeEngine:
    """Replace the real engines on ``app.state`` with fakes.

    ``create_app`` constructs the real psycopg engines eagerly; we swap
    them out before entering the lifespan so the probe + dispose paths
    don't hit the network.
    """
    conn = _FakeConn(
        has_card=has_card, lock_granted=lock_granted, has_embedding=has_embedding
    )
    engine = _FakeEngine(conn)
    app.state.read_engine = engine
    app.state.refresh_engine = engine
    return engine


async def test_lifespan_holds_auto_refresh_task_when_cards_empty() -> None:
    """Empty cards table → task is stored on app.state and is an asyncio.Task."""
    app = create_app(settings=_settings(auto_refresh=True))
    _install_engines(app, has_card=False)

    # Stub the refresh runner so the background task drains immediately
    # without touching Scryfall.
    async def _quick(_engine: Any) -> int:
        return 0

    runner = RefreshRunner(
        engine=app.state.refresh_engine,
        status=app.state.refresh_status,
        ingest=_quick,
    )
    # Patch the per-app runner builder so _auto_refresh_if_empty picks
    # up our stub rather than the production embed/tags pipeline.
    import biblioplex.main as main_mod

    original_builder = main_mod.build_refresh_runner
    main_mod.build_refresh_runner = lambda _app: runner  # type: ignore[assignment]
    try:
        async with app.router.lifespan_context(app):
            task = getattr(app.state, "_auto_refresh_task", None)
            assert task is not None, "lifespan should hold the auto-refresh task"
            assert isinstance(task, asyncio.Task)
            # Drain the (tiny) stub run so finally can clear the ref.
            await task
    finally:
        main_mod.build_refresh_runner = original_builder  # type: ignore[assignment]


async def test_lifespan_skips_task_when_cards_present() -> None:
    """Non-empty cards table → no task created, attr unset or None."""
    app = create_app(settings=_settings(auto_refresh=True))
    _install_engines(app, has_card=True)

    async with app.router.lifespan_context(app):
        task = getattr(app.state, "_auto_refresh_task", None)
        assert task is None


async def test_auto_refresh_probe_uses_xact_scoped_lock() -> None:
    """Bug.9: probe must use `pg_try_advisory_xact_lock` and never `pg_advisory_unlock`.

    Session-level `pg_try_advisory_lock` survives txn rollback, so if
    the manual unlock statement ever raised (statement-timeout, cancelled
    task) the lock would leak on the pooled connection and block every
    future refresh in the process. Txn-scoped variant auto-releases when
    the surrounding `conn.begin()` commits or rolls back.

    Assert against the SQL fired BEFORE the runner starts (probe path
    only) — the runner has its own advisory-lock lifecycle on a
    separate connection in production.
    """
    app = create_app(settings=_settings(auto_refresh=True))
    engine = _install_engines(app, has_card=False)

    async def _quick(_engine: Any) -> int:
        return 0

    runner = RefreshRunner(
        engine=app.state.refresh_engine,
        status=app.state.refresh_status,
        ingest=_quick,
    )
    import biblioplex.main as main_mod

    original_builder = main_mod.build_refresh_runner
    probe_sqls: list[str] = []

    def _capture_and_build(a: Any) -> RefreshRunner:
        # The lifespan probe runs BEFORE build_refresh_runner is called
        # to mint the background task — snapshot the fake conn's SQL log
        # here so the runner's later SQL doesn't contaminate the pin.
        probe_sqls[:] = list(engine._conn.executed_sql)
        return runner

    main_mod.build_refresh_runner = _capture_and_build  # type: ignore[assignment]
    try:
        async with app.router.lifespan_context(app):
            task = getattr(app.state, "_auto_refresh_task", None)
            if task is not None:
                # Drain — errors are logged inside `_run` and swallowed.
                await task
    finally:
        main_mod.build_refresh_runner = original_builder  # type: ignore[assignment]

    advisory_sqls = [s for s in probe_sqls if "advisory" in s.lower()]
    assert advisory_sqls, "probe must issue an advisory-lock statement"
    assert any("pg_try_advisory_xact_lock" in s for s in advisory_sqls), (
        f"probe must use txn-scoped variant; got {advisory_sqls!r}"
    )
    assert not any("advisory_unlock" in s for s in advisory_sqls), (
        f"probe must not call manual unlock; got {advisory_sqls!r}"
    )


async def test_lifespan_skips_task_when_advisory_lock_not_granted() -> None:
    """Bug.7: advisory-lock probe returns 0 → no task minted.

    Multi-worker boot race — every worker independently probes
    cards-empty. Prior code minted N jobs and let N-1 lose on
    `pg_try_advisory_lock` inside the runner. Now the boot path
    gates on the same lock upstream so only the eventual winner
    launches the background task.
    """
    app = create_app(settings=_settings(auto_refresh=True))
    _install_engines(app, has_card=False, lock_granted=False)

    async with app.router.lifespan_context(app):
        task = getattr(app.state, "_auto_refresh_task", None)
        assert task is None, "advisory-lock loser must not mint a task"


async def test_lifespan_skips_task_when_auto_refresh_disabled() -> None:
    """AUTO_REFRESH_IF_EMPTY=false → no probe, no task, regardless of card count."""
    app = create_app(settings=_settings(auto_refresh=False))
    _install_engines(app, has_card=False)

    async with app.router.lifespan_context(app):
        task = getattr(app.state, "_auto_refresh_task", None)
        assert task is None


async def test_lifespan_shutdown_cancels_pending_auto_refresh() -> None:
    """Shutdown of lifespan cancels + awaits a still-running auto-refresh.

    The fake ingest blocks on an Event that never fires; on lifespan
    exit the task must end up ``done()`` (cancelled) and the shutdown
    must not propagate ``CancelledError`` out of the context.
    """
    app = create_app(settings=_settings(auto_refresh=True))
    _install_engines(app, has_card=False)

    blocker = asyncio.Event()
    started = asyncio.Event()

    async def _hangs(_engine: Any) -> int:
        started.set()
        await blocker.wait()  # never fires — we want a still-running task
        return 0

    runner = RefreshRunner(
        engine=app.state.refresh_engine,
        status=app.state.refresh_status,
        ingest=_hangs,
    )

    import biblioplex.main as main_mod

    original_builder = main_mod.build_refresh_runner
    main_mod.build_refresh_runner = lambda _app: runner  # type: ignore[assignment]
    captured: dict[str, asyncio.Task[None]] = {}
    try:
        async with app.router.lifespan_context(app):
            # Wait until the task has actually entered the ingest body
            # so cancellation lands inside `await blocker.wait()` rather
            # than before the coroutine has scheduled.
            await asyncio.wait_for(started.wait(), timeout=2.0)
            task = app.state._auto_refresh_task
            assert task is not None
            assert not task.done()
            captured["task"] = task
        # Lifespan exited cleanly — CancelledError did not leak out.
        assert captured["task"].done()
        assert captured["task"].cancelled()
    finally:
        main_mod.build_refresh_runner = original_builder  # type: ignore[assignment]


async def test_lifespan_resumes_when_cards_exist_but_no_embeddings() -> None:
    """Interrupted first ingest: cards present, zero embeddings → refresh kicks."""
    app = create_app(settings=_settings(auto_refresh=True))
    _install_engines(app, has_card=True, has_embedding=False)

    async def _quick(_engine: Any) -> int:
        return 0

    runner = RefreshRunner(
        engine=app.state.refresh_engine,
        status=app.state.refresh_status,
        ingest=_quick,
    )
    import biblioplex.main as main_mod

    original_builder = main_mod.build_refresh_runner
    main_mod.build_refresh_runner = lambda _app: runner  # type: ignore[assignment]
    try:
        async with app.router.lifespan_context(app):
            task = getattr(app.state, "_auto_refresh_task", None)
            assert task is not None, "incomplete DB must resume the refresh"
            await task
    finally:
        main_mod.build_refresh_runner = original_builder  # type: ignore[assignment]


async def test_lifespan_skips_when_cards_and_embeddings_exist() -> None:
    """Healthy DB (even if only some cards are embedded) → no refresh."""
    app = create_app(settings=_settings(auto_refresh=True))
    _install_engines(app, has_card=True, has_embedding=True)

    async with app.router.lifespan_context(app):
        assert getattr(app.state, "_auto_refresh_task", None) is None
