"""``POST /decks/diff`` — D14.15, §6.18.

Pure Python line-level diff on parsed decklists — no SQL, no resolve.
Complements `/decks/compare` (analyze-shaped stats per side).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from biblioplex.decklist.diff import diff_qty_maps
from biblioplex.errors import scryfall_error as _scryfall_error

from ._common import _ERROR_RESPONSE, DECKLIST_MAX_LEN, _whole_list_entries


class DiffRequest(BaseModel):
    """Body for ``POST /decks/diff`` — pure line-level decklist diff."""

    a: str = Field(..., description="First plaintext decklist (baseline).")
    b: str = Field(..., description="Second plaintext decklist (compared).")


def _decklist_qty_map(text: str) -> dict[str, tuple[str, int]]:
    """Fold a decklist into ``lowered_name -> (display_name, qty)``.

    Sums duplicate lines. Display name = the first-seen casing of that
    name in the source; diff results show the canonicalised (parsed)
    form, not the raw string.
    """
    out: dict[str, tuple[str, int]] = {}
    for e in _whole_list_entries(text):
        key = e.name.lower()
        prev = out.get(key)
        if prev is None:
            out[key] = (e.name, e.qty)
        else:
            out[key] = (prev[0], prev[1] + e.qty)
    return out


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/diff",
    summary="Line-level diff between two decklists",
    description=(
        "Pure Python line-level diff on parsed decklists — no SQL, no "
        "resolve. Returns per-name added/removed/qty-changed lists plus "
        "unchanged count and per-side totals. Complements /decks/compare "
        "(analyze-shaped stats per side) for iterative deckbuilding sessions."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def diff_decklists(body: DiffRequest) -> Any:
    """Implement §6.18 — pure-Python decklist diff."""
    for label, side in (("a", body.a), ("b", body.b)):
        if not side or not side.strip():
            return _scryfall_error(
                422, "bad_request", f"decklist {label!r} is required"
            )
        if len(side) > DECKLIST_MAX_LEN:
            return _scryfall_error(
                422, "bad_request", f"decklist {label!r} too long"
            )

    a_map = _decklist_qty_map(body.a)
    b_map = _decklist_qty_map(body.b)
    # D19.40 extracted the classification below into `decklist.diff` so
    # `GET /decks/{id}/branches` reuses it against a deck_card_rows()
    # fold instead of writing a second differ.
    return {"object": "deck_diff", **diff_qty_maps(a_map, b_map)}
