"""``POST /decks/mulligan`` — D14.10.

Draws opening hands from the resolved deck and aggregates keep-rate
heuristics.
"""

from __future__ import annotations

import random
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from biblioplex.decklist.mulligan import sample_hands
from biblioplex.decklist.resolve import resolve_and_fetch_deck_rows
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _Engine, get_read_engine

from ._common import _ERROR_RESPONSE, DECKLIST_MAX_LEN, _is_land, _maindeck_entries


class MulliganRequest(BaseModel):
    """Body for ``POST /decks/mulligan``."""

    decklist: str = Field(..., description="Plaintext decklist, one entry per line.")
    hands: int = Field(
        default=500,
        ge=1,
        le=10000,
        description="Number of opening hands to sample (1–10000).",
    )
    hand_size: int = Field(
        default=7,
        ge=1,
        le=20,
        description="Cards per opening hand (1–20). Commander defaults to 7.",
    )
    seed: int | None = Field(
        default=None,
        description="Optional RNG seed for reproducible runs.",
    )


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/mulligan",
    summary="Sample opening hands and report keep-rate heuristics (D14.10)",
    description=(
        "Draws `hands` × `hand_size` opening hands from the resolved deck "
        "and aggregates stats: land min / median / max / mean, cheap "
        "cards (cmc≤3) mean, and `keep_rate` = fraction of hands with "
        "2 ≤ lands ≤ 5 (London-mulligan heuristic). No formal EV, no "
        "bottom-decking sim. Deterministic when `seed` is set."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def decklist_mulligan(
    body: MulliganRequest,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement D14.10."""
    decklist = body.decklist
    if not decklist or not decklist.strip():
        return _scryfall_error(422, "bad_request", "decklist is required")
    if len(decklist) > DECKLIST_MAX_LEN:
        return _scryfall_error(422, "bad_request", "decklist too long")

    entries = _maindeck_entries(decklist)
    names = [e.name for e in entries]

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        resolved_rows, unresolved = await resolve_and_fetch_deck_rows(conn, names)

    row_by_lower: dict[str, Any] = {
        r.input_name.lower(): r.row for r in resolved_rows
    }
    pool: list[tuple[str, int | None, bool]] = []
    for e in entries:
        row = row_by_lower.get(e.name.lower())
        if row is None:
            continue
        cmc_val = row.cmc
        try:
            cmc = int(float(cmc_val)) if cmc_val is not None else None
        except (TypeError, ValueError):
            cmc = None
        pool.extend([(row.name, cmc, _is_land(row))] * e.qty)

    rng = random.Random(body.seed)  # noqa: S311
    stats = sample_hands(
        pool, n_hands=body.hands, hand_size=body.hand_size, rng=rng
    )
    return {
        "object": "deck_mulligan",
        "hands": stats.hands,
        "hand_size": stats.hand_size,
        "seed": body.seed,
        "pool_size": len(pool),
        "stats": {
            "land_min": stats.land_min,
            "land_max": stats.land_max,
            "land_median": stats.land_median,
            "land_mean": round(stats.land_mean, 4),
            "cmc_le_3_mean": round(stats.cmc_le_3_mean, 4),
            "keep_rate": round(stats.keep_rate, 4),
        },
        "unresolved": unresolved,
    }
