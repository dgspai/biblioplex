"""``POST /decks/suggest`` — D13.3, §6.9.

Reuses the `/analyze` tally pipeline (see `_common.py`) to compute
deltas, then for each underfilled slot (delta < 0) returns the top-K
cards carrying that predicted label, ranked by `edhrec_rank` and
optionally constrained to the given commander's color identity.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from biblioplex.classify.rules import TARGETS
from biblioplex.decklist.resolve import resolve_and_fetch_deck_rows
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _decorate, _Engine, get_read_engine

from ._common import (
    _ERROR_RESPONSE,
    DECKLIST_MAX_LEN,
    _maindeck_entries,
    _resolve_commander_ci,
    _tally,
)


class SuggestRequest(BaseModel):
    """Body for ``POST /decks/suggest``.

    ``commander`` is optional — when set we resolve it via the same
    exact-name path ``/cards/named?exact=`` uses and constrain
    suggestions to its ``color_identity`` (subset semantics: any card
    whose color identity is a subset of the commander's).
    """

    decklist: str = Field(..., description="Plaintext decklist, one entry per line.")
    commander: str | None = Field(
        default=None,
        description="Optional commander name; constrains suggestions to color identity.",
    )


SUGGEST_LIMIT_MAX = 20
SUGGEST_LIMIT_DEFAULT = 5


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/suggest",
    summary="Suggest fills for underfilled slot targets",
    description=(
        "Runs the /analyze pipeline, then for each slot with a negative "
        "delta returns the top-K cards carrying that predicted label — "
        "ranked by `edhrec_rank ASC NULLS LAST`, filtered to exclude "
        "cards already in the decklist, and (when `commander` is set) "
        "constrained to the commander's color identity. `limit` is per-"
        "slot and clamps to [1, 20] with a default of 5. §6.9."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def suggest_decklist(
    body: SuggestRequest,
    limit: int = Query(
        default=SUGGEST_LIMIT_DEFAULT,
        ge=1,
        le=SUGGEST_LIMIT_MAX,
        description="Per-slot suggestion count.",
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement §6.9."""
    decklist = body.decklist
    if not decklist or not decklist.strip():
        return _scryfall_error(422, "bad_request", "decklist is required")
    if len(decklist) > DECKLIST_MAX_LEN:
        return _scryfall_error(422, "bad_request", "decklist too long")

    entries = _maindeck_entries(decklist)
    names = [e.name for e in entries]

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        resolved_rows, unresolved_from_fetch = await resolve_and_fetch_deck_rows(
            conn, names
        )

        commander_ci: list[str] | None = None
        if body.commander:
            result = await _resolve_commander_ci(conn, body.commander)
            if isinstance(result, JSONResponse):
                return result
            commander_ci = result

        t = _tally(entries, resolved_rows, unresolved_from_fetch)

        # D15.7: fold the per-slot loop into ONE query. `unnest(:labels)`
        # drives a LATERAL top-K over predicted_labels per underfilled
        # label — up to 5× fewer round-trips on a fresh precon-style deck.
        suggestions: dict[str, list[dict[str, Any]]] = {}
        seen_ids_list = list(t.seen_ids)
        underfilled = [lab for lab, delta in t.deltas.items() if delta < 0]
        if underfilled:
            params: dict[str, Any] = {
                "labels": underfilled,
                "seen": seen_ids_list,
                "limit": limit,
            }
            where_ci = ""
            if commander_ci is not None:
                where_ci = "AND c.color_identity <@ :ci "
                params["ci"] = commander_ci
            sql = (
                "SELECT lab.label AS label, "  # noqa: S608
                "       cand.raw AS raw, "
                "       cand.predicted_labels AS predicted_labels, "
                "       cand.label_scores AS label_scores "
                "FROM unnest(CAST(:labels AS text[])) AS lab(label) "
                "CROSS JOIN LATERAL ( "
                "  SELECT c.raw, c.predicted_labels, c.label_scores, c.edhrec_rank "
                "  FROM cards c "
                "  WHERE lab.label = ANY(c.predicted_labels) "
                "  AND c.id <> ALL(:seen) "
                "  AND c.edhrec_rank IS NOT NULL "
                f"  {where_ci}"
                "  ORDER BY c.edhrec_rank ASC "
                "  LIMIT :limit "
                ") cand "
                "ORDER BY lab.label, cand.edhrec_rank ASC"
            )
            result = await conn.execute(text(sql), params)
            # Ensure every underfilled slot appears in the response, even
            # if the LATERAL returned zero rows for it.
            suggestions = {lab: [] for lab in underfilled}
            for row in result.all():
                suggestions[row.label].append(
                    _decorate(row.raw, row.predicted_labels, row.label_scores)
                )

    return {
        "object": "deck_suggestions",
        "deltas": t.deltas,
        "targets": dict(TARGETS),
        "suggestions": suggestions,
        "commander_color_identity": commander_ci,
        "unresolved": t.unresolved,
    }
