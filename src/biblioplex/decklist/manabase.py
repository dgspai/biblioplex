"""Deck manabase builder (D14.6).

Hand-curated nonbasic priority list + proportional basic distribution.
Given a commander color identity + a target land count + optional
budget cap + optional pip weights, returns a recommended land package.

**Not a full land database.** The priority list is ~35 lands chosen for
broad applicability (rainbow fixing, fetches, shocks, common utility).
Operators looking for a deep tuned mana base should treat the output
as a starting point, not a final answer. When the list stops matching
user needs, the fix is either (a) extend the list, or (b) move to a
data-driven "top-K CI-compatible lands by EDHrec-rank" query using the
existing `cards_edhrec_rank_idx`.
"""

from __future__ import annotations

# Priority list — top of list wins first. Curated to give reasonable
# results for the common commander CIs (mono through 5c). Entries here
# should be lands that a majority of decks in the matching CI want; the
# endpoint filters them down by `color_identity <@ commander_ci` at
# query time, so an over-inclusive list is fine.
NONBASIC_PRIORITY: list[str] = [
    # Rainbow / any-color fixing
    "Command Tower",
    "Path of Ancestry",
    "Exotic Orchard",
    "Reflecting Pool",
    "City of Brass",
    "Mana Confluence",
    "Prismatic Vista",
    "Fabled Passage",
    "Terramorphic Expanse",
    "Evolving Wilds",
    # Zendikar / Onslaught fetches (10)
    "Flooded Strand",
    "Polluted Delta",
    "Bloodstained Mire",
    "Wooded Foothills",
    "Windswept Heath",
    "Marsh Flats",
    "Scalding Tarn",
    "Verdant Catacombs",
    "Arid Mesa",
    "Misty Rainforest",
    # Ravnica shocklands (10)
    "Hallowed Fountain",
    "Watery Grave",
    "Blood Crypt",
    "Stomping Ground",
    "Temple Garden",
    "Godless Shrine",
    "Overgrown Tomb",
    "Steam Vents",
    "Breeding Pool",
    "Sacred Foundry",
    # Utility
    "Reliquary Tower",
    "Bojuka Bog",
    "Strip Mine",
    "Wasteland",
    "Ash Barrens",
    "Rogue's Passage",
]

# Basic-land name per color letter. `"C"` maps to Wastes so colorless
# commanders (D16.5) get their remaining slots filled instead of the
# prior "out of scope" note.
BASIC_NAMES: dict[str, str] = {
    "W": "Plains",
    "U": "Island",
    "B": "Swamp",
    "R": "Mountain",
    "G": "Forest",
    "C": "Wastes",
}


def distribute_basics(
    pip_weights: dict[str, int], basic_count: int
) -> dict[str, int]:
    """Distribute ``basic_count`` basics across colors proportionally.

    Uses the largest-remainder (Hare quota) method so integer rounding
    stays fair — mono-color decks get all basics of that color, split
    decks split proportionally to their pip weight.

    Args:
        pip_weights: WUBRG(C) letter → integer weight. Zero-weight
            colors get zero basics regardless of the total. Absent
            colors treated as zero. `"C"` (colorless / Wastes) is
            distributed alongside the colors — D16.5.
        basic_count: Total basics to distribute. Zero → empty dict.

    Returns:
        WUBRGC letter → basic count. Colors with a zero final count are
        included with value 0 so the response shape stays uniform.
    """
    letters = ("W", "U", "B", "R", "G", "C")
    out = {c: 0 for c in letters}
    if basic_count <= 0:
        return out
    total_weight = sum(max(0, pip_weights.get(c, 0)) for c in letters)
    if total_weight <= 0:
        return out
    # Compute the ideal (fractional) allocation, then hand out the floor
    # to every color and distribute the remaining seats to the colors
    # with the largest fractional remainders.
    fractional: dict[str, float] = {}
    floors: dict[str, int] = {}
    for c in letters:
        w = max(0, pip_weights.get(c, 0))
        ideal = basic_count * w / total_weight
        floors[c] = int(ideal)
        fractional[c] = ideal - floors[c]
    assigned = sum(floors.values())
    remainder = basic_count - assigned
    # Distribute leftovers to largest fractional remainders (tie-break
    # by WUBRG order — deterministic).
    order = sorted(
        letters, key=lambda c: (-fractional[c], letters.index(c))
    )
    for c in order[:remainder]:
        floors[c] += 1
    out.update(floors)
    return out
