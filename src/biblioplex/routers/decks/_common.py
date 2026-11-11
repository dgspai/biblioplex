"""Shared surface for the `routers.decks` subpackage (D16.37).

Pure move from `routers/decks.py`. Holds the module-level helpers and
constants used by two or more endpoint modules — `DECKLIST_MAX_LEN`,
the shared 422 response model, the commander color-identity resolver,
the `/analyze` + `/suggest` tally machinery, the D19.9 section-routing
helpers (`_maindeck_entries` / `_whole_list_entries` /
`_legality_body_entries` — must keep ONE definition each), and the
`/analyze` pipeline itself (reused by `/compare`).

Anything used by exactly one endpoint module stays local to that module.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from fastapi.responses import JSONResponse
from sqlalchemy import text

from biblioplex.classify.rules import LABELS, TARGETS
from biblioplex.decklist.parser import DeckEntry
from biblioplex.decklist.resolve import resolve_and_fetch_deck_rows
from biblioplex.decklist.sections import DeckSections, parse_sections, subtract_entries
from biblioplex.errors import ErrorEnvelope
from biblioplex.errors import scryfall_error as _scryfall_error

# 8000 chars (~320 lines) rejected real Moxfield lists: the sync pushes
# whatever a deck holds, and bulk "everything in this colour" lists run to
# a couple of thousand lines. 64K is ~2500 lines — past any list Moxfield
# hands back, still a bound on the O(n) resolve every analysis route does.
DECKLIST_MAX_LEN = 64000

_ERROR_RESPONSE: dict[str, Any] = {
    "model": ErrorEnvelope,
    "description": "Scryfall-shaped error envelope.",
}

_COMMANDER_CI_SQL = (
    "SELECT color_identity FROM cards WHERE LOWER(name) = LOWER(:name)"
)


async def _resolve_commander_ci(
    conn: Any, name: str
) -> list[str] | JSONResponse:
    """Look up ``commander_ci`` by name; return the CI list or a 422 envelope.

    Callers ``isinstance``-check the result: ``JSONResponse`` is the
    ready-to-return error path (unknown commander); a ``list[str]`` is
    the resolved (possibly empty) color identity.

    D10.13: factored out of the three identical resolve blocks in
    ``/decks/suggest`` / ``/upgrade`` / ``/manabase``.
    """
    row = (
        await conn.execute(text(_COMMANDER_CI_SQL), {"name": name})
    ).one_or_none()
    if row is None:
        return _scryfall_error(
            422,
            "commander_not_found",
            f"No card found with name '{name}'",
        )
    return list(row.color_identity or [])

# Curve buckets: 0, 1, 2, 3, 4, 5, 6, 7+. Cap at 7 so `Emrakul`s stack in
# one bin — deckbuilders read "how many 7-drops or more", not "how many
# 15-drops". Lands sit out of the curve entirely.
_CURVE_BUCKETS = ("0", "1", "2", "3", "4", "5", "6", "7+")
_COLOR_KEYS = ("W", "U", "B", "R", "G", "C")
_MANA_SYMBOL_RE = re.compile(r"\{([^}]+)\}")
_LAND_TYPE_RE = re.compile(r"\bland\b", re.I)

# Layouts where the top-level ``type_line`` joins the FRONT and BACK
# faces with " // " and the back can independently be a land. For these
# we inspect the front face's ``type_line`` instead — a MDFC land-back
# card (Bala Ged Recovery, Emeria's Call) plays as a spell, not a land.
_MDFC_LAYOUTS = frozenset({"transform", "modal_dfc"})


def _is_land(row: Any) -> bool:
    """Land detection for the /decks/analyze tally (D13.2).

    See module docstring for the layout-aware rule. Robust to missing
    or malformed ``card_faces`` — falls through to the top-level
    ``type_line`` in the degenerate cases.
    """
    layout = getattr(row, "layout", None) or ""
    if layout in _MDFC_LAYOUTS:
        faces = getattr(row, "card_faces", None)
        if isinstance(faces, list) and faces:
            front = faces[0]
            if isinstance(front, dict):
                front_type = front.get("type_line") or ""
                return bool(_LAND_TYPE_RE.search(front_type))
    return bool(_LAND_TYPE_RE.search(getattr(row, "type_line", None) or ""))


def _cmc_bucket(cmc: Any) -> str:
    try:
        n = int(float(cmc or 0))
    except (TypeError, ValueError):
        return "0"
    if n < 0:
        return "0"
    return "7+" if n >= 7 else str(n)


# Guide §6 bell over the 62 nonland slots (edh-guide.md). MV6+ is one
# bucket (curve "6" + "7+" fold together); MV0 has no target — the guide
# doesn't slot 0-drops, so it stays out of the delta the way land does.
_CURVE_TARGETS: dict[str, int] = {"1": 9, "2": 18, "3": 15, "4": 10, "5": 5, "6+": 5}


def _curve_deltas(curve: dict[str, int]) -> dict[str, int]:
    """actual - target per §6 bell key; folds curve "6"+"7+" into "6+"."""
    actual = {k: curve.get(k, 0) for k in ("1", "2", "3", "4", "5")}
    actual["6+"] = curve.get("6", 0) + curve.get("7+", 0)
    return {k: actual[k] - t for k, t in _CURVE_TARGETS.items()}


def _count_pips(mana_cost: str | None) -> dict[str, int]:
    """Count colored/colorless pip letters across a mana cost.

    Each symbol contributes one per color letter it contains — hybrids
    like ``{U/B}`` add to both, phyrexian ``{W/P}`` adds W, generic
    ``{2}`` / ``{X}`` add nothing. `{C}` counts as colorless.
    """
    out = {c: 0 for c in _COLOR_KEYS}
    if not mana_cost:
        return out
    for sym in _MANA_SYMBOL_RE.findall(mana_cost):
        for c in _COLOR_KEYS:
            if c in sym:
                out[c] += 1
    return out


class _Tally:
    """Result of walking an entries × resolved-rows tally.

    Shared between /analyze (which surfaces every field) and /suggest
    (which only needs `deltas`, `seen_ids`, `commander_ci` and the
    unresolved list). Consolidating the loop here keeps the per-slot
    query in /suggest free of the tally bookkeeping.
    """

    __slots__ = (
        "counts",
        "curve",
        "pips",
        "cards_out",
        "seen_ids",
        "unresolved",
        "deltas",
    )

    def __init__(
        self,
        *,
        counts: dict[str, int],
        curve: dict[str, int],
        pips: dict[str, int],
        cards_out: list[dict[str, Any]],
        seen_ids: set[UUID],
        unresolved: list[str],
    ) -> None:
        self.counts = counts
        self.curve = curve
        self.pips = pips
        self.cards_out = cards_out
        self.seen_ids = seen_ids
        self.unresolved = unresolved
        self.deltas = {lab: counts[lab] - target for lab, target in TARGETS.items()}


def _tally(
    entries: list[Any],
    resolved_rows: list[Any],
    unresolved_from_fetch: list[str],
) -> _Tally:
    """Shared tally loop used by /analyze and /suggest.

    Returns a :class:`_Tally` bundling counts, curve, pips, per-card
    output list, the set of resolved ids (for /suggest to exclude
    already-in-deck candidates), and the aggregated unresolved list.
    """
    row_by_lower: dict[str, Any] = {r.input_name.lower(): r.row for r in resolved_rows}
    id_by_lower: dict[str, UUID] = {
        r.input_name.lower(): r.row.id for r in resolved_rows
    }

    counts: dict[str, int] = {lab: 0 for lab in LABELS}
    counts["land"] = 0
    curve: dict[str, int] = {b: 0 for b in _CURVE_BUCKETS}
    pips: dict[str, int] = {c: 0 for c in _COLOR_KEYS}
    cards_out: list[dict[str, Any]] = []
    seen_ids: set[UUID] = set()
    unresolved: list[str] = list(unresolved_from_fetch)

    qty_by_id: dict[UUID, int] = {}
    first_name_by_id: dict[UUID, str] = {}
    for e in entries:
        rid = id_by_lower.get(e.name.lower())
        if rid is None:
            continue
        qty_by_id[rid] = qty_by_id.get(rid, 0) + e.qty
        first_name_by_id.setdefault(rid, e.name)

    for e in entries:
        rid = id_by_lower.get(e.name.lower())
        if rid is None or rid in seen_ids:
            continue
        seen_ids.add(rid)
        row = row_by_lower.get(e.name.lower())
        if row is None:
            unresolved.append(first_name_by_id.get(rid, e.name))
            continue
        qty = qty_by_id[rid]
        labels = list(row.predicted_labels or [])
        if _is_land(row):
            counts["land"] += qty
        else:
            for lab in labels:
                if lab in counts:
                    counts[lab] += qty
            curve[_cmc_bucket(row.cmc)] += qty
            for c, n in _count_pips(row.mana_cost).items():
                pips[c] += n * qty
        cards_out.append(
            {
                "id": str(rid),
                "name": row.name,
                "qty": qty,
                "predicted_labels": labels,
                # So a client can reproduce the tally's land-exclusion: a land
                # tagged e.g. "ramp" is counted as land, not ramp (above), and
                # a slot drill-down must exclude it to match the shown count.
                "is_land": _is_land(row),
            }
        )

    return _Tally(
        counts=counts,
        curve=curve,
        pips=pips,
        cards_out=cards_out,
        seen_ids=seen_ids,
        unresolved=unresolved,
    )


async def _compute_deck_inventory_coverage(
    conn: Any,
    entries: list[Any],
    resolved_rows: list[Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compute inventory ownership coverage and missing cards list for a deck (D18.5)."""
    if not resolved_rows:
        return (
            {
                "total_cards_in_deck": 0,
                "owned_cards_in_deck": 0,
                "total_distinct_cards": 0,
                "owned_distinct_cards": 0,
                "total_missing_count": 0,
                "coverage_pct": 100.0,
                "missing_on_wishlist": 0,
            },
            [],
        )

    name_to_qty: dict[str, int] = {}
    for e in entries:
        k = e.name.lower()
        name_to_qty[k] = name_to_qty.get(k, 0) + e.qty

    oracle_info: dict[str, dict[str, Any]] = {}
    for r in resolved_rows:
        row = r.row
        raw_oid = getattr(row, "oracle_id", None)
        if raw_oid is None:
            continue
        oid = str(raw_oid)
        req_qty = name_to_qty.get(r.input_name.lower(), 1)
        if oid in oracle_info:
            oracle_info[oid]["qty_required"] += req_qty
        else:
            oracle_info[oid] = {
                "id": str(row.id),
                "oracle_id": oid,
                "name": row.name,
                "qty_required": req_qty,
            }

    oracle_ids = list(oracle_info.keys())
    if not oracle_ids:
        return (
            {
                "total_cards_in_deck": 0,
                "owned_cards_in_deck": 0,
                "total_distinct_cards": 0,
                "owned_distinct_cards": 0,
                "total_missing_count": 0,
                "coverage_pct": 100.0,
                "missing_on_wishlist": 0,
            },
            [],
        )

    # SUM + GROUP BY, not a bare SELECT: `inventory` holds one row per
    # printing, so a card owned across two editions is two rows and a
    # row-per-oracle dict would silently keep only the last one.
    inv_rows = (
        await conn.execute(
            text(
                "SELECT oracle_id, SUM(quantity) AS owned FROM inventory "
                "WHERE oracle_id = ANY(CAST(:oracle_ids AS uuid[])) "
                "GROUP BY oracle_id"
            ),
            {"oracle_ids": oracle_ids},
        )
    ).all()

    oracle_to_owned: dict[str, int] = {
        str(row.oracle_id): int(row.owned) for row in inv_rows
    }

    # D18.10 — which of the deck's cards are already on the wishlist. Answers
    # "of what I still need, what have I already flagged to buy?" without a
    # second round-trip per missing card.
    wl_rows = (
        await conn.execute(
            text(
                "SELECT DISTINCT oracle_id FROM wishlist "
                "WHERE oracle_id = ANY(CAST(:oracle_ids AS uuid[])) "
                "AND quantity > 0"
            ),
            {"oracle_ids": oracle_ids},
        )
    ).all()
    wanted_oracles: set[str] = {str(row.oracle_id) for row in wl_rows}

    total_cards_in_deck = 0
    owned_cards_in_deck = 0
    total_distinct_cards = len(oracle_info)
    owned_distinct_cards = 0
    missing_cards: list[dict[str, Any]] = []

    for oid, info in oracle_info.items():
        req = info["qty_required"]
        available = oracle_to_owned.get(oid, 0)
        owned = min(req, available)
        missing = max(0, req - available)

        total_cards_in_deck += req
        owned_cards_in_deck += owned
        if available > 0:
            owned_distinct_cards += 1

        if missing > 0:
            missing_cards.append({
                "id": info["id"],
                "oracle_id": info["oracle_id"],
                "name": info["name"],
                "qty_required": req,
                "qty_owned": owned,
                "qty_missing": missing,
                "on_wishlist": oid in wanted_oracles,
            })

    # Cards NOT yet on the wishlist lead — they are the ones needing action.
    missing_cards.sort(
        key=lambda r: (r["on_wishlist"], -r["qty_missing"], r["name"].lower())
    )
    total_missing_count = total_cards_in_deck - owned_cards_in_deck
    coverage_pct = (
        round((owned_cards_in_deck / total_cards_in_deck) * 100, 2)
        if total_cards_in_deck > 0
        else 100.0
    )

    summary = {
        "total_cards_in_deck": total_cards_in_deck,
        "owned_cards_in_deck": owned_cards_in_deck,
        "total_distinct_cards": total_distinct_cards,
        "owned_distinct_cards": owned_distinct_cards,
        "total_missing_count": total_missing_count,
        "coverage_pct": coverage_pct,
        "missing_on_wishlist": sum(1 for m in missing_cards if m["on_wishlist"]),
    }

    return summary, missing_cards


