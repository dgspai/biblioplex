"""``POST /decks/bracket`` — D14.4.

Rough heuristic estimate of the WOTC Commander bracket 1-5 for a
decklist.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from biblioplex.decklist.bracket import SIGNAL_TAG_SLUGS, estimate_bracket, to_payload
from biblioplex.decklist.resolve import resolve_and_fetch_deck_rows
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import _Engine, get_read_engine

from ._common import _ERROR_RESPONSE, DECKLIST_MAX_LEN, _maindeck_entries


class BracketRequest(BaseModel):
    """Body for ``POST /decks/bracket``."""

    decklist: str = Field(..., description="Plaintext decklist, one entry per line.")


_COMBOS_COUNT_SQL = (
    "SELECT count(*) AS n "
    "FROM combos "
    "WHERE oracle_ids <@ CAST(:deck_oids AS uuid[])"
)


# D15.25 — one aggregate query counts hits per Scryfall Tagger slug for
# every bracket signal that has an ``otag:`` equivalent (fast_mana / mld
# / tutors / extra_turns). GAME_CHANGERS stays name-based via
# ``estimate_bracket`` since it has no Tagger equivalent.
#
# COUNT FILTER over one WHERE-narrowed row set is cheaper than four
# separate SELECTs; the `IN (:slugs)` predicate uses the
# `oracle_tags_tag_idx` index. Bug.27 rule: `CAST(:X AS TYPE)` not
# `:X::TYPE` because SQLAlchemy `text()` treats `::` as an escaped
# colon and drops the bindparam.
_BRACKET_TAG_COUNTS_SQL = (
    "SELECT "
    "  COUNT(*) FILTER (WHERE tag = :slug_fast_mana) AS fast_mana, "
    "  COUNT(*) FILTER (WHERE tag = :slug_mld) AS mld, "
    "  COUNT(*) FILTER (WHERE tag = :slug_tutors) AS tutors, "
    "  COUNT(*) FILTER (WHERE tag = :slug_extra_turns) AS extra_turns "
    "FROM oracle_tags "
    "WHERE oracle_id = ANY(CAST(:deck_oids AS uuid[])) "
    "  AND tag IN ("
    "    :slug_fast_mana, :slug_mld, :slug_tutors, :slug_extra_turns"
    "  )"
)


router = APIRouter(prefix="/decks", tags=["decks"])


@router.post(
    "/bracket",
    summary="Estimate the WOTC Commander bracket 1–5 for a decklist (D14.4)",
    description=(
        "Rough heuristic per WOTC's Sept-2024 bracket system. Signals: "
        "game-changer count, fast mana, MLD, tutors, extra-turn spells, "
        "and combos ingested from Commander Spellbook. The rubric is "
        "qualitative — treat as a rough estimate. See "
        "`docs/deck-bracket.md` for the ladder + signal definitions."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def decklist_bracket(
    body: BracketRequest,
    request: Request,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Implement D14.4."""
    decklist = body.decklist
    if not decklist or not decklist.strip():
        return _scryfall_error(422, "bad_request", "decklist is required")
    if len(decklist) > DECKLIST_MAX_LEN:
        return _scryfall_error(422, "bad_request", "decklist too long")

    entries = _maindeck_entries(decklist)
    names = [e.name for e in entries]

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        resolved_rows, unresolved = await resolve_and_fetch_deck_rows(conn, names)
        combo_count = 0
        tag_signal_counts: dict[str, int] = {}
        if resolved_rows:
            deck_oids = sorted({str(r.row.oracle_id) for r in resolved_rows})
            row = (
                await conn.execute(
                    text(_COMBOS_COUNT_SQL),
                    {"deck_oids": deck_oids},
                )
            ).one_or_none()
            combo_count = int(row.n) if row is not None else 0

            # D15.25: fire ONE aggregate over oracle_tags for the four
            # tag-based bracket signals. Slugs come from the shared
            # SIGNAL_TAG_SLUGS map so a Tagger slug rename lands in one
            # place. When oracle_tags is empty (fresh deployment, tag
            # ingest not yet run), the counts fall through as 0.
            tag_params = {
                "deck_oids": deck_oids,
                "slug_fast_mana": SIGNAL_TAG_SLUGS["fast_mana"],
                "slug_mld": SIGNAL_TAG_SLUGS["mld"],
                "slug_tutors": SIGNAL_TAG_SLUGS["tutors"],
                "slug_extra_turns": SIGNAL_TAG_SLUGS["extra_turns"],
            }
            tag_row = (
                await conn.execute(text(_BRACKET_TAG_COUNTS_SQL), tag_params)
            ).one_or_none()
            if tag_row is not None:
                tag_signal_counts = {
                    "fast_mana": int(tag_row.fast_mana),
                    "mld": int(tag_row.mld),
                    "tutors": int(tag_row.tutors),
                    "extra_turns": int(tag_row.extra_turns),
                }

    deck_names_lower = [r.row.name.lower() for r in resolved_rows]
    # Bug.8: preserve the original casing in `game_changer_names` so
    # the wire response reads `"Cyclonic Rift"`, not `"cyclonic rift"`.
    original_by_lower = {r.row.name.lower(): r.row.name for r in resolved_rows}
    # D15.18: flag combos as deployment-disabled when COMBOS_SOURCE_URL is
    # empty so a cEDH deck on a default deployment isn't silently capped
    # at bracket 3 with no explanation.
    combos_enabled = bool(request.app.state.settings.combos_source_url)
    estimate = estimate_bracket(
        deck_names_lower,
        combos=combo_count,
        original_by_lower=original_by_lower,
        combos_enabled=combos_enabled,
        tag_signal_counts=tag_signal_counts,
    )
    payload = to_payload(estimate)
    payload["unresolved"] = unresolved
    return payload
