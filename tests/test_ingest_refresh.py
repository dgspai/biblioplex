"""Tests for the refresh runner (D1.6, D1.8, tech_spec.md §4.2).

The runner coordinates discovery → stream → flatten → staging-insert →
collapse → truncate, guarded by `pg_try_advisory_lock(42)`. This module
tests the state machine, the lock-contention branch, and the runner's
SQL-emission order (D1.8: COLLAPSE_SQL must run AFTER ingest and BEFORE
TRUNCATE_STAGING_SQL per §4.2 steps 4 & 7). The real DB round-trip is
covered by the M8 smoke test.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from biblioplex.ingest.collapse import COLLAPSE_SQL, TRUNCATE_STAGING_SQL
from biblioplex.ingest.refresh import (
    ADVISORY_LOCK_KEY,
    AlreadyRunning,
    RefreshRunner,
    RefreshStatus,
)


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _FakeConn:
    """Async-context-manager DB conn that records executed SQL."""

    def __init__(self, *, lock_granted: bool = True) -> None:
        self.lock_granted = lock_granted
        self.executed: list[str] = []
        self.committed = 0

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _FakeConn:
        return self

    async def execution_options(self, **_kw: Any) -> _FakeConn:
        # D13.7 concurrent-build path calls `.execution_options(
        # isolation_level="AUTOCOMMIT")` on a fresh conn. Fake returns
        # itself so subsequent executes land in the same executed log.
        return self

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append(sql)
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(self.lock_granted)
        if "pg_advisory_unlock" in sql:
            return _FakeResult(True)
        return _FakeResult(None)

    async def commit(self) -> None:
        self.committed += 1


class _FakeEngine:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def begin(self) -> _FakeConn:
        return self._conn

    def connect(self) -> _FakeConn:
        return self._conn


async def _no_op_ingest(_engine: Any) -> int:
    """Fake ingest body: pretend we staged 3 cards, do no work."""
    return 3


# --- RefreshStatus state machine -------------------------------------------


def test_refresh_status_starts_idle() -> None:
    """Fresh status: not running, no started_at, no job_id."""
    status = RefreshStatus()
    snap = status.snapshot()
    assert snap.running is False
    assert snap.started_at is None
    assert snap.job_id is None


def test_refresh_status_mark_running_then_finished() -> None:
    """`mark_started(job_id)` flips running True; `mark_finished()` flips it back."""
    status = RefreshStatus()
    status.mark_started("job-1")
    snap = status.snapshot()
    assert snap.running is True
    assert snap.job_id == "job-1"
    assert snap.started_at is not None

    status.mark_finished()
    snap = status.snapshot()
    assert snap.running is False
    # job_id retained for /health visibility into the most-recent run.
    assert snap.job_id == "job-1"


def test_refresh_status_mark_started_while_running_raises() -> None:
    """Starting a second run while one is in flight is a caller bug."""
    status = RefreshStatus()
    status.mark_started("job-1")
    with pytest.raises(AlreadyRunning):
        status.mark_started("job-2")


def test_refresh_status_stage_records_start_and_end() -> None:
    """`stage(name)` records started_at on enter and finished_at on exit."""
    status = RefreshStatus()
    status.mark_started("job-stages")

    with status.stage("ingest") as st:
        st.rows = 42
        mid = status.snapshot()
        # In-flight: entry exists with started_at set, finished_at still None.
        assert len(mid.stages) == 1
        assert mid.stages[0].name == "ingest"
        assert mid.stages[0].started_at is not None
        assert mid.stages[0].finished_at is None
        assert mid.stages[0].rows == 42
        assert mid.stages[0].error is None

    done = status.snapshot()
    assert len(done.stages) == 1
    assert done.stages[0].finished_at is not None
    assert done.stages[0].finished_at >= done.stages[0].started_at
    assert done.stages[0].rows == 42
    assert done.stages[0].error is None


def test_refresh_status_stage_finalizes_on_exception() -> None:
    """A stage that raises still gets `finished_at` stamped and `error` set."""
    status = RefreshStatus()
    status.mark_started("job-crash")

    with pytest.raises(RuntimeError), status.stage("embed"):
        raise RuntimeError("boom")

    snap = status.snapshot()
    assert snap.stages[0].name == "embed"
    assert snap.stages[0].finished_at is not None
    # Ran-but-crashed marker: rows never set, error captured.
    assert snap.stages[0].rows is None
    assert snap.stages[0].error == "RuntimeError: boom"


def test_refresh_status_mark_started_resets_stages() -> None:
    """A new run clears the prior run's stage history."""
    status = RefreshStatus()
    status.mark_started("job-1")
    with status.stage("ingest"):
        pass
    status.mark_finished()

    # After finish, prior stages remain visible until the next start.
    assert len(status.snapshot().stages) == 1

    status.mark_started("job-2")
    assert status.snapshot().stages == ()


