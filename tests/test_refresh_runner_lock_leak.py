"""D10.9 — advisory-lock release survives an aborted-transaction state.

Bug repro: `RefreshRunner.run_locked` releases the advisory lock inside a
fresh `conn.begin()` in its `finally` block. If the prior txn raised and
left the underlying SA connection in *aborted-transaction* state,
`conn.begin()` itself raises and `pg_advisory_unlock(42)` never runs —
the lock leaks until the pool recycles the session.

The fake here models that state explicitly:

* `_AbortableFakeConn.begin()` raises `InvalidRequestError` ("can't reuse
  this connection") whenever `aborted=True`, mirroring how SQLAlchemy
  surfaces an aborted async connection on the next implicit auto-begin.
* `rollback()` clears the flag — same as a real DBAPI rollback after an
  aborted transaction.

The fix: `run_locked`'s `finally` must `await conn.rollback()` (best-
effort) before opening the unlock txn. With that in place, both the
happy path and this aborted-state path release the lock and reach
`mark_finished()`.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import InvalidRequestError

from biblioplex.ingest.refresh import (
    ADVISORY_LOCK_KEY,
    RefreshRunner,
    RefreshStatus,
)


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _AbortableFakeConn:
    """Async-context-manager DB conn with a toggleable aborted-txn state.

    When ``aborted=True``, calling ``begin()`` raises ``InvalidRequestError``
    — the same surface SQLAlchemy presents when the underlying DBAPI
    connection is in an aborted transaction and a new implicit auto-begin
    is attempted. ``rollback()`` clears the flag (matching a real DBAPI
    rollback which transitions the connection back to a usable state).
    """

    def __init__(self, *, lock_granted: bool = True) -> None:
        self.lock_granted = lock_granted
        self.executed: list[str] = []
        self.aborted: bool = False
        self.rollbacks: int = 0
        self.begin_attempts: int = 0

    async def __aenter__(self) -> _AbortableFakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _AbortableFakeConn:
        self.begin_attempts += 1
        if self.aborted:
            raise InvalidRequestError(
                "Can't reconnect until invalid transaction is rolled back."
            )
        return self

    async def execution_options(self, **_kw: Any) -> _AbortableFakeConn:
        # D13.7 concurrent-build path.
        return self

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append(sql)
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(self.lock_granted)
        if "pg_advisory_unlock" in sql:
            return _FakeResult(True)
        return _FakeResult(None)

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.aborted = False

    async def commit(self) -> None:
        return None


class _FakeEngine:
    def __init__(self, conn: _AbortableFakeConn) -> None:
        self._conn = conn

    def begin(self) -> _AbortableFakeConn:
        return self._conn

    def connect(self) -> _AbortableFakeConn:
        return self._conn


async def _noop_ingest(_engine: Any) -> int:
    return 0


# --- D10.9 coverage ----------------------------------------------------------


async def test_unlock_runs_when_embed_leaves_conn_aborted() -> None:
    """Embed raises AND leaves the conn in aborted state — unlock must still fire.

    Pre-fix: the `finally` opened a fresh `conn.begin()` which itself
    raised ``InvalidRequestError`` because the SA conn was still in an
    aborted transaction. The `pg_advisory_unlock(42)` SQL never made it
    to the driver and the lock leaked.

    Post-fix: `finally` first calls ``await conn.rollback()`` to clear
    the aborted state, so the unlock-txn `begin()` succeeds and
    `pg_advisory_unlock(42)` runs exactly once.

    `mark_finished()` is asserted via the outer-finally invariant — it
    must run regardless of either inner failure.
    """
    status = RefreshStatus()
    conn = _AbortableFakeConn(lock_granted=True)
    engine = _FakeEngine(conn)

    async def aborting_embed(_engine: Any) -> int:
        # Simulate "embed roundtrip blew up mid-txn AND left the SA
        # connection in aborted state" — the exact failure mode the
        # pre-fix `finally` block cannot recover from.
        conn.aborted = True
        raise RuntimeError("embed boom; conn aborted")

    runner = RefreshRunner(
        engine=engine,
        status=status,
        ingest=_noop_ingest,
        embed=aborting_embed,
    )

    with pytest.raises(RuntimeError, match="embed boom"):
        await runner.run(job_id="job-aborted")

    # The lock MUST be released even though the inner txn aborted.
    unlocks = [s for s in conn.executed if "pg_advisory_unlock" in s]
    assert len(unlocks) == 1, (
        f"expected exactly one pg_advisory_unlock call; got executed={conn.executed!r}"
    )
    assert f"({ADVISORY_LOCK_KEY})" in unlocks[0]

    # `rollback()` must have been called to clear the aborted state
    # before the unlock-txn `begin()`.
    assert conn.rollbacks >= 1, (
        "finally must rollback() to clear aborted state before unlocking"
    )

    # Outer finally always restores idle status (which calls mark_finished()).
    assert status.snapshot().running is False


async def test_unlock_runs_on_happy_path_without_extra_rollback_noise() -> None:
    """Happy path: no aborted state, unlock runs once, status reset.

    Sanity check that the rollback-before-unlock fix doesn't break the
    common path. The `await conn.rollback()` is a no-op on a clean
    connection — assert exactly one unlock and that the runner returns
    the staged-row count from ingest.
    """
    status = RefreshStatus()
    conn = _AbortableFakeConn(lock_granted=True)
    engine = _FakeEngine(conn)

    async def ingest_three(_engine: Any) -> int:
        return 3

    runner = RefreshRunner(
        engine=engine, status=status, ingest=ingest_three
    )

    staged = await runner.run(job_id="job-happy")

    assert staged == 3
    unlocks = [s for s in conn.executed if "pg_advisory_unlock" in s]
    assert len(unlocks) == 1
    assert status.snapshot().running is False
