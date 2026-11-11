"""Color-set helpers for the search DSL (tech_spec.md §5.4).

Exposes `parse_color_letters(s)` which accepts:

- Bare letter shorthand `WUBRG` / `C` (case-insensitive).
- Static nicknames: guilds (2-color), shards (3-color allied), wedges
  (3-color enemy), 4-color names, and full WUBRG.

Returns a `frozenset[str]` of uppercase color letters. Empty set means
colorless (only valid via the `colorless`/`c` aliases handled in the
compiler, not from `parse_color_letters`).
"""

from __future__ import annotations

__all__ = ["COLOR_NICKNAMES", "VALID_LETTERS", "parse_color_letters"]


VALID_LETTERS: frozenset[str] = frozenset({"W", "U", "B", "R", "G", "C"})


# Static nickname → letter-set map (Scryfall canon).
#
# 4-color names use the "absent color" convention from Commander culture:
# yore=no green, witch=no white, glint=no white (alt), ink=no red, dune=no blue,
# etc. We encode the most-used variants. Drift entry recorded for any user
# request whose nickname isn't represented here.
COLOR_NICKNAMES: dict[str, frozenset[str]] = {
    # ---- mono / catch-alls (handled separately too) ----
    "white": frozenset({"W"}),
    "blue": frozenset({"U"}),
    "black": frozenset({"B"}),
    "red": frozenset({"R"}),
    "green": frozenset({"G"}),
    # ---- guilds (2-color) ----
    "azorius": frozenset({"W", "U"}),
    "dimir": frozenset({"U", "B"}),
    "rakdos": frozenset({"B", "R"}),
    "gruul": frozenset({"R", "G"}),
    "selesnya": frozenset({"G", "W"}),
    "orzhov": frozenset({"W", "B"}),
    "izzet": frozenset({"U", "R"}),
    "golgari": frozenset({"B", "G"}),
    "boros": frozenset({"R", "W"}),
    "simic": frozenset({"G", "U"}),
    # ---- shards (3-color allied) ----
    "bant": frozenset({"G", "W", "U"}),
    "esper": frozenset({"W", "U", "B"}),
    "grixis": frozenset({"U", "B", "R"}),
    "jund": frozenset({"B", "R", "G"}),
    "naya": frozenset({"R", "G", "W"}),
    # ---- wedges (3-color enemy) ----
    "abzan": frozenset({"W", "B", "G"}),
    "jeskai": frozenset({"U", "R", "W"}),
    "sultai": frozenset({"B", "G", "U"}),
    "mardu": frozenset({"R", "W", "B"}),
    "temur": frozenset({"G", "U", "R"}),
    # ---- 4-color (most common naming, "missing X") ----
    "yore": frozenset({"W", "U", "B", "R"}),       # no green
    "yoretiller": frozenset({"W", "U", "B", "R"}),
    "witch": frozenset({"U", "B", "R", "G"}),      # no white
    "witchmaw": frozenset({"U", "B", "R", "G"}),
    "ink": frozenset({"W", "U", "B", "G"}),        # no red
    "inktreader": frozenset({"W", "U", "B", "G"}),
    "dune": frozenset({"W", "B", "R", "G"}),       # no blue
    "dunebrood": frozenset({"W", "B", "R", "G"}),
    "glint": frozenset({"U", "B", "R", "G"}),      # no white (alt to "witch")
    "glinteye": frozenset({"U", "B", "R", "G"}),
    # ---- 5-color ----
    "wubrg": frozenset({"W", "U", "B", "R", "G"}),
    "rainbow": frozenset({"W", "U", "B", "R", "G"}),
}


def parse_color_letters(value: str) -> frozenset[str]:
    """Resolve `value` (bare letters or a nickname) to a color-letter set.

    Raises:
        ValueError: when `value` is neither a known nickname nor a string
            composed entirely of `WUBRGC` letters (case-insensitive).
    """
    lowered = value.lower().strip()
    if not lowered:
        raise ValueError("empty color spec")

    if lowered in COLOR_NICKNAMES:
        return COLOR_NICKNAMES[lowered]

    # Bare letters path.
    upper = lowered.upper()
    if all(ch in VALID_LETTERS for ch in upper):
        return frozenset(upper)

    raise ValueError(f"unknown color spec: {value}")