def test_refresh_status_planned_stages_reset_on_start() -> None:
    status = RefreshStatus()
    assert status.snapshot().planned == ()
    status.mark_started("job-1")
    status.set_planned(("ingest", "embed"))
    assert status.snapshot().planned == ("ingest", "embed")
    status.mark_finished()
    status.mark_started("job-2")
    assert status.snapshot().planned == ()


async def test_runner_records_selected_stages_in_canonical_order() -> None:
    status = RefreshStatus()
    runner = RefreshRunner(
        engine=_FakeEngine(_FakeConn(lock_granted=True)),
        status=status,
        ingest=_no_op_ingest,
    )
    status.mark_started("job-x")
    await runner.run_locked(frozenset({"ingest", "tags"}))
    assert status.snapshot().planned == ("ingest", "tags")


# --- Runner --------------------------------------------------------------


async def test_runner_runs_ingest_and_updates_status() -> None:
    """Successful run: status flips running True during, False after."""
    status = RefreshStatus()
    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    runner = RefreshRunner(engine=engine, status=status, ingest=_no_op_ingest)

    await runner.run(job_id="job-x")

    snap = status.snapshot()
    assert snap.running is False
    assert snap.job_id == "job-x"

    # Lock was acquired and released.
    joined = " ".join(conn.executed)
    assert "pg_try_advisory_lock" in joined
    assert f"({ADVISORY_LOCK_KEY})" in joined
    assert "pg_advisory_unlock" in joined
    # Truncate ran after collapse (idempotency per §4.2 step 7).
    assert any("TRUNCATE" in s for s in conn.executed)


async def test_runner_returns_already_running_when_lock_contended() -> None:
    """`pg_try_advisory_lock` returns False → runner raises AlreadyRunning, no work."""
    status = RefreshStatus()
    conn = _FakeConn(lock_granted=False)
    engine = _FakeEngine(conn)
    called = {"ingest": 0}

    async def tracked_ingest(_engine: Any) -> int:
        called["ingest"] += 1
        return 0

    runner = RefreshRunner(engine=engine, status=status, ingest=tracked_ingest)

    with pytest.raises(AlreadyRunning):
        await runner.run(job_id="job-y")

    # No ingest, no truncate, status untouched.
    assert called["ingest"] == 0
    assert status.snapshot().running is False
    assert all("TRUNCATE" not in s for s in conn.executed)


async def test_runner_releases_lock_on_ingest_failure() -> None:
    """If ingest raises, the advisory lock is still released and status reset."""
    status = RefreshStatus()
    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)

    async def failing_ingest(_engine: Any) -> int:
        raise RuntimeError("boom")

    runner = RefreshRunner(engine=engine, status=status, ingest=failing_ingest)

    with pytest.raises(RuntimeError, match="boom"):
        await runner.run(job_id="job-z")

    assert status.snapshot().running is False
    assert any("pg_advisory_unlock" in s for s in conn.executed)


async def test_runner_emits_collapse_between_ingest_and_truncate() -> None:
    """D1.8: COLLAPSE_SQL runs AFTER the ingest body and BEFORE TRUNCATE_STAGING_SQL.

    Drives §4.2 step 4 (collapse staging → cards) followed by step 7
    (truncate staging). Captures the ingest checkpoint via a sentinel SQL
    string the test ingest body emits, then asserts ordering on the fake
    conn's executed-SQL list.
    """
    status = RefreshStatus()
    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    sentinel = "-- INGEST DONE --"

    async def ingest_marker(_engine: Any) -> int:
        # Emit a sentinel so we can locate the boundary between "ingest
        # work" and the runner's collapse/truncate calls in the SQL log.
        await conn.execute(sentinel)
        return 7

    runner = RefreshRunner(engine=engine, status=status, ingest=ingest_marker)

    staged = await runner.run(job_id="job-collapse")

    assert staged == 7, "runner must return the ingest body's row count"

    # Locate each landmark in the executed-SQL log.
    idx_sentinel = next(i for i, s in enumerate(conn.executed) if sentinel in s)
    idx_collapse = next(i for i, s in enumerate(conn.executed) if COLLAPSE_SQL in s)
    idx_truncate = max(
        i for i, s in enumerate(conn.executed) if TRUNCATE_STAGING_SQL in s
    )

    assert idx_sentinel < idx_collapse < idx_truncate, (
        "expected order: ingest body → COLLAPSE_SQL → TRUNCATE_STAGING_SQL; "
        f"got executed={conn.executed!r}"
    )


