"""``POST /decks/manabase`` — D14.6.

Pip-weighted basics + a CI-filtered nonbasic package drawn from a
curated priority list.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from biblioplex.decklist.manabase import BASIC_NAMES, NONBASIC_PRIORITY, distribute_basics
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _Engine, get_read_engine

from ._common import _ERROR_RESPONSE, _resolve_commander_ci


class ManabaseRequest(BaseModel):
    """Body for ``POST /decks/manabase``.

    ``pip_weights`` distributes the basics — WUBRG letter → integer
    weight (proportional). When absent, the endpoint defaults to an even
    weight across every color in the commander's color identity.
    """

    commander: str = Field(..., description="Commander card name.")
    pip_weights: dict[str, int] | None = Field(
        default=None,
        description=(
            "Optional per-color pip weights (W/U/B/R/G → int). When "
            "omitted, basics distribute evenly across the commander's "
            "color identity."
        ),
    )
    budget_usd: float | None = Field(
        default=None,
        gt=0,
        description="Optional per-card USD cap for the nonbasic picks.",
    )


MANABASE_LAND_COUNT_DEFAULT = 37
MANABASE_LAND_COUNT_MIN = 20
MANABASE_LAND_COUNT_MAX = 50

# Bug.22: valid pip_weights keys. Anything else → 422 bad_pip_weights.
_VALID_PIP_KEYS: frozenset[str] = frozenset({"W", "U", "B", "R", "G", "C"})


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/manabase",
    summary="Recommend a mana base for a commander (D14.6)",
    description=(
        "Pip-weighted basics + a CI-filtered nonbasic package drawn from "
        "a curated priority list. Not a full land database — the "
        "priority list is ~35 lands chosen for broad applicability. "
        "See `src/biblioplex/decklist/manabase.py:NONBASIC_PRIORITY` "
        "for the exact set."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def decklist_manabase(
    body: ManabaseRequest,
    land_count: int = Query(
        default=MANABASE_LAND_COUNT_DEFAULT,
        ge=MANABASE_LAND_COUNT_MIN,
        le=MANABASE_LAND_COUNT_MAX,
        description="Total land count to build toward (default 37).",
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement D14.6."""
    # Bug.22: reject unknown pip_weights keys up-front so a typo like
    # `{"X": 5}` surfaces as 422 instead of a silently-empty basics dict
    # (garbage keys → total_weight=0 → distribute_basics returns {} →
    # response reads "manabase incomplete").
    if body.pip_weights is not None:
        bad = sorted(k for k in body.pip_weights if k not in _VALID_PIP_KEYS)
        if bad:
            return _scryfall_error(
                422,
                "bad_pip_weights",
                f"unknown pip_weights key(s): {bad} — valid: {sorted(_VALID_PIP_KEYS)}",
            )
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result_ci = await _resolve_commander_ci(conn, body.commander)
        if isinstance(result_ci, JSONResponse):
            return result_ci
        commander_ci = result_ci

        # Nonbasic picks: filter the curated priority list to lands whose
        # CI is a subset of the commander's, cap by budget, preserve
        # priority-list order via `array_position`.
        params: dict[str, Any] = {
            "names": NONBASIC_PRIORITY,
            "ci": commander_ci,
        }
        where_budget = ""
        if body.budget_usd is not None:
            where_budget = "AND (price_usd IS NULL OR price_usd <= :budget) "
            params["budget"] = body.budget_usd
        nonbasic_target = min(land_count - _min_basics(commander_ci), len(NONBASIC_PRIORITY))
        params["nb_limit"] = max(0, nonbasic_target)
        # Bug.27: `CAST(:X AS text[])` not `:X::text[]` — `text()` treats
        # `::` as escaped colon and never binds the preceding param.
        result = await conn.execute(
            text(
                "SELECT name, raw, color_identity, price_usd "  # noqa: S608
                "FROM cards "
                "WHERE name = ANY(CAST(:names AS text[])) "
                "AND color_identity <@ CAST(:ci AS text[]) "
                f"{where_budget}"
                "ORDER BY array_position(CAST(:names AS text[]), name) "
                "LIMIT :nb_limit"
            ),
            params,
        )
        nonbasics = [dict(row.raw) for row in result.all()]

    # Basics distribution: pip weights (or default-even across CI) →
    # proportional split over remaining slots. D16.5 extends the
    # letter set with "C" (Wastes) so colorless commanders reach the
    # target land count instead of returning an empty basics dict.
    remaining = max(0, land_count - len(nonbasics))
    letters = ("W", "U", "B", "R", "G", "C")
    if body.pip_weights is not None:
        pip_weights = {c: int(body.pip_weights.get(c, 0)) for c in letters}
    elif commander_ci:
        pip_weights = {c: (1 if c in commander_ci else 0) for c in letters}
    else:
        # Colorless commander with no override → all Wastes.
        pip_weights = {c: 0 for c in letters}
        pip_weights["C"] = 1
    basic_counts = distribute_basics(pip_weights, remaining)
    basics = {BASIC_NAMES[c]: n for c, n in basic_counts.items() if n > 0}

    notes: list[str] = []
    total = len(nonbasics) + sum(basics.values())
    if total < land_count:
        notes.append(
            f"Manabase builds to {total} of {land_count} target lands — "
            "priority list is intentionally small; extend "
            "NONBASIC_PRIORITY to reach the full count on colorless / "
            "mono-color commanders."
        )
    return {
        "object": "deck_manabase",
        "commander_color_identity": commander_ci,
        "basics": basics,
        "nonbasics": nonbasics,
        "notes": notes,
    }


def _min_basics(commander_ci: list[str]) -> int:
    """Rough floor for basics reserved from the nonbasic cap.

    D10.15: colorless commanders return 0 — the priority list has zero
    zero-CI lands (every entry has a real color identity, so the ``<@``
    filter drops them all), reserving 5 slots for Wastes just shrank
    the nonbasic budget for no gain. Non-empty CIs keep the min-5 floor
    so a 5c commander's basics aren't crowded out by priority lands.
    """
    if not commander_ci:
        return 0
    return max(len(commander_ci), 5)
