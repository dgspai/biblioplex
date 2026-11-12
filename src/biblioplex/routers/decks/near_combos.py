"""``POST /decks/near-combos`` — D14.9.

Extension of `/decks/combos`. Returns every ingested combo whose gap
(cards required but not present in the deck) is between 1 and
`?missing`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from biblioplex.decklist.resolve import resolve_and_fetch_deck_rows
from biblioplex.decklist.sections import parse_sections
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _Engine, get_read_engine

from ._common import _ERROR_RESPONSE, DECKLIST_MAX_LEN


class NearCombosRequest(BaseModel):
    """Body for ``POST /decks/near-combos``."""

    decklist: str = Field(..., description="Plaintext decklist, one entry per line.")


# D14.9 — combos the deck is `:missing` cards away from completing.
# `co.oracle_ids && :deck_oids` (any-overlap) is GIN-backed and narrows
# to combos with at least one card in the deck. The two LATERAL blocks
# split the combo's oracle_ids into (a) the gap set (in the combo, NOT
# in the deck) and (b) the in-deck set. The outer `gap.gap_count BETWEEN
# 1 AND :missing` gates the small-gap window and drops combos already
# fully present (`gap_count = 0`), which is /decks/combos's job.
_NEAR_COMBOS_SQL = (
    "SELECT co.id AS combo_id, "
    "       co.prereqs, "
    "       co.steps, "
    "       co.results, "
    "       gap.gap_names, "
    "       gap.gap_ids, "
    "       gap.gap_count AS missing_count, "
    "       in_deck.in_names "
    "FROM combos co "
    "LEFT JOIN LATERAL ( "
    "    SELECT array_agg(cd.name ORDER BY t.ord) AS gap_names, "
    "           array_agg(cd.id::text ORDER BY t.ord) AS gap_ids, "
    "           count(*)::int AS gap_count "
    "    FROM unnest(co.oracle_ids) WITH ORDINALITY t(oid, ord) "
    "    LEFT JOIN cards cd ON cd.oracle_id = t.oid "
    "    WHERE NOT (t.oid = ANY(CAST(:deck_oids AS uuid[]))) "
    ") gap ON true "
    "LEFT JOIN LATERAL ( "
    "    SELECT array_agg(cd.name ORDER BY t.ord) AS in_names "
    "    FROM unnest(co.oracle_ids) WITH ORDINALITY t(oid, ord) "
    "    JOIN cards cd ON cd.oracle_id = t.oid "
    "    WHERE t.oid = ANY(CAST(:deck_oids AS uuid[])) "
    ") in_deck ON true "
    "WHERE co.oracle_ids && CAST(:deck_oids AS uuid[]) "
    "  AND gap.gap_count BETWEEN 1 AND :missing "
    "  AND (CAST(:ci AS text[]) IS NULL OR co.color_identity <@ CAST(:ci AS text[])) "
    "ORDER BY gap.gap_count ASC, co.id ASC "
    "LIMIT :limit"
)


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/near-combos",
    summary="Combos the deck is N cards away from completing (D14.9)",
    description=(
        "Extension of `/decks/combos`. Returns every ingested combo whose "
        "gap (cards required but not present in the deck) is between 1 "
        "and `?missing` (default 1, cap 3). Response carries the in-deck "
        "and gap card lists per combo so agents can render 'you're 1 card "
        "from Thassa's Oracle — get Demonic Consultation.' GIN-backed via "
        "`combos.oracle_ids && CAST(:deck_oids AS uuid[])` for the pre-filter."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def decklist_near_combos(
    body: NearCombosRequest,
    missing: int = Query(
        default=1,
        ge=1,
        le=3,
        description=(
            "How many cards away from complete. `1` is the useful cEDH "
            "signal; deeper gaps (>3) approach noise and are rejected."
        ),
    ),
    limit: int = Query(
        default=25,
        ge=1,
        le=100,
        description="Max combos to return (sorted by gap ASC).",
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement D14.9."""
    decklist = body.decklist
    if not decklist or not decklist.strip():
        return _scryfall_error(422, "bad_request", "decklist is required")
    if len(decklist) > DECKLIST_MAX_LEN:
        return _scryfall_error(422, "bad_request", "decklist too long")

    sections = parse_sections(decklist)
    names = [e.name for e in sections.main]
    commanders = {e.name.lower() for e in sections.commander}

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        resolved_rows, unresolved = await resolve_and_fetch_deck_rows(conn, names)
        combos_out: list[dict[str, Any]] = []
        if resolved_rows:
            deck_oids = sorted({str(r.row.oracle_id) for r in resolved_rows})
            # A suggestion outside the commander's identity is unplayable.
            cmd_rows = [r for r in resolved_rows if r.input_name.lower() in commanders]
            ci = (
                sorted({c for r in cmd_rows for c in r.row.color_identity or []})
                if cmd_rows
                else None
            )
            result = await conn.execute(
                text(_NEAR_COMBOS_SQL),
                {"deck_oids": deck_oids, "missing": missing, "limit": limit, "ci": ci},
            )
            for row in result.all():
                gap_names = list(row.gap_names or [])
                gap_ids = list(row.gap_ids or [])
                gap = [
                    {"name": n, "id": i}
                    for n, i in zip(gap_names, gap_ids, strict=False)
                ]
                combos_out.append(
                    {
                        "id": row.combo_id,
                        "prereqs": row.prereqs,
                        "steps": row.steps,
                        "results": list(row.results or []),
                        "in_deck": list(row.in_names or []),
                        "gap": gap,
                        "missing": int(row.missing_count),
                    }
                )

    return {
        "object": "deck_near_combos",
        "combos": combos_out,
        "unresolved": unresolved,
    }
