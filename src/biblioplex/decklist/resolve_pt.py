"""Portuguese printed-name → ``oracle_id`` resolver.

LigaMagic (and other pt-BR sources) list decks by their Portuguese
printed name — "Banco de Nevoeiro", not "Fog Bank". The main
``cards`` table only carries English ``name`` (biblioplex ingests
Scryfall ``default_cards``), so those names miss ``decklist/resolve.py``
entirely. This resolver bridges the gap against the ``pt_names`` side
table populated by ``scripts/build_pt_names.py``.

Four passes, each one batched round-trip:

1. **PT exact** — ``LOWER(printed_name) = LOWER(:input)`` against
   ``pt_names``.
2. **PT fuzzy** — pg_trgm best-match against ``LOWER(printed_name)`` at
   the same ``0.4`` floor ``resolve.py`` uses, for inputs the exact pass
   missed (accent-drop, punctuation drift).
3. **EN exact** — remaining misses are matched against ``cards.name``.
   Since 2024 WotC no longer prints Portuguese for many products
   (magic.wizards.com/pt-BR/news/announcements/changes-to-magic-product-
   languages-in-2024), so those cards have no ``pt_names`` row and
   LigaMagic shows their *English* name — this pass recovers them.
4. **EN fuzzy** — pg_trgm best-match against ``cards.name_sort_key`` at
   the same ``0.4`` floor. Catches the residual case where LigaMagic
   shows an *unofficial* Portuguese rendering of a card that was never
   officially printed in PT (reordered / re-translated words), which
   matches neither the official ``pt_names`` nor ``cards.name`` exactly.

Returns ``oracle_id`` (not the printing ``cards.id`` that ``resolve.py``
returns) because the consumer matches against wishlist / inventory sets,
which are keyed by oracle. Reuses :class:`ResolveResult` for the shape.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import text

from biblioplex.decklist.resolve import FUZZY_THRESHOLD, ResolveResult

__all__ = ["resolve_pt_names"]


# DISTINCT ON collapses a printed name that maps to several oracle rows
# (rare, but reprints across functionally-distinct cards happen) to one
# deterministic winner — lowest oracle_id — so a name resolves the same
# way run to run.
_PT_EXACT_SQL = (
    "SELECT DISTINCT ON (LOWER(t.n)) t.n AS input_name, p.oracle_id "
    "FROM pt_names p "
    "JOIN unnest(CAST(:names AS text[])) AS t(n) "
    "ON LOWER(p.printed_name) = LOWER(t.n) "
    "ORDER BY LOWER(t.n), p.oracle_id"
)

# The `%` operator prefilter is what engages the pg_trgm GIN index
# (pt_names_lower_trgm); `similarity(...) >= :threshold` then enforces our
# 0.4 floor over that small candidate set. Without the `%`, `similarity()`
# in WHERE is a per-row function call → a seq scan of every pt_names row
# for EVERY miss, which times out on decks with hundreds of unresolved
# names (a multi-deck bundle). `%` uses pg_trgm's own threshold (default
# 0.3, a superset of 0.4) so the floor filter never loses a real match.
_PT_FUZZY_SQL = (
    "SELECT m.n AS input_name, f.oracle_id "
    "FROM unnest(CAST(:misses AS text[])) AS m(n) "
    "CROSS JOIN LATERAL ( "
    "  SELECT p.oracle_id "
    "  FROM pt_names p "
    "  WHERE LOWER(p.printed_name) % LOWER(m.n) "
    "    AND similarity(LOWER(p.printed_name), LOWER(m.n)) >= :threshold "
    "  ORDER BY similarity(LOWER(p.printed_name), LOWER(m.n)) DESC "
    "  LIMIT 1 "
    ") f"
)

_EN_EXACT_SQL = (
    "SELECT DISTINCT ON (LOWER(t.n)) t.n AS input_name, c.oracle_id "
    "FROM cards c "
    "JOIN unnest(CAST(:names AS text[])) AS t(n) "
    "ON LOWER(c.name) = LOWER(t.n) "
    "ORDER BY LOWER(t.n), c.oracle_id"
)

# Same index-backed shape as _PT_FUZZY_SQL, against cards.name_sort_key
# (backed by the cards_name_sort_key_trgm GIN index).
_EN_FUZZY_SQL = (
    "SELECT m.n AS input_name, f.oracle_id "
    "FROM unnest(CAST(:misses AS text[])) AS m(n) "
    "CROSS JOIN LATERAL ( "
    "  SELECT c.oracle_id "
    "  FROM cards c "
    "  WHERE c.name_sort_key % LOWER(m.n) "
    "    AND similarity(c.name_sort_key, LOWER(m.n)) >= :threshold "
    "  ORDER BY similarity(c.name_sort_key, LOWER(m.n)) DESC "
    "  LIMIT 1 "
    ") f"
)


async def resolve_pt_names(conn: Any, names: Iterable[str]) -> ResolveResult:
    """Resolve Portuguese (or English-fallback) card names to oracle ids.

    Args:
        conn: An open SQLAlchemy async connection.
        names: Card-name inputs in their original form. Duplicates are
            deduplicated by case-folded form; first occurrence wins the
            returned ordering.

    Returns:
        ``ResolveResult`` with ``matches`` (input → ``oracle_id``) in
        input order and ``unresolved`` (inputs that missed all three
        passes).
    """
    ordered_inputs: list[str] = []
    seen: set[str] = set()
    for raw in names:
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered_inputs.append(raw)
    if not ordered_inputs:
        return ResolveResult()

    matches: dict[str, UUID] = {}

    def _absorb(rows: Iterable[Any]) -> None:
        for row in rows:
            # A batched pass can echo an input only once (DISTINCT ON /
            # one-row LATERAL); guard anyway so an earlier pass's hit is
            # never overwritten by a lower-confidence later one.
            matches.setdefault(row.input_name, row.oracle_id)

    def _misses() -> list[str]:
        return [n for n in ordered_inputs if n not in matches]

    # Pass 1 — PT exact.
    _absorb(
        (await conn.execute(text(_PT_EXACT_SQL), {"names": ordered_inputs})).all()
    )

    # Pass 2 — PT fuzzy over the misses.
    misses = _misses()
    if misses:
        _absorb(
            (
                await conn.execute(
                    text(_PT_FUZZY_SQL),
                    {"misses": misses, "threshold": FUZZY_THRESHOLD},
                )
            ).all()
        )

    # Pass 3 — English exact fallback (cards never printed in Portuguese
    # show their English name on the source page).
    misses = _misses()
    if misses:
        _absorb(
            (await conn.execute(text(_EN_EXACT_SQL), {"names": misses})).all()
        )

    # Pass 4 — English fuzzy fallback for an unofficial PT rendering of a
    # never-in-PT card (matches neither pt_names nor cards.name exactly).
    misses = _misses()
    if misses:
        _absorb(
            (
                await conn.execute(
                    text(_EN_FUZZY_SQL),
                    {"misses": misses, "threshold": FUZZY_THRESHOLD},
                )
            ).all()
        )

    ordered_matches = {n: matches[n] for n in ordered_inputs if n in matches}
    unresolved = [n for n in ordered_inputs if n not in matches]
    return ResolveResult(matches=ordered_matches, unresolved=unresolved)
