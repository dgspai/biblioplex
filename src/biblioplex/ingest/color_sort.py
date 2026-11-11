"""Canonical WUBRG-cycle position for ``order=color`` per §6.1.

Scryfall's ``order=color`` algorithm is undocumented; this encodes the
Commander-culture canonical cycle decided in the spec-drift sweep:

  0  colorless
  1–5   mono — W, U, B, R, G
  6–10  2c allied — WU, UB, BR, RG, GW
  11–15 2c enemy  — WB, BG, UR, RW, UG
  16–20 3c shards — WUB, UBR, BRG, RGW, GWU
  21–25 3c wedges — WBG, URW, BGU, RWB, GUR
  26–30 4c        — UBRG, WBRG, WURG, WUBG, WUBR (missing W/U/B/R/G)
  31    5c WUBRG

The lookup table is keyed by tuples sorted in WUBRG letter order so any
input order normalizes to one key. Unknown letters are dropped before
lookup; a colors_eff array that contains nothing recognizable maps to
position 0 (colorless) rather than raising — ingest must not crash on
unexpected color data.
"""

from __future__ import annotations

_WUBRG_RANK: dict[str, int] = {"W": 0, "U": 1, "B": 2, "R": 3, "G": 4}

# Keyed by canonical (WUBRG-sorted, deduped) tuples. 32 entries total.
_POSITION: dict[tuple[str, ...], int] = {
    (): 0,
    ("W",): 1, ("U",): 2, ("B",): 3, ("R",): 4, ("G",): 5,
    # 2c allied (WU, UB, BR, RG, GW)
    ("W", "U"): 6, ("U", "B"): 7, ("B", "R"): 8, ("R", "G"): 9, ("W", "G"): 10,
    # 2c enemy (WB, BG, UR, RW, UG)
    ("W", "B"): 11, ("B", "G"): 12, ("U", "R"): 13, ("W", "R"): 14, ("U", "G"): 15,
    # 3c shards (WUB, UBR, BRG, RGW, GWU)
    ("W", "U", "B"): 16, ("U", "B", "R"): 17, ("B", "R", "G"): 18,
    ("W", "R", "G"): 19, ("W", "U", "G"): 20,
    # 3c wedges (WBG, URW, BGU, RWB, GUR)
    ("W", "B", "G"): 21, ("W", "U", "R"): 22, ("U", "B", "G"): 23,
    ("W", "B", "R"): 24, ("U", "R", "G"): 25,
    # 4c (missing W, U, B, R, G in WUBRG order)
    ("U", "B", "R", "G"): 26, ("W", "B", "R", "G"): 27,
    ("W", "U", "R", "G"): 28, ("W", "U", "B", "G"): 29,
    ("W", "U", "B", "R"): 30,
    # 5c
    ("W", "U", "B", "R", "G"): 31,
}


def color_sort_key(colors: list[str] | None) -> int:
    """Map a ``colors_eff`` array to its canonical WUBRG-cycle position (0–31).

    Sorts the input in WUBRG letter order, dedupes, drops any letters that
    aren't one of W/U/B/R/G, and looks the resulting tuple up in the cycle
    table. An empty or fully-unrecognized array returns ``0`` (colorless).
    """
    if not colors:
        return 0
    canonical = tuple(
        sorted({c for c in colors if c in _WUBRG_RANK}, key=_WUBRG_RANK.__getitem__)
    )
    return _POSITION.get(canonical, 0)
