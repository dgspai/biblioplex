"""``POST /decks/goldfish`` — D14.17, §6.20.

Deterministic goldfish (solitaire) simulation over N turns. Extension
of `/decks/mulligan`.
"""

from __future__ import annotations

import random
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from biblioplex.decklist.goldfish import run_goldfish
from biblioplex.decklist.resolve import resolve_and_fetch_deck_rows
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _Engine, get_read_engine

from ._common import _ERROR_RESPONSE, DECKLIST_MAX_LEN, _is_land, _maindeck_entries


class GoldfishRequest(BaseModel):
    """Body for ``POST /decks/goldfish``."""

    decklist: str = Field(
        ..., description="Plaintext decklist, one entry per line."
    )
    hands: int = Field(
        default=500,
        ge=10,
        le=5000,
        description="Number of goldfish sims to run (10-5000).",
    )
    hand_size: int = Field(
        default=7, ge=1, le=20, description="Opening hand size."
    )
    seed: int | None = Field(
        default=None, description="Optional RNG seed for reproducible runs."
    )


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/goldfish",
    summary="Deterministic goldfish (solitaire) sim over N turns (D14.17)",
    description=(
        "Extension of `/decks/mulligan`: for each seeded sim, mulligan "
        "to keep, then walk `turns` turns of naive play (draw, one land "
        "drop, cast biggest-first spells that fit mana). Reports "
        "per-turn averages for mana, spells cast, lands in play, plus a "
        "text sample of turn lines (deterministic under seed) for "
        "spot-checking. Non-goals: interaction, targeting, colored "
        "mana. Pairs with `/decks/bracket` + curve — 'does this deck "
        "actually curve out?'"
    ),
    responses={422: _ERROR_RESPONSE},
)
async def goldfish_decklist(
    body: GoldfishRequest,
    turns: int = Query(
        default=6,
        ge=1,
        le=12,
        description="Number of turns per sim (1-12).",
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement §6.20."""
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
    stats = run_goldfish(
        pool,
        sims=body.hands,
        turns=turns,
        hand_size=body.hand_size,
        rng=rng,
    )
    return {
        "object": "deck_goldfish",
        "sims": stats.sims,
        "turns": stats.turns,
        "hand_size": stats.hand_size,
        "seed": body.seed,
        "pool_size": len(pool),
        "mulligans_taken": stats.mulligans_taken,
        "hands_kept": stats.hands_kept,
        "avg_mana_by_turn": list(stats.avg_mana_by_turn),
        "avg_spells_cast_by_turn": list(stats.avg_spells_cast_by_turn),
        "avg_lands_in_play_by_turn": list(stats.avg_lands_in_play_by_turn),
        "sample_lines": list(stats.sample_lines),
        "unresolved": unresolved,
    }