# ---- D19.9: which section(s) each endpoint reads ----
#
# Every endpoint below used to call `parse_decklist`, which treats each
# line as a card — so a stored export's `Commander` / `Mainboard` /
# `Sideboard` headings were sent to the resolver and came back as phantom
# `unresolved` entries. These three helpers replace that call, and the
# choice between them is per-endpoint policy, not a blanket rule. See the
# `Spec Drift / M19` entry for the reasoning behind each assignment.
#
# A heading-less decklist puts everything in `main`, so pasted lists
# behave exactly as they did before this change.


def _maindeck_entries(decklist: str) -> list[DeckEntry]:
    """Maindeck pool: headings dropped, commander and companion included.

    D19.10 folded commander and companion INTO `main`, so this is already
    the whole analysable deck — `DeckSections` is not a partition and
    adding those buckets back in double-counts them.
    """
    return parse_sections(decklist).main


def _whole_list_entries(decklist: str) -> list[DeckEntry]:
    """Main + sideboard, for endpoints that report on the whole list.

    Price (you buy the sideboard too), export (a format converter must
    not silently drop cards) and diff (a change report wants every
    change) each span both.
    """
    sections = parse_sections(decklist)
    return [*sections.main, *sections.sideboard]


def _legality_body_entries(sections: DeckSections) -> list[DeckEntry]:
    """`main` minus the commander — the body `check_legality` expects.

    `check_legality` documents `qty_by_lower` as the deck BODY with the
    commander excluded, and asserts 99 whenever a commander CI was
    supplied. Since D19.10 the commander sits inside `main`, so handing
    `main` over unmodified reports a bogus `deck_size` violation on a
    perfectly legal 100-card deck. Subtraction is by quantity rather than
    by name so a commander that is also a legal singleton elsewhere in
    the list keeps its other copy.
    """
    # Mechanism shared with `deck_cards` (D19.19); semantics deliberately
    # NOT shared — only the commander comes out here. A companion occupies
    # a sideboard slot, and folding it in would silently change a shipped
    # deck-size check.
    return subtract_entries(sections.main, sections.commander)


async def _analyze_pipeline(decklist: str, conn: Any) -> dict[str, Any]:
    """Run the /decks/analyze pipeline against an already-open conn.

    Extracted so /decks/compare can invoke it twice on the same
    connection without redundant SQL setup. Callers validate the
    decklist envelope (length, non-empty) upstream.
    """
    entries = _maindeck_entries(decklist)
    names = [e.name for e in entries]
    resolved_rows, unresolved_from_fetch = await resolve_and_fetch_deck_rows(
        conn, names
    )
    t = _tally(entries, resolved_rows, unresolved_from_fetch)
    inventory_summary, missing_cards = await _compute_deck_inventory_coverage(
        conn, entries, resolved_rows
    )
    return {
        "object": "deck_analysis",
        "counts": t.counts,
        "targets": dict(TARGETS),
        "deltas": t.deltas,
        "curve": t.curve,
        "curve_targets": dict(_CURVE_TARGETS),
        "curve_deltas": _curve_deltas(t.curve),
        "pips": t.pips,
        "cards": t.cards_out,
        "unresolved": t.unresolved,
        "inventory_summary": inventory_summary,
        "missing_cards": missing_cards,
    }