async def test_runner_skips_collapse_on_ingest_failure() -> None:
    """If ingest raises, COLLAPSE_SQL and TRUNCATE_STAGING_SQL must NOT run.

    Collapsing a half-populated staging table would corrupt `cards` with
    partial-refresh rows. Staging is left dirty; the next run clears it
    before ingesting and truncates again at its tail.
    """
    status = RefreshStatus()
    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)

    async def failing_ingest(_engine: Any) -> int:
        raise RuntimeError("ingest boom")

    runner = RefreshRunner(engine=engine, status=status, ingest=failing_ingest)

    with pytest.raises(RuntimeError, match="ingest boom"):
        await runner.run(job_id="job-fail-collapse")

    assert all(COLLAPSE_SQL not in s for s in conn.executed), (
        "collapse must not run when ingest fails"
    )
    # Only the pre-ingest clear runs; the tail truncate is skipped.
    assert sum(TRUNCATE_STAGING_SQL in s for s in conn.executed) == 1, (
        "tail truncate must not run when ingest fails"
    )
    # Lock still released.
    assert any("pg_advisory_unlock" in s for s in conn.executed)


async def test_runner_runs_embed_between_collapse_and_truncate() -> None:
    """D5.3: embed_pending fires AFTER COLLAPSE_SQL and BEFORE TRUNCATE_STAGING_SQL.

    The embed step is the injected `embed` callable on RefreshRunner.
    We capture its position relative to the collapse / truncate SQL via
    a sentinel string the embed body emits on the fake conn.
    """
    status = RefreshStatus()
    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    sentinel = "-- EMBED DONE --"
    embed_called = {"count": 0}

    async def embed_marker(_engine: Any) -> int:
        embed_called["count"] += 1
        await conn.execute(sentinel)
        return 5

    runner = RefreshRunner(
        engine=engine, status=status, ingest=_no_op_ingest, embed=embed_marker
    )

    await runner.run(job_id="job-embed")

    assert embed_called["count"] == 1

    idx_collapse = next(i for i, s in enumerate(conn.executed) if COLLAPSE_SQL in s)
    idx_embed = next(i for i, s in enumerate(conn.executed) if sentinel in s)
    idx_truncate = max(
        i for i, s in enumerate(conn.executed) if TRUNCATE_STAGING_SQL in s
    )
    assert idx_collapse < idx_embed < idx_truncate, (
        f"expected COLLAPSE_SQL → embed → TRUNCATE_STAGING_SQL; got {conn.executed!r}"
    )


async def test_runner_builds_hnsw_after_embed_before_truncate() -> None:
    """D5.4/D13.7: HNSW concurrent build fires AFTER embed and BEFORE
    TRUNCATE_STAGING_SQL. Per §4.2 step 6 the index is built post-
    embedding because bulk-then-index is ~10× faster than incremental.
    """
    from biblioplex.ingest.refresh import (
        HNSW_CONCURRENT_BUILD_SQL,
        HNSW_SWAP_SQL,
    )

    status = RefreshStatus()
    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    embed_sentinel = "-- EMBED DONE --"

    async def embed_marker(_engine: Any) -> int:
        await conn.execute(embed_sentinel)
        return 1

    runner = RefreshRunner(
        engine=engine, status=status, ingest=_no_op_ingest, embed=embed_marker
    )

    await runner.run(job_id="job-hnsw")

    idx_embed = next(i for i, s in enumerate(conn.executed) if embed_sentinel in s)
    idx_build = next(
        i for i, s in enumerate(conn.executed) if HNSW_CONCURRENT_BUILD_SQL in s
    )
    idx_swap = next(i for i, s in enumerate(conn.executed) if HNSW_SWAP_SQL in s)
    idx_truncate = max(
        i for i, s in enumerate(conn.executed) if TRUNCATE_STAGING_SQL in s
    )
    assert idx_embed < idx_build < idx_swap < idx_truncate, (
        f"expected embed → build → swap → TRUNCATE_STAGING_SQL; "
        f"got {conn.executed!r}"
    )
    # Concurrent build uses cosine opclass under the scratch name.
    assert "CREATE INDEX CONCURRENTLY" in HNSW_CONCURRENT_BUILD_SQL
    assert "cards_embedding_hnsw_new" in HNSW_CONCURRENT_BUILD_SQL
    assert "vector_cosine_ops" in HNSW_CONCURRENT_BUILD_SQL


async def test_runner_skips_hnsw_on_ingest_failure() -> None:
    """If ingest raises, the HNSW rebuild must NOT run.

    Rebuilding the index over a stale `cards.embedding` set would waste
    minutes for no gain; skip it on the failure path.
    """
    from biblioplex.ingest.refresh import HNSW_CONCURRENT_BUILD_SQL

    status = RefreshStatus()
    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)

    async def failing_ingest(_engine: Any) -> int:
        raise RuntimeError("ingest boom")

    runner = RefreshRunner(
        engine=engine, status=status, ingest=failing_ingest
    )

    with pytest.raises(RuntimeError, match="ingest boom"):
        await runner.run(job_id="job-fail-hnsw")

    assert all(HNSW_CONCURRENT_BUILD_SQL not in s for s in conn.executed)


