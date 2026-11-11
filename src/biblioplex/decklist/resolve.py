"""Batch decklist name → card-id resolver for §6.7.

Two-pass strategy:

1. **Exact**. One batched ``WHERE LOWER(name) = ANY(:lowered)`` returns
   every input whose case-folded name matches a Scryfall card.
2. **Fuzzy**. Each miss gets one trigram fallback query against
   ``name_sort_key`` at the same ``0.4`` threshold used by §6.4's
   ``/cards/named?fuzzy=`` endpoint. ``ORDER BY similarity DESC LIMIT 1``
   so the DB picks the best match; the Python loop only iterates over the
   inputs that need fuzzing.

``resolve_and_fetch_deck_rows`` (D13.4) folds resolution + slot-data
fetch into ONE CTE query — /decks/analyze previously did two round-trips
(resolve_names → SELECT slot cols), which on a 100-card decklist paid
both the fuzzy per-miss tail AND a full extra round-trip. The fold uses
``unnest(CAST(:ords AS int[]), CAST(:names AS text[]), CAST(:lnames AS text[]))`` to build the
input table so the query stays a fixed shape regardless of decklist
size.

ponytail: two callables now — ``resolve_names`` (id-only) stays for
callers that only need id resolution; ``resolve_and_fetch_deck_rows``
is the fold. Both share the exact/fuzzy semantics.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text

__all__ = [
    "FUZZY_THRESHOLD",
    "ResolveResult",
    "ResolvedDeckRow",
    "resolve_and_fetch_deck_rows",
    "resolve_names",
]


FUZZY_THRESHOLD = 0.4


# Columns fetched by ``resolve_and_fetch_deck_rows`` — pinned so the
# consumer (routers/decks.py) can rely on attribute names. Add here
# rather than passing a col list from the caller to keep the SQL a
# single fixed string.
_DECK_ROW_COLUMNS = (
    "id",
    "oracle_id",
    "name",
    "type_line",
    "layout",
    "card_faces",
    "predicted_labels",
    "cmc",
    "mana_cost",
    "color_identity",
    "legalities",
    # D15.24: data-driven "any number of copies" exemption. Read by
    # `decklist/legality.py:_has_unlimited_copies` to replace the
    # curated frozenset that used to bit-rot with each new set.
    "has_unlimited_copies",
    # D14.11: cheapest-printing USD price for the oracle. NULL when
    # Scryfall carries no market data (Reserved List, promo-only) —
    # `/decks/price` collects these as `missing_price` rather than
    # silently zeroing the sum.
    "price_usd",
)


@dataclass(frozen=True)
class ResolvedDeckRow:
    """One resolved decklist entry with its slot-relevant columns."""

    input_name: str
    """Original decklist input (case preserved)."""

    row: Any
    """SA row proxy exposing every column in ``_DECK_ROW_COLUMNS``."""

    similarity: float | None = None
    """D15.14: trigram score when the row was matched via the fuzzy
    fallback, ``None`` for exact hits. Sourced from ``row.similarity``
    at construction so consumers can flag guessed matches in the UI."""


@dataclass(frozen=True)
class ResolveResult:
    """Outcome of batch name resolution."""

    matches: dict[str, UUID] = field(default_factory=dict)
    """Original input string → resolved card id. Order follows input order."""

    unresolved: list[str] = field(default_factory=list)
    """Original inputs that missed both exact and fuzzy passes."""


async def resolve_names(conn: Any, names: Iterable[str]) -> ResolveResult:
    """Resolve a batch of card-name strings to Scryfall UUIDs.

    Args:
        conn: An open SQLAlchemy async connection.
        names: Decklist name inputs in their original form. Duplicates
            are deduplicated by case-folded form; the first occurrence
            wins in the returned ordering.

    Returns:
        ``ResolveResult`` with ``matches`` (input → uuid) and
        ``unresolved`` (inputs that missed both passes), in input order.
    """
    ordered_inputs: list[str] = []
    seen_lowered: set[str] = set()
    for raw in names:
        key = raw.lower()
        if key in seen_lowered:
            continue
        seen_lowered.add(key)
        ordered_inputs.append(raw)
    if not ordered_inputs:
        return ResolveResult()

    # Bug.17: lower on the DB side for both operands via `LOWER(t.n)` so
    # locale-aware Postgres folding stays consistent with Python `.lower()`
    # lookups downstream. See docstring.
    # Bug.27: `CAST(:names AS text[])` not `:names::text[]` — `text()`
    # treats `::` as escaped colon and skips the bindparam.
    exact_rows = (
        await conn.execute(
            text(
                "SELECT c.id, t.n AS input_name FROM cards c "
                "JOIN unnest(CAST(:names AS text[])) AS t(n) "
                "ON LOWER(c.name) = LOWER(t.n)"
            ),
            {"names": ordered_inputs},
        )
    ).all()
    by_lower: dict[str, UUID] = {row.input_name.lower(): row.id for row in exact_rows}

    matches: dict[str, UUID] = {}
    misses: list[str] = []
    for raw in ordered_inputs:
        hit = by_lower.get(raw.lower())
        if hit is not None:
            matches[raw] = hit
        else:
            misses.append(raw)

    # Bug.19: batch the fuzzy fallback into ONE round-trip via a
    # `misses` unnest + LATERAL top-1 per miss. Prior shape fired one
    # trigram lookup per unresolved input in a Python loop — a 50-typo
    # decklist paid 51 round-trips (1 exact + 50 fuzzy).
    fuzzy_by_input: dict[str, UUID] = {}
    if misses:
        fuzzy_rows = (
            await conn.execute(
                text(
                    "SELECT m.n AS input_name, f.id "
                    "FROM unnest(CAST(:misses AS text[])) AS m(n) "
                    "CROSS JOIN LATERAL ( "
                    "  SELECT c.id "
                    "  FROM cards c "
                    "  WHERE similarity(c.name_sort_key, LOWER(m.n)) >= :threshold "
                    "  ORDER BY similarity(c.name_sort_key, LOWER(m.n)) DESC "
                    "  LIMIT 1 "
                    ") f"
                ),
                {"misses": misses, "threshold": FUZZY_THRESHOLD},
            )
        ).all()
        fuzzy_by_input = {row.input_name: row.id for row in fuzzy_rows}

    unresolved: list[str] = []
    ordered_matches: dict[str, UUID] = {}
    for raw in ordered_inputs:
        exact = matches.get(raw)
        if exact is not None:
            ordered_matches[raw] = exact
            continue
        fuzzy = fuzzy_by_input.get(raw)
        if fuzzy is not None:
            ordered_matches[raw] = fuzzy
        else:
            unresolved.append(raw)
    return ResolveResult(matches=ordered_matches, unresolved=unresolved)


# One CTE that walks the decklist inputs through the exact pass, then
# the fuzzy fallback for the remaining misses, then joins the winning
# id back to `cards` for the slot-relevant columns. See module doc.
#
# D15.14: `exact_hits` contributes `NULL::float AS similarity` and
# `fuzzy_hits` surfaces `f.s AS similarity` so the resolved CTE union
# threads a single-typed score column through to the final SELECT.
# Consumers read `row.similarity` to distinguish exact (None) from
# fuzzy (0.4-1.0) matches.
_RESOLVE_AND_FETCH_SQL = f"""
WITH input(ord, n, lname) AS (
    SELECT * FROM unnest(
        CAST(:ords AS int[]), CAST(:names AS text[]), CAST(:lnames AS text[])
    ) AS t(ord, n, lname)
),
exact_hits AS (
    SELECT i.ord, i.n AS input_name, c.id, NULL::float AS similarity
    FROM input i
    JOIN cards c ON LOWER(c.name) = LOWER(i.n)
),
misses AS (
    SELECT i.ord, i.n, i.lname
    FROM input i
    WHERE i.n NOT IN (SELECT input_name FROM exact_hits)
),
fuzzy_hits AS (
    SELECT m.ord, m.n AS input_name, f.id, f.s AS similarity
    FROM misses m
    CROSS JOIN LATERAL (
        SELECT c.id, similarity(c.name_sort_key, LOWER(m.n)) AS s
        FROM cards c
        WHERE similarity(c.name_sort_key, LOWER(m.n)) >= {FUZZY_THRESHOLD}
        ORDER BY s DESC
        LIMIT 1
    ) f
),
resolved AS (
    SELECT ord, input_name, id, similarity FROM exact_hits
    UNION ALL
    SELECT ord, input_name, id, similarity FROM fuzzy_hits
)
SELECT r.ord, r.input_name, r.similarity,
       c.id, c.oracle_id, c.name, c.type_line, c.layout, c.card_faces,
       c.predicted_labels, c.cmc, c.mana_cost, c.color_identity,
       c.legalities, c.has_unlimited_copies, c.price_usd
