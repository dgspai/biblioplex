"""``POST /decks/export`` — D14.13, §6.14.

Round-trip formatter symmetric with `parse_decklist`. Zero DB touch.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from biblioplex.decklist.format import SUPPORTED_STYLES, format_decklist
from biblioplex.errors import scryfall_error as _scryfall_error

from ._common import _ERROR_RESPONSE, DECKLIST_MAX_LEN, _whole_list_entries


class ExportRequest(BaseModel):
    """Body for ``POST /decks/export`` — canonicalise a decklist string."""

    decklist: str = Field(
        ..., description="Raw plaintext decklist to canonicalise + emit."
    )


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/export",
    summary="Format a decklist in Arena / MTGO / plain-text style",
    description=(
        "Round-trip formatter symmetric with `parse_decklist`. Style "
        "`arena` emits `<qty> <name>` (printing hints not preserved "
        "through parse — see §6.14 note); `mtgo` and `txt` emit "
        "`<qty> <name>` per line. Canonicalises Arena tail annotations "
        "(set / collector / foil markers) via `parse_decklist`. Zero DB "
        "touch."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def export_decklist(
    body: ExportRequest,
    style: str = Query(
        "mtgo",
        description="Output style: `arena`, `mtgo`, or `txt`.",
    ),
) -> Any:
    """Implement §6.14 — decklist canonicalisation + style output."""
    if not body.decklist or not body.decklist.strip():
        return _scryfall_error(422, "bad_request", "decklist is required")
    if len(body.decklist) > DECKLIST_MAX_LEN:
        return _scryfall_error(422, "bad_request", "decklist too long")
    style_norm = style.strip().lower()
    if style_norm not in SUPPORTED_STYLES:
        return _scryfall_error(
            422,
            "bad_request",
            f"unsupported style {style!r}; expected one of "
            f"{sorted(SUPPORTED_STYLES)}",
        )
    entries = _whole_list_entries(body.decklist)
    output = format_decklist(entries, style=style_norm)
    return {
        "object": "deck_export",
        "style": style_norm,
        "decklist": output,
        "line_count": len(entries),
        "total_qty": sum(e.qty for e in entries),
    }
