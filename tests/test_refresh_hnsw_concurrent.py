"""HNSW rebuild runs CONCURRENTLY on a scratch name + atomic swap (D13.7).

Pre-D13.7 the refresh runner did ``DROP INDEX; CREATE INDEX ...`` inside
a single transaction. For the duration of the rebuild (~seconds to
minutes on 30k rows), ``/similar`` either seq-scanned (5s statement
timeout kicked in) or errored. That's the API's only cold window.

Post-D13.7:
* Pre-cleanup ``DROP INDEX IF EXISTS cards_embedding_hnsw_new`` runs on
  a fresh conn with ``isolation_level="AUTOCOMMIT"`` (CONCURRENTLY
  forbids txns).
* Concurrent build under the scratch name runs on the same AUTOCOMMIT
  conn, leaving the currently-serving ``cards_embedding_hnsw`` live.
* Atomic swap (``DROP IF EXISTS ...; ALTER INDEX ... RENAME ...``) runs
  on the lock-holding conn in a tiny txn.
* Staging truncate follows in the same swap txn.

These tests pin the SQL ordering + the presence of the AUTOCOMMIT hop.
"""

from __future__ import annotations

from typing import Any

from biblioplex.ingest.collapse import COLLAPSE_SQL, TRUNCATE_STAGING_SQL
from biblioplex.ingest.refresh import (
    HNSW_CONCURRENT_BUILD_SQL,
    HNSW_CONCURRENT_CLEANUP_SQL,
    HNSW_SWAP_SQL,
    RefreshRunner,
    RefreshStatus,
)


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _TrackingFakeConn:
    """Fake conn that records (execution_options_calls, executed_sqls).

    The runner opens ONE lock-holding conn plus (D13.7) a SECOND conn
    just for the CONCURRENTLY build. This fake serves both via
    ``connect()`` and tracks how many times ``execution_options`` was
    invoked — the concurrent path must call it exactly once with
    ``isolation_level="AUTOCOMMIT"``.
    """

    def __init__(self, *, lock_granted: bool = True) -> None:
        self.lock_granted = lock_granted
        self.executed: list[str] = []
        self.execution_option_calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _TrackingFakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _TrackingFakeConn:
        return self

    async def execution_options(self, **kw: Any) -> _TrackingFakeConn:
        self.execution_option_calls.append(dict(kw))
        return self

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append(sql)
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(self.lock_granted)
        return _FakeResult(None)


class _FakeEngine:
    def __init__(self, conn: _TrackingFakeConn) -> None:
        self._conn = conn

    def begin(self) -> _TrackingFakeConn:
        return self._conn

    def connect(self) -> _TrackingFakeConn:
        return self._conn


async def _noop_ingest(_engine: Any) -> int:
    return 0


async def test_concurrent_build_runs_under_autocommit() -> None:
    """The CONCURRENTLY branch calls `.execution_options(isolation_level=
    "AUTOCOMMIT")` on a fresh connection before executing the build."""
    status = RefreshStatus()
    conn = _TrackingFakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    runner = RefreshRunner(engine=engine, status=status, ingest=_noop_ingest)

    await runner.run(job_id="job-d13-7")

    # At least one execution_options call must specify AUTOCOMMIT — the
    # runner is free to call it more than once in the future (e.g. for
    # setting statement_timeout on the build), so assert existence not
    # exact count.
    autocommit_calls = [
        c for c in conn.execution_option_calls
        if c.get("isolation_level") == "AUTOCOMMIT"
    ]
    assert autocommit_calls, (
        f"expected at least one AUTOCOMMIT execution_options call; "
        f"got {conn.execution_option_calls!r}"
    )


async def test_concurrent_build_ordering() -> None:
    """collapse → cleanup → CONCURRENTLY build → swap → truncate."""
    status = RefreshStatus()
    conn = _TrackingFakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    runner = RefreshRunner(engine=engine, status=status, ingest=_noop_ingest)

    await runner.run(job_id="job-order")

    idx_collapse = next(i for i, s in enumerate(conn.executed) if COLLAPSE_SQL in s)
    idx_cleanup = next(
        i for i, s in enumerate(conn.executed) if HNSW_CONCURRENT_CLEANUP_SQL in s
    )
    idx_build = next(
        i for i, s in enumerate(conn.executed) if HNSW_CONCURRENT_BUILD_SQL in s
    )
    idx_swap = next(i for i, s in enumerate(conn.executed) if HNSW_SWAP_SQL in s)
    idx_truncate = max(
        i for i, s in enumerate(conn.executed) if TRUNCATE_STAGING_SQL in s
    )
    assert idx_collapse < idx_cleanup < idx_build < idx_swap < idx_truncate


