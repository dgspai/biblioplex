"""``POST /decks/synergy`` — D14.19.

Pairwise card co-occurrence signal (shared tags + combos) over the
resolved deck's oracle_ids.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from biblioplex.decklist.resolve import resolve_and_fetch_deck_rows
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _Engine, get_read_engine

from ._common import _ERROR_RESPONSE, DECKLIST_MAX_LEN, _maindeck_entries


class SynergyRequest(BaseModel):
    """Body for ``POST /decks/synergy`` — pairwise co-occurrence signal."""

    decklist: str = Field(
        ..., description="Plaintext decklist, one entry per line."
    )


SYNERGY_LIMIT_MAX = 100
SYNERGY_LIMIT_DEFAULT = 25


# D14.19 — pairwise co-occurrence signal. `oracle_tags a JOIN oracle_tags b
# ON a.tag = b.tag AND a.oracle_id < b.oracle_id` gives the unordered pair
# fold; the same shape over `combos.oracle_ids` (via LATERAL unnest to
# enumerate the (a, b) pair per combo) covers the combo signal. Both
# aggregate per-pair then join on the pair columns; `score = n_tags +
# 5 * n_combos`. The Bug.27 rule applies: `CAST(:X AS TYPE)` NOT
# `:X::TYPE`. `oracle_tags` GIN on `oracle_id` + PK-ish on
# `combos.oracle_ids` back the ANY(...) predicates.
_SYNERGY_SQL = """
WITH tag_pairs AS (
    SELECT ta.oracle_id AS a_oid, tb.oracle_id AS b_oid,
           array_agg(DISTINCT ta.tag ORDER BY ta.tag) AS shared_tags,
           count(DISTINCT ta.tag)::int AS n_tags
    FROM oracle_tags ta
    JOIN oracle_tags tb ON tb.tag = ta.tag
        AND ta.oracle_id < tb.oracle_id
    WHERE ta.oracle_id = ANY(CAST(:deck_oids AS uuid[]))
      AND tb.oracle_id = ANY(CAST(:deck_oids AS uuid[]))
    GROUP BY 1, 2
),
combo_pairs AS (
    SELECT ea.oid AS a_oid, eb.oid AS b_oid,
           array_agg(DISTINCT co.id ORDER BY co.id) AS shared_combos,
           count(DISTINCT co.id)::int AS n_combos
    FROM combos co,
         unnest(co.oracle_ids) ea(oid),
         unnest(co.oracle_ids) eb(oid)
    WHERE ea.oid < eb.oid
      AND ea.oid = ANY(CAST(:deck_oids AS uuid[]))
      AND eb.oid = ANY(CAST(:deck_oids AS uuid[]))
    GROUP BY 1, 2
),
pair_scores AS (
    SELECT COALESCE(t.a_oid, c.a_oid) AS a_oid,
           COALESCE(t.b_oid, c.b_oid) AS b_oid,
           COALESCE(t.shared_tags, ARRAY[]::text[]) AS shared_tags,
           COALESCE(t.n_tags, 0) AS n_tags,
           COALESCE(c.shared_combos, ARRAY[]::text[]) AS shared_combos,
           COALESCE(c.n_combos, 0) AS n_combos,
           COALESCE(t.n_tags, 0) + 5 * COALESCE(c.n_combos, 0) AS score
    FROM tag_pairs t
    FULL OUTER JOIN combo_pairs c
      ON c.a_oid = t.a_oid AND c.b_oid = t.b_oid
)
SELECT ps.a_oid, ps.b_oid,
       ca.name AS a_name, cb.name AS b_name,
       ca.id AS a_id, cb.id AS b_id,
       ps.shared_tags, ps.n_tags,
       ps.shared_combos, ps.n_combos,
       ps.score
FROM pair_scores ps
JOIN cards ca ON ca.oracle_id = ps.a_oid
JOIN cards cb ON cb.oracle_id = ps.b_oid
WHERE ps.score > 0
ORDER BY ps.score DESC, ca.name ASC, cb.name ASC
LIMIT :limit
"""


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/synergy",
    summary="Pairwise card co-occurrence signal (shared tags + combos)",
    description=(
        "For each unordered pair of resolved deck oracle_ids, computes "
        "(a) count of shared `oracle_tags.tag`, and (b) count of combos "
        "where both ids appear. `score = shared_tags + 5 * shared_combos` "
        "(combos are much stronger signal than sharing 'flying'). Top "
        "`limit` pairs sorted by score DESC. Empty on stock deployments "
        "without a completed oracle-tags OR combos ingest — no schema "
        "delta. Companion to `/decks/combos` and `/decks/near-combos`."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def synergy_decklist(
    body: SynergyRequest,
    limit: int = Query(
        default=SYNERGY_LIMIT_DEFAULT,
        ge=1,
        le=SYNERGY_LIMIT_MAX,
        description="Max number of synergy pairs to return.",
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement §6.22."""
    decklist = body.decklist
    if not decklist or not decklist.strip():
        return _scryfall_error(422, "bad_request", "decklist is required")
    if len(decklist) > DECKLIST_MAX_LEN:
        return _scryfall_error(422, "bad_request", "decklist too long")

    entries = _maindeck_entries(decklist)
    names = [e.name for e in entries]

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        resolved_rows, unresolved = await resolve_and_fetch_deck_rows(conn, names)
        pairs: list[dict[str, Any]] = []
        if resolved_rows:
            deck_oids = [str(r.row.oracle_id) for r in resolved_rows]
            result = await conn.execute(
                text(_SYNERGY_SQL), {"deck_oids": deck_oids, "limit": limit}
            )
            for row in result.all():
                pairs.append({
                    "a": {"id": str(row.a_id), "name": row.a_name},
                    "b": {"id": str(row.b_id), "name": row.b_name},
                    "score": int(row.score),
                    "shared_tags": list(row.shared_tags or []),
                    "shared_combos": list(row.shared_combos or []),
                })

    return {
        "object": "deck_synergy",
        "pairs": pairs,
        "unresolved": unresolved,
    }
