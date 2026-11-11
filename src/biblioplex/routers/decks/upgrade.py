"""``POST /decks/upgrade`` — D14.5 (+D14.14 legality gate).

Proposes per-card swap upgrades ranked by EDHrec-rank delta.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from biblioplex.decklist.resolve import resolve_and_fetch_deck_rows
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _decorate, _Engine, get_read_engine

from ._common import (
    _ERROR_RESPONSE,
    DECKLIST_MAX_LEN,
    _maindeck_entries,
    _resolve_commander_ci,
)


class UpgradeRequest(BaseModel):
    """Body for ``POST /decks/upgrade``.

    ``budget_usd`` caps candidates by ``cards.price_usd`` (NULL prices
    survive so unpriced upgrades aren't excluded — treat missing price
    as "unknown, don't filter" rather than "expensive").
    """

    decklist: str = Field(..., description="Plaintext decklist, one entry per line.")
    commander: str | None = Field(
        default=None,
        description="Optional commander name; constrains upgrades to its color identity.",
    )
    budget_usd: float | None = Field(
        default=None,
        gt=0,
        description="Optional per-card USD cap for candidate cards.",
    )


UPGRADE_LIMIT_MAX = 25
UPGRADE_LIMIT_DEFAULT = 10


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/upgrade",
    summary="Propose per-card swap upgrades ranked by EDHrec-rank delta (D14.5)",
    description=(
        "For each card in the deck with an EDHrec rank and at least one "
        "predicted label, find the single most-played (`edhrec_rank ASC`) "
        "alternative card that (a) shares the current card's dominant "
        "predicted label, (b) isn't already in the deck, (c) has a lower "
        "(better) rank than the current card, (d) fits the commander's "
        "color identity if `commander` is supplied, and (e) is under "
        "`budget_usd` if supplied. Swaps rank by rank-delta (larger = "
        "bigger upgrade). Empty `swaps` when the deck is already "
        "high-power or the classifier scored no cards with labels."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def decklist_upgrade(
    body: UpgradeRequest,
    limit: int = Query(
        default=UPGRADE_LIMIT_DEFAULT,
        ge=1,
        le=UPGRADE_LIMIT_MAX,
        description="Max number of swap suggestions to return.",
    ),
    fmt: str | None = Query(
        default=None,
        description=(
            "D14.14 — Format slug for legality gating on candidate rows. "
            "Defaults to `commander` when a commander name is supplied "
            "and `vintage` otherwise (vintage bans the fewest cards). "
            "Matches Scryfall's `legalities` map keys."
        ),
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement D14.5 (+D14.14 legality gate)."""
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

        # D14.14 — pick default format when caller doesn't supply one.
        fmt_effective = (fmt or "").strip().lower() or (
            "commander" if body.commander else "vintage"
        )

        swaps: list[dict[str, Any]] = []
        if resolved_rows:
            deck_ids = [str(r.row.id) for r in resolved_rows]
            params: dict[str, Any] = {
                "deck_ids": deck_ids,
                "limit": limit,
                "fmt": fmt_effective,
            }
            where_ci = ""
            if commander_ci is not None:
                where_ci = "AND c.color_identity <@ :ci "
                params["ci"] = commander_ci
            where_budget = ""
            if body.budget_usd is not None:
                where_budget = "AND (c.price_usd IS NULL OR c.price_usd <= :budget) "
                params["budget"] = body.budget_usd
            # D14.14 — legality gate on the candidate row. `legalities` is
            # a Scryfall JSONB map keyed on lowercased format slugs; a
            # value of `'legal'` (or `'restricted'` in Vintage) means the
            # card can appear. We accept only `'legal'` for the upgrade
            # suggestion — a card the caller could restrict-copy is
            # already awkward; suggesting it as a strict upgrade is worse.
            where_legality = "AND c.legalities->>:fmt = 'legal' "
            # Bug.2 fix: dominant label = argmax(label_scores) restricted to
            # `predicted_labels`. Prior code used `predicted_labels[1]`
            # (Postgres 1-indexed first element) which was alphabetically-
            # earliest by LABELS tuple order, not the highest-scoring slot.
            sql = (
                "SELECT dc.id AS out_id, "  # noqa: S608
                "       dc.name AS out_name, "
                "       dc.edhrec_rank AS out_rank, "
                "       cand.id AS in_id, "
                "       cand.name AS in_name, "
                "       cand.raw AS in_raw, "
                "       cand.edhrec_rank AS in_rank, "
                "       cand.predicted_labels, "
                "       cand.label_scores, "
                "       dom.dom_label AS matched_label, "
                "       (dc.edhrec_rank - cand.edhrec_rank) AS score "
                "FROM cards dc "
                "CROSS JOIN LATERAL ( "
                "  SELECT ls.key AS dom_label "
                "  FROM jsonb_each_text(dc.label_scores) ls "
                "  WHERE ls.key = ANY(dc.predicted_labels) "
                "  ORDER BY ls.value::float DESC, ls.key ASC "
                "  LIMIT 1 "
                ") dom "
                "CROSS JOIN LATERAL ( "
                "  SELECT c.id, c.name, c.raw, c.edhrec_rank, "
                "         c.predicted_labels, c.label_scores "
                "  FROM cards c "
                "  WHERE dom.dom_label = ANY(c.predicted_labels) "
                "  AND c.id <> ALL(CAST(:deck_ids AS uuid[])) "
                "  AND c.edhrec_rank IS NOT NULL "
                "  AND c.edhrec_rank < dc.edhrec_rank "
                f"  {where_ci}"
                f"  {where_budget}"
                f"  {where_legality}"
                "  ORDER BY c.edhrec_rank ASC "
                "  LIMIT 1 "
                ") cand "
                "WHERE dc.id = ANY(CAST(:deck_ids AS uuid[])) "
                "AND dc.edhrec_rank IS NOT NULL "
                "ORDER BY score DESC "
                "LIMIT :limit"
            )
            result = await conn.execute(text(sql), params)
            for row in result.all():
                swaps.append(
                    {
                        "out": {
                            "id": str(row.out_id),
                            "name": row.out_name,
                            "edhrec_rank": row.out_rank,
                        },
                        "in": _decorate(
                            row.in_raw, row.predicted_labels, row.label_scores
                        ),
                        "matched_label": row.matched_label,
                        "score": int(row.score),
                    }
                )

    return {
        "object": "deck_upgrades",
        "swaps": swaps,
        "commander_color_identity": commander_ci,
        "format": fmt_effective,
        "unresolved": unresolved,
    }