async def test_ingest_starts_from_empty_staging() -> None:
    """A failed run leaves staging populated (truncate is the last step).

    Ingest must clear it before loading, or the next collapse merges the
    stale snapshot with the fresh one and DISTINCT ON may keep a stale row.
    """
    status = RefreshStatus()
    conn = _TrackingFakeConn(lock_granted=True)
    engine = _FakeEngine(conn)
    order: list[str] = []

    async def _recording_ingest(_engine: object) -> int:
        order.append(f"ingest@{len(conn.executed)}")
        return 0

    runner = RefreshRunner(engine=engine, status=status, ingest=_recording_ingest)
    await runner.run(job_id="job-pre-truncate")

    at = int(order[0].split("@")[1])
    assert any(TRUNCATE_STAGING_SQL in s for s in conn.executed[:at]), conn.executed[:at]


async def test_pre_cleanup_drops_scratch_name() -> None:
    """The pre-cleanup targets the scratch index name specifically —
    a prior CONCURRENTLY crash can leave an INVALID `cards_embedding_hnsw_new`
    around, and `CREATE INDEX ... IF NOT EXISTS` would see it and skip.
    Pre-cleanup breaks that stall."""
    assert "cards_embedding_hnsw_new" in HNSW_CONCURRENT_CLEANUP_SQL
    assert "DROP INDEX IF EXISTS" in HNSW_CONCURRENT_CLEANUP_SQL


async def test_swap_txn_is_atomic_drop_then_rename() -> None:
    """Swap is one string containing both the drop of the served name
    and the rename of the scratch onto it — psycopg drivers submit it
    as a single simple-query, so Postgres commits atomically."""
    assert "DROP INDEX IF EXISTS cards_embedding_hnsw;" in HNSW_SWAP_SQL
    assert (
        "ALTER INDEX cards_embedding_hnsw_new RENAME TO cards_embedding_hnsw"
        in HNSW_SWAP_SQL
    )
    # Ordering within the string matters: drop first, then rename onto
    # the freed name.
    drop_pos = HNSW_SWAP_SQL.index("DROP INDEX IF EXISTS cards_embedding_hnsw;")
    rename_pos = HNSW_SWAP_SQL.index("ALTER INDEX cards_embedding_hnsw_new")
    assert drop_pos < rename_pos


async def test_concurrent_build_skipped_when_lock_not_granted() -> None:
    """Losing the advisory lock aborts BEFORE the concurrent build fires.
    Otherwise a losing replica would waste the build-time budget on a
    disposable scratch index."""
    from biblioplex.ingest.refresh import AlreadyRunning

    status = RefreshStatus()
    conn = _TrackingFakeConn(lock_granted=False)  # lock refused
    engine = _FakeEngine(conn)
    runner = RefreshRunner(engine=engine, status=status, ingest=_noop_ingest)

    try:
        await runner.run(job_id="job-loser")
    except AlreadyRunning:
        pass  # expected
    else:  # pragma: no cover
        raise AssertionError("expected AlreadyRunning when lock not granted")

    # No AUTOCOMMIT hop, no build.
    assert not any(
        c.get("isolation_level") == "AUTOCOMMIT"
        for c in conn.execution_option_calls
    )
    assert all(HNSW_CONCURRENT_BUILD_SQL not in s for s in conn.executed)


async def test_epoch_not_bumped_when_lock_not_granted() -> None:
    """Bug.6: AlreadyRunning bail must NOT bump the refresh epoch.

    Prior behavior invalidated every search-response ETag cache for a
    refresh that never actually ran (the losing replica's `mark_finished`
    unconditionally incremented). The winning replica bumps epoch when
    IT finishes — one bump per real completion.
    """
    from contextlib import suppress

    from biblioplex.ingest.refresh import AlreadyRunning

    status = RefreshStatus()
    epoch0 = status.epoch()
    conn = _TrackingFakeConn(lock_granted=False)
    engine = _FakeEngine(conn)
    runner = RefreshRunner(engine=engine, status=status, ingest=_noop_ingest)

    with suppress(AlreadyRunning):
        await runner.run(job_id="job-loser")

    assert status.epoch() == epoch0, "AlreadyRunning must not bump epoch"
