"""``POST /decks/combos`` — D14.3."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text

from biblioplex.decklist.resolve import resolve_and_fetch_deck_rows
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _Engine, get_read_engine

from ._common import _ERROR_RESPONSE, DECKLIST_MAX_LEN, _maindeck_entries


class CombosRequest(BaseModel):
    """Body for ``POST /decks/combos`` — decklist-scoped combo lookup."""

    decklist: str = Field(..., description="Plaintext decklist, one entry per line.")


_COMBOS_SUBSET_SQL = (
    "SELECT co.id AS combo_id, "
    "       co.prereqs, "
    "       co.steps, "
    "       co.results, "
    "       ( "
    "         SELECT array_agg(cd.name ORDER BY t.ord) "
    "         FROM unnest(co.oracle_ids) WITH ORDINALITY t(oid, ord) "
    "         JOIN cards cd ON cd.oracle_id = t.oid "
    "       ) AS card_names "
    "FROM combos co "
    "WHERE co.oracle_ids <@ CAST(:deck_oids AS uuid[]) "
    "ORDER BY co.id"
)


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/combos",
    summary="Commander Spellbook combos present in a decklist (D14.3)",
    description=(
        "Parses a plaintext decklist, resolves names against `cards` "
        "(exact + trigram fallback at 0.4), and returns every ingested "
        "combo whose full card set is a subset of the resolved deck's "
        "`oracle_id`s (`combos.oracle_ids <@ deck_oracle_ids`). Empty "
        "combos list when the corpus has no matches; unresolved names "
        "surface in a separate field like `/decks/analyze`."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def decklist_combos(
    body: CombosRequest,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement D14.3."""
    decklist = body.decklist
    if not decklist or not decklist.strip():
        return _scryfall_error(422, "bad_request", "decklist is required")
    if len(decklist) > DECKLIST_MAX_LEN:
        return _scryfall_error(422, "bad_request", "decklist too long")

    entries = _maindeck_entries(decklist)
    names = [e.name for e in entries]

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        resolved_rows, unresolved = await resolve_and_fetch_deck_rows(conn, names)
        # ponytail: skip the combos SQL when the deck resolves to zero
        # cards — the subset predicate on an empty array trivially
        # matches every combo with an empty `oracle_ids`, which we don't
        # want to surface (and never happens for real data anyway).
        combos_out: list[dict[str, Any]] = []
        if resolved_rows:
            deck_oids = sorted({str(r.row.oracle_id) for r in resolved_rows})
            result = await conn.execute(
                text(_COMBOS_SUBSET_SQL),
                {"deck_oids": deck_oids},
            )
            for row in result.all():
                combos_out.append(
                    {
                        "id": row.combo_id,
                        "cards": list(row.card_names or []),
                        "prereqs": row.prereqs,
                        "steps": row.steps,
                        "results": list(row.results or []),
                    }
                )

    return {
        "object": "deck_combos",
        "combos": combos_out,
        "unresolved": unresolved,
    }
