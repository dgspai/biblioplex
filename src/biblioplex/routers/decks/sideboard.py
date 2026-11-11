"""``POST /decks/sideboard-suggest`` — D14.12, §6.17.

Fixed meta-hate slugs seed the sideboard suggestions. Reuses
`suggest.SuggestRequest` — same body shape as `/decks/suggest`
(decklist + optional commander).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text

from biblioplex.decklist.resolve import resolve_and_fetch_deck_rows
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _decorate, _Engine, get_read_engine

from ._common import _ERROR_RESPONSE, DECKLIST_MAX_LEN, _maindeck_entries, _resolve_commander_ci
from .suggest import SuggestRequest

# Fixed meta-hate slugs seed the sideboard. Every value must match a
# real `oracle_tags.tag` slug for a populated corpus to return rows —
# stock deployments without a tags ingest return empty per-slug lists.
SIDEBOARD_SLUGS: tuple[str, ...] = (
    "graveyard-hate",
    "artifact-removal",
    "enchantment-removal",
    "counterspell",
    "board-wipe",
)

SIDEBOARD_LIMIT_MAX = 25
SIDEBOARD_LIMIT_DEFAULT = 15


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/sideboard-suggest",
    summary="Suggest sideboard cards for common meta-hate slots (D14.12)",
    description=(
        "Fixed slug set: `graveyard-hate`, `artifact-removal`, "
        "`enchantment-removal`, `counterspell`, `board-wipe`. Per slug, "
        "LATERAL top-K candidates (default 15) ranked by `edhrec_rank "
        "ASC`, excluding cards already in the deck and optionally scoped "
        "to the commander's color identity. Depends on a completed "
        "oracle-tags ingest — empty response on stock deployments "
        "without tagger data."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def sideboard_suggest_decklist(
    body: SuggestRequest,
    limit: int = Query(
        default=SIDEBOARD_LIMIT_DEFAULT,
        ge=1,
        le=SIDEBOARD_LIMIT_MAX,
        description="Per-slug candidate count.",
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement §6.17."""
    decklist = body.decklist
    if not decklist or not decklist.strip():
        return _scryfall_error(422, "bad_request", "decklist is required")
    if len(decklist) > DECKLIST_MAX_LEN:
        return _scryfall_error(422, "bad_request", "decklist too long")

    entries = _maindeck_entries(decklist)
    names = [e.name for e in entries]

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        resolved_rows, unresolved = await resolve_and_fetch_deck_rows(conn, names)

        commander_ci: list[str] | None = None
        if body.commander:
            result = await _resolve_commander_ci(conn, body.commander)
            if isinstance(result, JSONResponse):
                return result
            commander_ci = result

        seen_ids = [str(r.row.id) for r in resolved_rows]
        params: dict[str, Any] = {
            "slugs": list(SIDEBOARD_SLUGS),
            "seen": seen_ids,
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
            "FROM unnest(CAST(:slugs AS text[])) AS lab(label) "
            "CROSS JOIN LATERAL ( "
            "  SELECT c.raw, c.predicted_labels, c.label_scores, c.edhrec_rank "
            "  FROM cards c "
            "  WHERE EXISTS ( "
            "    SELECT 1 FROM oracle_tags ot "
            "    WHERE ot.oracle_id = c.oracle_id AND ot.tag = lab.label "
            "  ) "
            "  AND c.id <> ALL(CAST(:seen AS uuid[])) "
            "  AND c.edhrec_rank IS NOT NULL "
            f"  {where_ci}"
            "  ORDER BY c.edhrec_rank ASC "
            "  LIMIT :limit "
            ") cand "
            "ORDER BY lab.label, cand.edhrec_rank ASC"
        )
        result = await conn.execute(text(sql), params)
        suggestions: dict[str, list[dict[str, Any]]] = {
            slug: [] for slug in SIDEBOARD_SLUGS
        }
        for row in result.all():
            suggestions[row.label].append(
                _decorate(row.raw, row.predicted_labels, row.label_scores)
            )

    return {
        "object": "deck_sideboard",
        "suggestions": suggestions,
        "commander_color_identity": commander_ci,
        "unresolved": unresolved,
    }
