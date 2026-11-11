"""Property tests for HNSW post-embed build (D5.4, tech_spec.md §4.2 step 6).

The runner-ordering test lives in `test_ingest_refresh.py`; this module
covers the HNSW SQL structural properties and the "exactly one HNSW
build per successful refresh cycle" invariant under arbitrary stage
counts.

Post-D13.7 the rebuild happens under a scratch name with CONCURRENTLY
and swaps atomically — the assertions here check the concurrent-build
SQL and the swap SQL, not a single combined DROP+CREATE.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

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
        # D13.7 — CONCURRENTLY branch calls `.execution_options(
        # isolation_level="AUTOCOMMIT")` on a fresh connection. The
        # fake just returns itself so subsequent execute() calls land
        # in the same executed-log for ordering assertions.
        return self

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append(sql)
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(self.lock_granted)
        return _FakeResult(None)


class _FakeEngine:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def begin(self) -> _FakeConn:
        return self._conn

    def connect(self) -> _FakeConn:
        return self._conn


# --- Structural properties of the HNSW SQL --------------------------------


def test_hnsw_concurrent_build_uses_scratch_name() -> None:
    """Build runs under `cards_embedding_hnsw_new` — the live index stays live."""
    assert "cards_embedding_hnsw_new" in HNSW_CONCURRENT_BUILD_SQL
    assert "CONCURRENTLY" in HNSW_CONCURRENT_BUILD_SQL
    assert "IF NOT EXISTS" in HNSW_CONCURRENT_BUILD_SQL


def test_hnsw_cleanup_precedes_build_at_string_level() -> None:
    """Pre-cleanup drops the scratch name so a prior crash's INVALID index
    can't block CREATE ... IF NOT EXISTS."""
    assert "DROP INDEX IF EXISTS cards_embedding_hnsw_new" in HNSW_CONCURRENT_CLEANUP_SQL


def test_hnsw_swap_drops_live_then_renames_scratch() -> None:
    """Swap is atomic: drop the served name, then rename the scratch onto it."""
    assert "DROP INDEX IF EXISTS cards_embedding_hnsw" in HNSW_SWAP_SQL
    assert (
        "ALTER INDEX cards_embedding_hnsw_new RENAME TO cards_embedding_hnsw"
        in HNSW_SWAP_SQL
    )


def test_hnsw_sql_uses_cosine_ops() -> None:
    """§6.3 uses `embedding <=> $1`, the cosine-distance operator;
    matching index opclass is `vector_cosine_ops`."""
    assert "vector_cosine_ops" in HNSW_CONCURRENT_BUILD_SQL
    assert "USING hnsw" in HNSW_CONCURRENT_BUILD_SQL


# --- Runner invariants (property test, M5 mandate) ------------------------


@given(
    staged=st.integers(min_value=0, max_value=10_000),
    embedded=st.integers(min_value=0, max_value=10_000),
)
def test_runner_emits_exactly_one_hnsw_build_per_run(
    staged: int, embedded: int
) -> None:
    """For any ingest/embed row counts, the concurrent build fires exactly once
    and is followed by exactly one swap."""
    import asyncio

    async def _run() -> None:
        status = RefreshStatus()
        conn = _FakeConn(lock_granted=True)
        engine = _FakeEngine(conn)

        async def ingest(_engine: Any) -> int:
            return staged

        async def embed(_engine: Any) -> int:
            return embedded

        runner = RefreshRunner(
            engine=engine, status=status, ingest=ingest, embed=embed
        )
        await runner.run(job_id=f"job-{staged}-{embedded}")

        build_hits = [s for s in conn.executed if HNSW_CONCURRENT_BUILD_SQL in s]
        swap_hits = [s for s in conn.executed if HNSW_SWAP_SQL in s]
        assert len(build_hits) == 1, (
            f"expected exactly one HNSW concurrent build, got {len(build_hits)}; "
            f"executed={conn.executed!r}"
        )
        assert len(swap_hits) == 1, (
            f"expected exactly one HNSW swap, got {len(swap_hits)}"
        )
        # Ordering: collapse → cleanup → build → swap → truncate.
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

    asyncio.run(_run())