async def test_runner_skips_embed_on_ingest_failure() -> None:
    """If ingest raises, embed_pending must NOT run either."""
    status = RefreshStatus()
    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    embed_called = {"count": 0}

    async def tracked_embed(_engine: Any) -> int:
        embed_called["count"] += 1
        return 0

    async def failing_ingest(_engine: Any) -> int:
        raise RuntimeError("ingest boom")

    runner = RefreshRunner(
        engine=engine, status=status, ingest=failing_ingest, embed=tracked_embed
    )

    with pytest.raises(RuntimeError, match="ingest boom"):
        await runner.run(job_id="job-fail-embed")

    assert embed_called["count"] == 0


async def test_runner_tags_and_combos_both_run_post_ingest(monkeypatch: Any) -> None:
    """D14.1: tags + combos both run alongside the main pipeline.

    With the DAG-driven parallel runner they overlap rather than running
    strictly one after the other — this test only pins that both are
    invoked and both are best-effort side-tables (COMBOS_SOURCE_URL
    empty → injected no-op default keeps overhead at zero).
    """
    status = RefreshStatus()
    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    called: set[str] = set()

    async def tags_marker(_engine: Any) -> int:
        called.add("tags")
        return 1

    async def combos_marker(_engine: Any) -> int:
        called.add("combos")
        return 1

    runner = RefreshRunner(
        engine=engine,
        status=status,
        ingest=_no_op_ingest,
        tags=tags_marker,
        combos=combos_marker,
    )

    await runner.run(job_id="job-combos")

    assert called == {"tags", "combos"}


async def test_runner_combos_failure_does_not_break_refresh(monkeypatch: Any) -> None:
    """D14.1: combo-ingest exception is logged + swallowed.

    Third-party JSON source may 5xx / rate-limit / return junk. The
    cards refresh must stay committed and mark_finished must fire.
    """
    status = RefreshStatus()
    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)

    async def broken_combos(_engine: Any) -> int:
        raise RuntimeError("combo source 500")

    runner = RefreshRunner(
        engine=engine,
        status=status,
        ingest=_no_op_ingest,
        combos=broken_combos,
    )

    # Must NOT raise — combo failure is swallowed identically to the
    # oracle-tag failure branch.
    staged = await runner.run(job_id="job-combos-fail")
    assert staged == 3
    assert status.snapshot().running is False


async def test_runner_post_ingest_stages_run_concurrently() -> None:
    """embed+hnsw, classify, combos, and tags fan out in parallel post-ingest.

    Each best-effort branch blocks on a shared barrier; they only progress
    once every branch has entered its body. If the runner still ran them
    serially, the first branch would block forever and the test would
    time out. The `asyncio.wait_for(..., 2.0)` bound turns a serial-run
    regression into a fast, deterministic failure.
    """
    status = RefreshStatus()
    conn = _FakeConn(lock_granted=True)
    engine = _FakeEngine(conn)

    barrier = asyncio.Barrier(4)  # embed, classify, combos, tags

    async def wait_at_barrier(_engine: Any) -> int:
        await barrier.wait()
        return 1

    runner = RefreshRunner(
        engine=engine,
        status=status,
        ingest=_no_op_ingest,
        embed=wait_at_barrier,
        classify=wait_at_barrier,
        combos=wait_at_barrier,
        tags=wait_at_barrier,
    )

    await asyncio.wait_for(runner.run(job_id="job-parallel"), timeout=2.0)
    assert status.snapshot().running is False


async def test_runner_concurrent_calls_only_one_wins() -> None:
    """Two parallel runner.run() calls — second sees AlreadyRunning."""
    status = RefreshStatus()

    # Two engines so the lock semantics are simulated by the status object's
    # in-process flag, not by both sharing one fake conn that always grants.
    engine_a = _FakeEngine(_FakeConn(lock_granted=True))
    engine_b = _FakeEngine(_FakeConn(lock_granted=False))

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_ingest(_engine: Any) -> int:
        started.set()
        await release.wait()
        return 0

    runner_a = RefreshRunner(engine=engine_a, status=status, ingest=slow_ingest)
    runner_b = RefreshRunner(engine=engine_b, status=status, ingest=slow_ingest)

    task_a = asyncio.create_task(runner_a.run(job_id="a"))
    await started.wait()  # ensure A is mid-flight before B starts

    with pytest.raises(AlreadyRunning):
        await runner_b.run(job_id="b")

    release.set()
    await task_a
    assert status.snapshot().running is False
