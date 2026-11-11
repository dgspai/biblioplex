"""``POST /decks/compare`` — D15.5.

Runs the `/decks/analyze` pipeline against two decklists (concurrently,
one connection each) and returns the per-card diff plus label/curve/pip
deltas.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _Engine, get_read_engine

from ._common import _ERROR_RESPONSE, DECKLIST_MAX_LEN, _analyze_pipeline


class CompareRequest(BaseModel):
    """Body for ``POST /decks/compare`` — diff two decklists."""

    a: str = Field(..., description="First plaintext decklist (baseline).")
    b: str = Field(..., description="Second plaintext decklist (compared).")


def _cards_by_name_lower(analysis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map ``lowered-name → {id, name, qty, predicted_labels}`` for a side.

    Name-based comparison (not id-based) so unresolved names still
    surface in `added`/`removed`. Ties broken by first-seen order —
    `_tally` already dedups on id upstream.
    """
    return {c["name"].lower(): c for c in analysis["cards"]}


def _diff_dict_int(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    keys = set(a) | set(b)
    return {k: int(b.get(k, 0)) - int(a.get(k, 0)) for k in keys}


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/compare",
    summary="Compare two decklists — added, removed, changed, and slot deltas",
    description=(
        "Runs `/decks/analyze` against both decklists and returns the "
        "per-card diff (`added` / `removed` / `changed_qty`) plus label "
        "counts, curve, and pips deltas (`b - a`). Same 8000-char cap "
        "per side as `/decks/analyze`."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def compare_decklists(
    body: CompareRequest,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    for label, side in (("a", body.a), ("b", body.b)):
        if not side or not side.strip():
            return _scryfall_error(422, "bad_request", f"decklist {label!r} is required")
        if len(side) > DECKLIST_MAX_LEN:
            return _scryfall_error(422, "bad_request", f"decklist {label!r} too long")

    # D15.11: dispatch the two pipelines on separate connections via
    # `asyncio.gather` — they share zero SQL state, so a serial await
    # was leaving the second pipeline's wall-clock on the table.
    async with (
        engine.connect() as conn_a,  # type: ignore[attr-defined]
        engine.connect() as conn_b,  # type: ignore[attr-defined]
    ):
        a, b = await asyncio.gather(
            _analyze_pipeline(body.a, conn_a),
            _analyze_pipeline(body.b, conn_b),
        )

    a_by_name = _cards_by_name_lower(a)
    b_by_name = _cards_by_name_lower(b)

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed_qty: list[dict[str, Any]] = []

    for name_lower, card_b in b_by_name.items():
        if name_lower not in a_by_name:
            added.append(card_b)
        elif a_by_name[name_lower]["qty"] != card_b["qty"]:
            card_a = a_by_name[name_lower]
            changed_qty.append({
                "id": card_b["id"],
                "name": card_b["name"],
                "qty_a": card_a["qty"],
                "qty_b": card_b["qty"],
                "delta": card_b["qty"] - card_a["qty"],
            })
    for name_lower, card_a in a_by_name.items():
        if name_lower not in b_by_name:
            removed.append(card_a)

    return {
        "object": "deck_compare",
        "a": a,
        "b": b,
        "diff": {
            "added": added,
            "removed": removed,
            "changed_qty": changed_qty,
            "counts_delta": _diff_dict_int(a["counts"], b["counts"]),
            "curve_delta": _diff_dict_int(a["curve"], b["curve"]),
            "pips_delta": _diff_dict_int(a["pips"], b["pips"]),
        },
    }
