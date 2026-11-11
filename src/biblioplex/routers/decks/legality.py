"""``POST /decks/legality`` — D14.7.

Checks a decklist against a format's legality + build rules.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from biblioplex.decklist.legality import LegalityResult, check_legality
from biblioplex.decklist.resolve import resolve_and_fetch_deck_rows
from biblioplex.decklist.sections import DeckSections, parse_sections
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _Engine, get_read_engine

from ._common import (
    _ERROR_RESPONSE,
    DECKLIST_MAX_LEN,
    _legality_body_entries,
    _resolve_commander_ci,
)


class LegalityRequest(BaseModel):
    """Body for ``POST /decks/legality``.

    ``format`` matches the keys under Scryfall ``legalities`` (`commander`,
    `modern`, `legacy`, `pioneer`, `standard`, `vintage`, `pauper`,
    `brawl`, `oathbreaker`, `historic`, `alchemy`, `timeless`,
    `explorer`, `predh`, `duel`, `oldschool`, `premodern`, `future`).
    Unknown slugs are treated as "not legal for any card" — cheap +
    honest.
    """

    decklist: str = Field(..., description="Plaintext decklist, one entry per line.")
    format: str = Field(
        default="commander",
        description="Format slug matching Scryfall's `legalities` map.",
    )
    commander: str | None = Field(
        default=None,
        description=(
            "Optional commander name — resolved separately for CI check. "
            "When supplied, the deck body should be 99 cards; the "
            "commander is not counted in `qty_by_lower`."
        ),
    )


def _parsed_commander(sections: DeckSections) -> str | None:
    """First name under a `Commander` heading, or None.

    Lets `/decks/legality` and `/decks/bracket` infer the commander a
    stored export already names. An explicitly-passed `commander` always
    wins — explicit beats inferred.
    """
    return sections.commander[0].name if sections.commander else None


def _serialize_legality(result: LegalityResult) -> dict[str, Any]:
    return {
        "object": "deck_legality",
        "legal": result.legal,
        "format": result.format,
        "deck_size": result.deck_size,
        "expected_deck_size": result.expected_deck_size,
        "commander_color_identity": result.commander_color_identity,
        "violations": [
            {"card": v.card, "reason": v.reason, "detail": v.detail}
            for v in result.violations
        ],
    }


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/legality",
    summary="Check a decklist against a format's legality + build rules (D14.7)",
    description=(
        "Parses a decklist, resolves names against `cards`, and checks: "
        "(a) every card's `legalities[format]` is `'legal'` (or `'restricted'` "
        "for constructed vintage), (b) copy counts respect the format's cap "
        "(commander singleton; constructed max-4; basics + curated 'any "
        "number' cards exempt), (c) commander color-identity subsumption "
        "when a commander name is supplied, (d) deck size (commander: 100 "
        "with commander / 99 body when supplied separately; constructed: "
        ">= 60). Returns `{object: 'deck_legality', legal: bool, format, "
        "deck_size, expected_deck_size, commander_color_identity, "
        "violations: [{card, reason, detail}], unresolved: []}`. Violation "
        "`reason` slugs are stable: `not_legal`, `over_max_copies`, "
        "`color_identity`, `deck_size`, `unresolved`."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def decklist_legality(
    body: LegalityRequest,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement D14.7."""
    decklist = body.decklist
    if not decklist or not decklist.strip():
        return _scryfall_error(422, "bad_request", "decklist is required")
    if len(decklist) > DECKLIST_MAX_LEN:
        return _scryfall_error(422, "bad_request", "decklist too long")

    sections = parse_sections(decklist)
    entries = _legality_body_entries(sections)
    names = [e.name for e in entries]

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        # Explicit beats inferred: an explicitly-passed `commander` wins
        # over the name a `Commander` heading already carries, so a caller
        # can override what a stored export claims.
        commander_name = body.commander or _parsed_commander(sections)
        commander_ci: list[str] | None = None
        if commander_name:
            result_ci = await _resolve_commander_ci(conn, commander_name)
            if isinstance(result_ci, JSONResponse):
                return result_ci
            commander_ci = result_ci

        resolved_rows, unresolved = await resolve_and_fetch_deck_rows(
            conn, names
        )

    # Aggregate: sum qty per lowered name; last row wins for the display-name
    # display, but summed qty is honest.
    qty_by_lower: dict[str, int] = {}
    rows_by_lower: dict[str, Any] = {}
    for entry in entries:
        key = entry.name.lower()
        qty_by_lower[key] = qty_by_lower.get(key, 0) + entry.qty
    for r in resolved_rows:
        rows_by_lower[r.input_name.lower()] = r.row

    verdict = check_legality(
        qty_by_lower=qty_by_lower,
        rows_by_lower=rows_by_lower,
        fmt=body.format,
        commander_ci=commander_ci,
        unresolved=unresolved,
    )
    payload = _serialize_legality(verdict)
    payload["unresolved"] = unresolved
    return payload
