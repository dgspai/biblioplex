"""``POST /decks/price`` — D14.11, §6.16."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from biblioplex.decklist.resolve import resolve_and_fetch_deck_rows
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _Engine, get_read_engine

from ._common import _ERROR_RESPONSE, DECKLIST_MAX_LEN, _whole_list_entries


class PriceRequest(BaseModel):
    """Body for ``POST /decks/price``."""

    decklist: str = Field(
        ..., description="Plaintext decklist, one entry per line."
    )


PRICE_TOP_N = 10


def _price_cmc_bucket(cmc: Any) -> str:
    """0-1, 2-3, 4+ bucketing (agents want a curve-shaped price breakdown)."""
    try:
        v = int(float(cmc)) if cmc is not None else 0
    except (TypeError, ValueError):
        v = 0
    if v <= 1:
        return "0-1"
    if v <= 3:
        return "2-3"
    return "4+"


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/price",
    summary="Sum a decklist's cheapest-printing USD prices with breakdowns",
    description=(
        "Reuses `parse_decklist` + `resolve_and_fetch_deck_rows`. Sums "
        "`cards.price_usd` × qty per resolved row; NULL-priced rows (no "
        "Scryfall market data) surface in `missing_price` rather than "
        "being silently zeroed. Breakdown by slot (dominant "
        "predicted_label) and by CMC bucket (0-1 / 2-3 / 4+); top-N most "
        "expensive lines returned for shopping. Zero new SQL — the "
        "resolve fold carries `price_usd`."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def price_decklist(
    body: PriceRequest,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement §6.16."""
    decklist = body.decklist
    if not decklist or not decklist.strip():
        return _scryfall_error(422, "bad_request", "decklist is required")
    if len(decklist) > DECKLIST_MAX_LEN:
        return _scryfall_error(422, "bad_request", "decklist too long")

    entries = _whole_list_entries(decklist)
    name_to_qty: dict[str, int] = {}
    for e in entries:
        name_to_qty[e.name.lower()] = name_to_qty.get(e.name.lower(), 0) + e.qty
    names = [e.name for e in entries]

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        resolved_rows, unresolved = await resolve_and_fetch_deck_rows(conn, names)

    total_usd = 0.0
    by_slot: dict[str, float] = {}
    by_cmc: dict[str, float] = {"0-1": 0.0, "2-3": 0.0, "4+": 0.0}
    top_lines: list[dict[str, Any]] = []
    missing_price: list[str] = []

    for resolved in resolved_rows:
        row = resolved.row
        qty = name_to_qty.get(resolved.input_name.lower(), 1)
        unit = getattr(row, "price_usd", None)
        if unit is None:
            missing_price.append(row.name)
            continue
        try:
            unit_f = float(unit)
        except (TypeError, ValueError):
            missing_price.append(row.name)
            continue
        line = unit_f * qty
        total_usd += line

        # Slot bucket: first predicted_label if any, else "other".
        labels = getattr(row, "predicted_labels", None) or []
        slot = labels[0] if labels else "other"
        by_slot[slot] = by_slot.get(slot, 0.0) + line

        by_cmc[_price_cmc_bucket(getattr(row, "cmc", None))] += line

        top_lines.append({
            "name": row.name,
            "qty": qty,
            "unit_usd": round(unit_f, 2),
            "line_usd": round(line, 2),
        })

    top_lines.sort(key=lambda r: r["line_usd"], reverse=True)

    return {
        "object": "deck_price",
        "total_usd": round(total_usd, 2),
        "by_slot": {k: round(v, 2) for k, v in by_slot.items()},
        "by_cmc": {k: round(v, 2) for k, v in by_cmc.items()},
        "top": top_lines[:PRICE_TOP_N],
        "missing_price": missing_price,
        "unresolved": unresolved,
    }
