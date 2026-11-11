"""WOTC Commander bracket estimator (D14.4).

Rough heuristic mapping a resolved decklist to a 1–5 bracket per WOTC's
Commander bracket system (announced Sept 2024). The rubric is
qualitative; this module encodes a defensible interpretation into a
small ladder of curated card lists + a combo-count signal fed from the
D14.1 Commander Spellbook ingest.

**Not authoritative.** WOTC's system is a spirit-of-the-format guideline
without hard thresholds. Signals + thresholds here are calibrated
against a handful of well-known reference decks (precons for bracket 2,
mid-power upgrades for bracket 3, cEDH for bracket 5) and should be
treated as a rough estimate rather than a definitive score. See the
`Spec Drift / §6.10` entry in ``todo.md`` for the resolution path.

Signal sources:

* ``game_changers`` — WOTC's published "game changer" list (Sept 2024,
  updates rare). ~40 cards, curated frozenset. Kept as a name-based
  check because WOTC's list is authoritative and no Tagger equivalent
  exists.
* ``fast_mana`` — Scryfall Tagger ``fast-mana`` slug (D15.25). Overlaps
  with ``game_changers`` intentionally — a card can be both.
* ``mld`` — Scryfall Tagger ``mass-land-destruction`` slug (D15.25).
* ``tutors`` — Scryfall Tagger ``tutor`` slug (D15.25). Broader than
  the old curated "any card in library" list — includes narrow tutors
  the Tagger community classifies as tutors.
* ``extra_turns`` — Scryfall Tagger ``extra-turn`` slug (D15.25).
* ``combos`` — count of ingested Commander Spellbook combos whose full
  card set is a subset of the deck (computed at the router; passed in
  to :func:`estimate_bracket`).

D15.25 migrated 4 of 5 signals off curated frozensets and onto the
``oracle_tags`` side table. The router fires ONE aggregate query
(``COUNT(*) FILTER (WHERE tag = :slug)`` per signal) alongside the
existing combo count and passes the tag counts through to
:func:`estimate_bracket`. New sets auto-inherit signals via the next
refresh's oracle-tag ingest — no code push needed. When
``oracle_tags`` is empty (fresh deployment, tag ingest not yet run),
the 4 tag-based signals report 0 and the bracket falls back to
``game_changers`` + ``combos`` alone.

Bracket assignment (ladder, first match wins):

* 5 (cEDH) — ``combos >= 2`` OR (``combos >= 1`` AND ``fast_mana >= 3``)
* 4 (Optimized) — ``combos >= 1`` OR ``game_changers >= 4`` OR
  ``mld >= 1``
* 3 (Upgraded) — ``game_changers >= 1`` OR ``fast_mana >= 2`` OR
  ``tutors >= 3`` OR ``extra_turns >= 1``
* 2 (Core) — ``tutors >= 1`` OR ``fast_mana >= 1``
* 1 (Exhibition) — otherwise
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Curated lists — all keys are lower-case, name-only.
# Padding these is safe (unknown cards don't false-positive); the risk
# is stale removals (a card leaves the WOTC list — we still flag it).
# When maintenance becomes painful, migrate to `otag:` slugs and drop
# these constants.
GAME_CHANGERS: frozenset[str] = frozenset({
    "ancient tomb",
    "bolas's citadel",
    "chrome mox",
    "coalition victory",
    "consecrated sphinx",
    "crucible of worlds",
    "cyclonic rift",
    "demonic tutor",
    "drannith magistrate",
    "expropriate",
    "fierce guardianship",
    "force of will",
    "gaea's cradle",
    "grim monolith",
    "hullbreacher",
    "jeweled lotus",
    "kinnan, bonder prodigy",
    "mana crypt",
    "mana drain",
    "mana vault",
    "mishra's workshop",
    "mox diamond",
    "mox opal",
    "mystical tutor",
    "narset, parter of veils",
    "necropotence",
    "opposition agent",
    "rhystic study",
    "sensei's divining top",
    "serra's sanctum",
    "smothering tithe",
    "sway of the stars",
    "teferi's puzzle box",
    "the tabernacle at pendrell vale",
    "thassa's oracle",
    "tolarian academy",
    "trouble in pairs",
    "vampiric tutor",
    "vorinclex, voice of hunger",
})

# D15.25 — Scryfall Tagger slug per bracket signal. Router queries
# ``oracle_tags`` via `COUNT(*) FILTER (WHERE tag = :slug)` for each
# entry so new sets auto-inherit signal detection via the next
# oracle-tag ingest. If Tagger renames a slug (rare), swap the value
# here — no other code change needed.
SIGNAL_TAG_SLUGS: dict[str, str] = {
    "fast_mana": "fast-mana",
    "mld": "mass-land-destruction",
    "tutors": "tutor",
    "extra_turns": "extra-turn",
}


@dataclass(frozen=True)
class BracketEstimate:
    """Result of :func:`estimate_bracket`."""

    bracket: int
    signals: dict[str, int]
    game_changer_names: list[str] = field(default_factory=list)
    combos: int = 0
    notes: list[str] = field(default_factory=list)


def _count_hits(deck_names_lower: list[str], catalog: frozenset[str]) -> int:
    return sum(1 for n in deck_names_lower if n in catalog)


def _matching_names(
    deck_names_lower: list[str],
    catalog: frozenset[str],
    original_by_lower: dict[str, str] | None = None,
) -> list[str]:
    """Return deck names that hit ``catalog`` — deduped, sorted.

    When ``original_by_lower`` is supplied, matched lowered names are
    mapped back to the original casing (Bug.8) — the response's
    ``game_changers`` field surfaces `Cyclonic Rift`, not
    `cyclonic rift`. Missing entries fall back to the lower form so
    a partial map is still safe.
    """
    hits = sorted({n for n in deck_names_lower if n in catalog})
    if original_by_lower is None:
        return hits
    return [original_by_lower.get(n, n) for n in hits]


def estimate_bracket(
    deck_names_lower: list[str],
    combos: int,
    original_by_lower: dict[str, str] | None = None,
    combos_enabled: bool = True,
    tag_signal_counts: dict[str, int] | None = None,
) -> BracketEstimate:
    """Compute a 1–5 bracket estimate from a resolved decklist.

    Args:
        deck_names_lower: Lower-cased card names from the resolved deck.
            Used ONLY for the ``game_changers`` signal (WOTC's curated
            list) — the other 4 signals now come from ``oracle_tags``
            via ``tag_signal_counts`` (D15.25).
        combos: Number of ingested combos present in the deck (subset
            match against ``combos.oracle_ids``). Fed in by the router
            after firing the D14.3 combo-count SQL.
        original_by_lower: Optional map from lower-cased name → the
            original (proper-cased) name from the resolved deck row.
            When supplied, ``BracketEstimate.game_changer_names`` uses
            the proper casings so the wire response reads
            ``"Cyclonic Rift"`` instead of ``"cyclonic rift"`` (Bug.8).
        combos_enabled: D15.18 — whether the deployment ingests combos
            (i.e. ``settings.combos_source_url`` is non-empty). When
            False and the ladder assigns bracket ≤ 3, an explanatory
            note is appended so users know the bracket 4/5 signal is
            deployment-disabled rather than genuinely absent.
        tag_signal_counts: D15.25 — dict of pre-computed tag hit counts
            keyed by signal name (``fast_mana``, ``mld``, ``tutors``,
            ``extra_turns``). Router fires ONE aggregate SQL over
            ``oracle_tags`` and passes the map. ``None`` (or missing
            keys) → 0 for the absent signals so unit tests that don't
            care about tag-based signals stay concise.

    Returns:
        A :class:`BracketEstimate` bundling the per-signal counts, the
        chosen bracket, and one-line reason notes explaining which
        signals triggered the bracket floor.
    """
    tag_counts = tag_signal_counts or {}
    signals = {
        "game_changers": _count_hits(deck_names_lower, GAME_CHANGERS),
        "fast_mana": int(tag_counts.get("fast_mana", 0)),
        "mld": int(tag_counts.get("mld", 0)),
        "tutors": int(tag_counts.get("tutors", 0)),
        "extra_turns": int(tag_counts.get("extra_turns", 0)),
        "combos": combos,
    }
    notes: list[str] = []
    bracket: int

    if combos >= 2 or (combos >= 1 and signals["fast_mana"] >= 3):
        bracket = 5
        notes.append(
            f"cEDH signals: {combos} combos + {signals['fast_mana']} fast-mana cards"
        )
    elif combos >= 1 or signals["game_changers"] >= 4 or signals["mld"] >= 1:
        bracket = 4
        if combos >= 1:
            notes.append(f"{combos} combo(s) present")
        if signals["game_changers"] >= 4:
            notes.append(f"{signals['game_changers']} game-changer cards")
        if signals["mld"] >= 1:
            notes.append(f"{signals['mld']} mass land destruction spell(s)")
    elif (
        signals["game_changers"] >= 1
        or signals["fast_mana"] >= 2
        or signals["tutors"] >= 3
        or signals["extra_turns"] >= 1
    ):
        bracket = 3
        if signals["game_changers"] >= 1:
            notes.append(f"{signals['game_changers']} game-changer card(s)")
        if signals["fast_mana"] >= 2:
            notes.append(f"{signals['fast_mana']} fast-mana cards")
        if signals["tutors"] >= 3:
            notes.append(f"{signals['tutors']} tutors")
        if signals["extra_turns"] >= 1:
            notes.append(f"{signals['extra_turns']} extra-turn spell(s)")
    elif signals["tutors"] >= 1 or signals["fast_mana"] >= 1:
        bracket = 2
        if signals["tutors"] >= 1:
            notes.append(f"{signals['tutors']} tutor(s)")
        if signals["fast_mana"] >= 1:
            notes.append(f"{signals['fast_mana']} fast-mana card")
    else:
        bracket = 1
        notes.append("no bracket-shifting signals detected")

    if not combos_enabled and bracket <= 3:
        notes.append(
            "combos signal disabled — set COMBOS_SOURCE_URL to enable "
            "bracket 4/5 detection"
        )

    return BracketEstimate(
        bracket=bracket,
        signals=signals,
        game_changer_names=_matching_names(
            deck_names_lower, GAME_CHANGERS, original_by_lower
        ),
        combos=combos,
        notes=notes,
    )


def to_payload(estimate: BracketEstimate) -> dict[str, Any]:
    """Serialize a :class:`BracketEstimate` to the wire response shape."""
    return {
        "object": "deck_bracket",
        "bracket": estimate.bracket,
        "signals": estimate.signals,
        "game_changers": estimate.game_changer_names,
        "notes": estimate.notes,
    }
