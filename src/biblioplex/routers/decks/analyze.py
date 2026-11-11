"""``POST /decks/analyze`` — tech_spec.md §6.7.

Consumer-side companion to the rules-layer classifier. Takes a plaintext
decklist, resolves names against ``cards``, tallies per-label counts
across resolved cards (multilabel — a card with two slots counts in
both), and reports deltas against the §6.7 ``TARGETS`` template.

Pipeline: ``parse_decklist`` → ``resolve_and_fetch_deck_rows`` (one CTE
combining exact match, per-miss trigram fallback, and slot-column join)
→ tally. Single DB round-trip per request as of D13.4 (previously 2).

See ``_common.py`` for the shared tally + land-detection machinery
(``_tally``, ``_is_land``, ``_analyze_pipeline``) reused by `/compare`
and `/suggest`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _Engine, get_read_engine

from ._common import _ERROR_RESPONSE, DECKLIST_MAX_LEN, _analyze_pipeline


class AnalyzeRequest(BaseModel):
    """Body for ``POST /decks/analyze``."""

    decklist: str = Field(..., description="Plaintext decklist, one entry per line.")


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/analyze",
    summary="Analyze a decklist against the template slot targets",
    description=(
        "Parses a plaintext decklist, resolves names against `cards` "
        "(exact + trigram fallback at 0.4), and reports per-slot counts, "
        "targets, and deltas across `predicted_labels`. Multilabel: a "
        "card may contribute to several slots. Land is counted but not "
        "graded against `targets` (structural slot). See §6.7."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def analyze_decklist(
    body: AnalyzeRequest,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement §6.7."""
    decklist = body.decklist
    if not decklist or not decklist.strip():
        return _scryfall_error(422, "bad_request", "decklist is required")
    if len(decklist) > DECKLIST_MAX_LEN:
        return _scryfall_error(422, "bad_request", "decklist too long")

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        return await _analyze_pipeline(decklist, conn)
