"""Mana cost parsing for the search DSL (tech_spec.md §5.5).

A mana cost string like ``{2}{W}{W}`` is conceptually a multiset of symbols.
Per spec: generic numerals are EXPANDED — ``{2}`` → two ``{1}`` symbols — so
``2WW`` and ``{2}{W}{W}`` both tokenize to ``{1,1,W,W}``.

We also accept the Scryfall shorthand for bare letters:

* ``WW``  → ``{W}{W}``
* ``2WW`` → ``{1,1,W,W}``
* Hybrid / phyrexian / numeric-in-braces ALWAYS need braces: ``{W/U}``,
  ``{W/P}``, ``{2/W}``.

This module is pure Python — it returns multisets for query-side comparison
and the SQL compiler builds Postgres expressions from them.
"""

from __future__ import annotations

import re
from collections import Counter

__all__ = ["parse_cost_symbols", "GENERIC"]


# Sentinel for a generic (colorless numeric) pip after expansion. We keep
# generic distinct from the colorless ``{C}`` symbol — they're different in
# Magic and in Scryfall.
GENERIC = "1"


# A braced mana symbol: anything between {}, no nested braces.
_BRACED = re.compile(r"\{([^{}]+)\}")

# Bare-letter colors accepted as shorthand for ``{W}``-style.
_BARE_LETTERS = frozenset({"W", "U", "B", "R", "G", "C", "S", "X", "Y", "Z"})


def parse_cost_symbols(cost: str) -> Counter[str]:
    """Tokenize a mana-cost string into a multiset of symbols.

    Generic numerics (``{2}`` or bare ``2`` shorthand) expand to ``GENERIC``
    sentinels. Other symbols are stored case-normalized (uppercase, no spaces).

    Args:
        cost: a raw cost like ``{2}{W}{W}``, ``2WW``, ``{W/U}{R}``.

    Returns:
        Counter mapping symbol → count. Empty Counter on empty / whitespace
        input. Unrecognized characters are skipped silently — the search DSL
        should be lenient, not validating Magic-card legality.
    """
    out: Counter[str] = Counter()
    if not cost:
        return out

    s = cost.strip()
    if not s:
        return out

    # Walk left-to-right: braced symbols vs bare-letter shorthand.
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "{":
            m = _BRACED.match(s, i)
            if m is None:
                # Malformed brace; skip.
                i += 1
                continue
            payload = m.group(1).strip().upper()
            _accumulate(out, payload)
            i = m.end()
            continue
        if ch.isdigit():
            # Bare digit run → generic copies.
            j = i
            while j < n and s[j].isdigit():
                j += 1
            try:
                k = int(s[i:j])
            except ValueError:
                k = 0
            out[GENERIC] += k
            i = j
            continue
        upper = ch.upper()
        if upper in _BARE_LETTERS:
            out[upper] += 1
            i += 1
            continue
        # Unknown char: skip rather than raise.
        i += 1
    return out


def _accumulate(out: Counter[str], payload: str) -> None:
    """Add a braced symbol payload to the multiset, expanding generic numerics.

    ``2``    → 2x GENERIC
    ``W``    → 1x W
    ``W/U``  → 1x W/U (hybrid stays composite)
    ``W/P``  → 1x W/P (phyrexian stays composite)
    ``2/W``  → 1x 2/W (hybrid generic-or-colored stays composite)
    """
    if not payload:
        return
    if payload.isdigit():
        out[GENERIC] += int(payload)
        return
    out[payload] += 1