FROM resolved r
JOIN cards c ON c.id = r.id
ORDER BY r.ord
"""  # noqa: S608


async def resolve_and_fetch_deck_rows(
    conn: Any, names: Iterable[str]
) -> tuple[list[ResolvedDeckRow], list[str]]:
    """Resolve inputs AND fetch slot columns in ONE round-trip (D13.4).

    Uses the same exact + fuzzy semantics as ``resolve_names`` but
    executes them inside a single CTE plus a slot-column join. Cuts the
    per-request round-trip count from ``1 + (fuzzy_misses) + 1`` to
    exactly one.

    Args:
        conn: An open SQLAlchemy async connection.
        names: Decklist name inputs. Duplicates are deduplicated by
            case-folded form; the first occurrence's original casing is
            preserved in the returned row's ``input_name``.

    Returns:
        ``(resolved, unresolved)`` where ``resolved`` is a list of
        :class:`ResolvedDeckRow` in input order and ``unresolved`` is
        the list of inputs that missed both passes.
    """
    ordered_inputs: list[str] = []
    seen_lowered: set[str] = set()
    for raw in names:
        key = raw.lower()
        if key in seen_lowered:
            continue
        seen_lowered.add(key)
        ordered_inputs.append(raw)
    if not ordered_inputs:
        return [], []

    lowered = [n.lower() for n in ordered_inputs]
    ords = list(range(len(ordered_inputs)))
    rows = (
        await conn.execute(
            text(_RESOLVE_AND_FETCH_SQL),
            {"ords": ords, "names": ordered_inputs, "lnames": lowered},
        )
    ).all()

    resolved_by_input: dict[str, ResolvedDeckRow] = {}
    for row in rows:
        sim = getattr(row, "similarity", None)
        resolved_by_input[row.input_name] = ResolvedDeckRow(
            input_name=row.input_name,
            row=row,
            similarity=float(sim) if sim is not None else None,
        )

    resolved: list[ResolvedDeckRow] = []
    unresolved: list[str] = []
    for raw in ordered_inputs:
        hit = resolved_by_input.get(raw)
        if hit is None:
            unresolved.append(raw)
        else:
            resolved.append(hit)
    return resolved, unresolved
