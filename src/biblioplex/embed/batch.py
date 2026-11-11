"""Embed batcher per tech_spec.md §4.2 step 5 + §8.3.

Pulls every ``cards`` row with ``embedding IS NULL``, runs the source
text (``embed_source`` generated column per §3.1 / §8.3) through the
:class:`sentence_transformers.SentenceTransformer` in pages of
``batch_size``, writes the vectors back.

The function is engine + model + batch_size pure — easy to unit-test
with mocks, easy to inject from :class:`RefreshRunner.run_locked` per
D5.3 wiring.

Vectors are written as a string literal cast to ``vector`` so we don't
need pgvector's SQLAlchemy type registered on the bound connection:
``UPDATE cards SET embedding = CAST(:vec AS vector) WHERE id = :id``.
Trade-off: a tiny per-row format cost vs an import-time registration
chore. Per the ponytail lens, the cast wins on simplicity.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Protocol

from sqlalchemy import text

_log = logging.getLogger(__name__)


class _Engine(Protocol):
    def begin(self) -> Any: ...


class _Model(Protocol):
    def encode(self, texts: list[str]) -> Any: ...


# §4.2 step 5: select pending rows ordered by id so pagination is stable
# even when concurrent inserts arrive between batches (only the refresh
# worker writes to cards in practice, but stable ordering keeps tests
# deterministic and future-proofs against background backfills).
#
# D16.3: delta refresh. The OR-arm picks up rows whose `oracle_search` has
# changed since the last embed — `oracle_text_hash` recorded md5 of the
# text that produced the current embedding; a mismatch against a fresh
# md5(oracle_search) means the text drifted and the embedding is stale.
# On steady-state refresh most rows match on both arms (embedding NOT NULL
# and hash equals) and drop out of the pending set — skipping the encode
# entirely. The partial `cards_embedding_pending_idx (D10.14)` still
# backs the IS NULL arm; the drift arm is a bounded seq-scan whose cost
# is dominated by the encode work it avoids.
SELECT_PENDING_SQL: str = (
    "SELECT id, embed_source "
    "FROM cards "
    "WHERE embedding IS NULL "
    "OR oracle_text_hash IS DISTINCT FROM md5(oracle_search) "
    "ORDER BY id "
    "LIMIT :batch"
)

# Cast the bound `vec` (string like "[0.1,0.2,...]") to pgvector at the
# SQL boundary — keeps the call site free of pgvector typing. D16.3: the
# same UPDATE also snapshots `md5(oracle_search)` into `oracle_text_hash`
# so the next refresh's delta gate can compare against the text that
# actually produced this vector.
UPDATE_EMBEDDING_SQL: str = (
    "UPDATE cards SET embedding = CAST(:vec AS vector), "
    "oracle_text_hash = md5(oracle_search) "
    "WHERE id = :id"
)


def _finite_or_zero(x: float) -> float:
    """Return ``x`` unchanged when finite, else ``0.0`` (clamps NaN / ±Inf)."""
    return x if math.isfinite(x) else 0.0


def _vec_to_literal(vec: Any) -> str:
    """Serialize a 1-D vector (list/np.ndarray) to pgvector's ``[a,b,c]`` form.

    Non-finite components (``NaN``, ``±Inf``) are clamped to ``0.0`` before
    serialization — ``repr(float('inf'))`` is ``'inf'``, which pgvector's
    ``CAST(... AS vector)`` rejects, and one rogue component would otherwise
    abort the entire embed batch. Silent clamp keeps refresh forward
    progress; the zero vector is a valid pgvector input (defense in depth
    against degenerate encoder output — D10.7).
    """
    # tolist() handles numpy arrays cleanly; lists pass through `iter` fine.
    if hasattr(vec, "tolist"):
        vec = vec.tolist()
    return "[" + ",".join(repr(_finite_or_zero(float(x))) for x in vec) + "]"


async def embed_pending(
    engine: _Engine,
    model: _Model,
    batch_size: int,
) -> int:
    """Embed every ``cards`` row with NULL ``embedding``.

    Loops: ``SELECT ... LIMIT :batch`` → ``model.encode(texts)`` →
    ``UPDATE cards SET embedding = ... WHERE id = ...`` (one executemany
    per page). Exits when a SELECT page returns zero rows.

    Args:
        engine: SQLAlchemy async engine — same shape used by the refresh
            runner. We open one transaction per page so a long ingest
            doesn't hold a single transaction for the full table.
        model: A SentenceTransformer (or any object with a compatible
            ``encode(list[str]) -> list[list[float]]`` signature).
        batch_size: How many rows to select + encode + update per loop.
            Must be ``>= 1``; pulled from ``EMBED_BATCH`` env in prod.

    Returns:
        Total number of rows whose embedding was written.

    Raises:
        ValueError: If ``batch_size <= 0``.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")

    select_stmt = text(SELECT_PENDING_SQL)
    update_stmt = text(UPDATE_EMBEDDING_SQL)
    total = 0
    batches = 0

    while True:
        async with engine.begin() as conn:
            result = await conn.execute(select_stmt, {"batch": batch_size})
            rows = list(result.all())
            if not rows:
                return total

            texts = [row.embed_source or "" for row in rows]
            vectors = model.encode(texts)

            payload: list[dict[str, Any]] = [
                {"id": row.id, "vec": _vec_to_literal(vec)}
                for row, vec in zip(rows, vectors, strict=True)
            ]
            await conn.execute(update_stmt, payload)
            total += len(rows)
            batches += 1
            if batches % 10 == 0:
                _log.info("embed: %d rows encoded", total)
