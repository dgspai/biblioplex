"""Tests for the embed batcher (D5.3, tech_spec.md §4.2 step 5 + §8.3).

`embed_pending(engine, model, batch_size)` repeatedly selects
unembedded rows, runs them through ``model.encode``, and writes the
vectors back. The function returns the total number of rows updated;
it loops until a SELECT page comes back empty.

Tests use a fake async engine / connection that records SQL execution
order and parameters. The model is a MagicMock returning fake vectors.
No DB, no HF download.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from biblioplex.embed.batch import (
    SELECT_PENDING_SQL,
    UPDATE_EMBEDDING_SQL,
    _vec_to_literal,
    embed_pending,
)


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one(self) -> Any:
        return self._rows[0] if self._rows else None


class _Row(tuple[Any, Any]):
    """Tiny row stub exposing .id / .embed_source like a SQLAlchemy Row."""

    __slots__ = ()

    def __new__(cls, id_: int, embed_source: str) -> _Row:
        return tuple.__new__(cls, (id_, embed_source))

    @property
    def id(self) -> int:
        return self[0]

    @property
    def embed_source(self) -> str:
        return self[1]


class _FakeConn:
    """Records every execute() call: (sql, params).

    Serves SELECT pages from `pending_pages` in order; UPDATE returns
    a benign result. The contextmanager-on-begin pattern matches the
    SQLAlchemy AsyncEngine surface used by the rest of the codebase.
    """

    def __init__(self, pending_pages: list[list[_Row]]) -> None:
        self._pages = list(pending_pages)
        self.executed: list[tuple[str, Any]] = []
        self.update_params: list[Any] = []

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append((sql, params))
        if "SELECT" in sql and "embedding IS NULL" in sql:
            if self._pages:
                return _FakeResult(self._pages.pop(0))
            return _FakeResult([])
        if "UPDATE cards SET embedding" in sql:
            # SQLAlchemy executemany passes a list of dicts.
            if isinstance(params, list):
                self.update_params.extend(params)
            elif params is not None:
                self.update_params.append(params)
            return _FakeResult([])
        return _FakeResult([])


class _FakeEngine:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def begin(self) -> _FakeConn:
        return self._conn


def _make_model(dim: int = 4) -> Any:
    """Model stub: encode(texts) returns one dim-long vector per text."""

    class _M:
        calls: list[list[str]] = []

        def encode(self, texts: list[str], **_kw: Any) -> list[list[float]]:
            self.calls.append(list(texts))
            return [[float(i + j) for j in range(dim)] for i, _ in enumerate(texts)]

    return _M()


# --- Basic behavior --------------------------------------------------------


async def test_embed_pending_no_rows_returns_zero() -> None:
    """No pending rows → zero updates, zero encode calls."""
    conn = _FakeConn(pending_pages=[])
    engine = _FakeEngine(conn)
    model = _make_model()

    total = await embed_pending(engine, model, batch_size=10)

    assert total == 0
    assert model.calls == []
    # Exactly one SELECT issued (returns empty, loop exits).
    selects = [s for s, _ in conn.executed if "SELECT" in s]
    assert len(selects) == 1


async def test_embed_pending_single_batch() -> None:
    """One page of N rows where N <= batch → one encode, N updates."""
    pages = [[_Row(1, "alpha"), _Row(2, "beta"), _Row(3, "gamma")]]
    conn = _FakeConn(pending_pages=pages)
    engine = _FakeEngine(conn)
    model = _make_model()

    total = await embed_pending(engine, model, batch_size=10)

    assert total == 3
    assert len(model.calls) == 1
    assert model.calls[0] == ["alpha", "beta", "gamma"]
    assert len(conn.update_params) == 3
    # Each update payload carries id + vec.
    for p in conn.update_params:
        assert "id" in p and "vec" in p


async def test_embed_pending_loops_until_empty() -> None:
    """Multiple pages → loop continues until SELECT returns []."""
    pages = [
        [_Row(1, "a"), _Row(2, "b")],
        [_Row(3, "c"), _Row(4, "d")],
        [_Row(5, "e")],
    ]
    conn = _FakeConn(pending_pages=pages)
    engine = _FakeEngine(conn)
    model = _make_model()

    total = await embed_pending(engine, model, batch_size=2)

    assert total == 5
    assert len(model.calls) == 3  # one per page
    assert len(conn.update_params) == 5


# --- SQL contract checks --------------------------------------------------


def test_select_sql_targets_embedding_null_and_orders_by_id() -> None:
    assert "embedding IS NULL" in SELECT_PENDING_SQL
    assert "ORDER BY id" in SELECT_PENDING_SQL
    assert "LIMIT" in SELECT_PENDING_SQL


def test_update_sql_casts_to_vector() -> None:
    # We pass the vector as a string literal cast to `vector` so we don't
    # need pgvector's SQLAlchemy type registered on the bound connection.
    assert "UPDATE cards SET embedding" in UPDATE_EMBEDDING_SQL
    assert "vector" in UPDATE_EMBEDDING_SQL.lower()
    assert "WHERE id" in UPDATE_EMBEDDING_SQL


# --- D16.3: delta refresh — skip embed on unchanged oracle_text ------------
#
# `oracle_text_hash` records the hash of the text that produced the current
# embedding. Collapse never touches this column, so a row whose oracle_search
# is unchanged since last embed keeps its old hash — comparing against a
# freshly-computed `md5(oracle_search)` matches, and the row drops out of
# the pending set. Cold boot (embedding IS NULL) still fires via the OR arm.


def test_d16_3_select_sql_folds_hash_drift_into_pending_predicate() -> None:
    """SELECT_PENDING_SQL picks up NULL-embedding rows OR hash-drifted rows.

    Empty `oracle_text_hash` (the DEFAULT for freshly-migrated rows) will
    never match any real `md5(oracle_search)` string, so the first delta
    refresh after the in-place ALTER re-embeds every row exactly once.
    Subsequent refreshes short-circuit whenever the text is unchanged.
    """
    assert "embedding IS NULL" in SELECT_PENDING_SQL
    assert "oracle_text_hash IS DISTINCT FROM md5(oracle_search)" in SELECT_PENDING_SQL


def test_d16_3_update_sql_writes_hash_alongside_embedding() -> None:
    """UPDATE writes both the new embedding and its source-text hash.

    Writing `md5(oracle_search)` from the row itself (rather than a bound
    scalar) means the recorded hash always reflects the text that was
    embedded in this pass — safe under concurrent staging updates because
    the outer refresh advisory lock serializes with any writer to
    `oracle_search`.
    """
    assert "embedding = CAST(:vec AS vector)" in UPDATE_EMBEDDING_SQL
    assert "oracle_text_hash = md5(oracle_search)" in UPDATE_EMBEDDING_SQL


# --- Property tests (M5 mandate) ------------------------------------------


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    n=st.integers(min_value=0, max_value=37),
    batch=st.integers(min_value=1, max_value=8),
)
async def test_embed_pending_issues_n_updates_and_ceil_n_over_b_encodes(
    n: int, batch: int
) -> None:
    """For arbitrary N pending rows + batch B: exactly N updates, ceil(N/B) encode calls."""
    rows = [_Row(i + 1, f"src-{i}") for i in range(n)]
    # Slice into pages of `batch` to mimic LIMIT pagination.
    pages = [rows[i : i + batch] for i in range(0, len(rows), batch)]
    conn = _FakeConn(pending_pages=pages)
    engine = _FakeEngine(conn)
    model = _make_model()

    total = await embed_pending(engine, model, batch_size=batch)

    assert total == n
    assert len(model.calls) == math.ceil(n / batch) if n > 0 else len(model.calls) == 0
    assert len(conn.update_params) == n


@given(batch_size=st.integers(min_value=-5, max_value=0))
def test_embed_pending_rejects_nonpositive_batch_size(batch_size: int) -> None:
    """`batch_size <= 0` is a configuration error — refuse it at the boundary."""
    conn = _FakeConn(pending_pages=[])
    engine = _FakeEngine(conn)
    model = _make_model()

    async def _run() -> None:
        await embed_pending(engine, model, batch_size=batch_size)

    import asyncio

    with pytest.raises(ValueError, match="batch_size"):
        asyncio.run(_run())


# --- D10.7: non-finite component clamp (NaN/+Inf/-Inf → 0.0) ---------------
#
# `repr(float('inf'))` / `repr(float('nan'))` produce literal tokens `'inf'` /
# `'nan'` which pgvector's `CAST(... AS vector)` rejects, killing the whole
# refresh batch. `_vec_to_literal` defensively clamps non-finite components
# to `0.0` so one rogue vector cannot abort the embed pass.


def _parse_literal(literal: str) -> list[float]:
    """Parse `[a,b,c,...]` back into a list of floats (round-trip helper)."""
    assert literal.startswith("[") and literal.endswith("]")
    inner = literal[1:-1]
    if not inner:
        return []
    return [float(tok) for tok in inner.split(",")]


def _has_nonfinite_token(literal: str) -> bool:
    """True iff the rendered literal contains any inf/nan/Infinity/NaN token (case-insensitive)."""
    low = literal.lower()
    return "inf" in low or "nan" in low


def test_vec_to_literal_clamps_positive_infinity() -> None:
    """A `+inf` component renders as `0.0` (not `'inf'`) at the same position."""
    vec = [0.1, float("inf"), 0.3]
    literal = _vec_to_literal(vec)

    assert not _has_nonfinite_token(literal)
    parsed = _parse_literal(literal)
    assert len(parsed) == 3
    assert parsed[1] == 0.0
    # Untouched neighbors round-trip to the original float value.
    assert parsed[0] == 0.1
    assert parsed[2] == 0.3


def test_vec_to_literal_clamps_negative_infinity() -> None:
    """A `-inf` component renders as `0.0` (not `'-inf'`) at the same position."""
    vec = [float("-inf"), 0.5, 0.6]
    literal = _vec_to_literal(vec)

    assert not _has_nonfinite_token(literal)
    parsed = _parse_literal(literal)
    assert parsed[0] == 0.0
    assert parsed[1] == 0.5
    assert parsed[2] == 0.6


def test_vec_to_literal_clamps_nan() -> None:
    """A `nan` component renders as `0.0` (not `'nan'`) at the same position."""
    vec = [0.7, 0.8, float("nan")]
    literal = _vec_to_literal(vec)

    assert not _has_nonfinite_token(literal)
    parsed = _parse_literal(literal)
    assert parsed[0] == 0.7
    assert parsed[1] == 0.8
    assert parsed[2] == 0.0


def test_vec_to_literal_preserves_finite_vector() -> None:
    """All-finite input: regression guard — output matches prior behavior."""
    vec = [0.0, -1.5, 2.25, 3.5]
    literal = _vec_to_literal(vec)

    assert literal == "[" + ",".join(repr(float(x)) for x in vec) + "]"
    assert _parse_literal(literal) == [0.0, -1.5, 2.25, 3.5]


def test_vec_to_literal_handles_numpy_array_via_tolist() -> None:
    """Numpy-array input is accepted via `tolist()` (existing branch is preserved)."""

    class _FakeNdArray:
        """Minimal `.tolist()` stub — avoids a numpy dep in this test file."""

        def __init__(self, values: list[float]) -> None:
            self._values = values

        def tolist(self) -> list[float]:
            return list(self._values)

    arr = _FakeNdArray([0.1, float("inf"), float("nan"), -0.4])
    literal = _vec_to_literal(arr)

    assert not _has_nonfinite_token(literal)
    parsed = _parse_literal(literal)
    assert parsed == [0.1, 0.0, 0.0, -0.4]


def test_vec_to_literal_never_emits_nonfinite_tokens_for_mixed_vector() -> None:
    """Belt-and-suspenders: rendered literal never contains inf/nan/Infinity/NaN substrings."""
    vec = [
        float("inf"),
        float("-inf"),
        float("nan"),
        0.1,
        -0.2,
        float("inf"),
    ]
    literal = _vec_to_literal(vec)

    assert not _has_nonfinite_token(literal)
    parsed = _parse_literal(literal)
    assert parsed == [0.0, 0.0, 0.0, 0.1, -0.2, 0.0]
