"""Plaintext decklist parser for `POST /decks/analyze` (tech_spec.md §6.7).

Grammar is intentionally loose so Arena/MTGO/cubeware exports all parse:

    entry := WS? COUNT? ('x'|'X')? WS NAME WS?

`COUNT` defaults to 1 when absent. Lines whose first non-whitespace
character is `#` or `//` are treated as comments and dropped. Blank
lines are dropped. Section headers (`Deck`, `Sideboard`, `Commander`,
…) flow through as 1-word "names" and end up in `unresolved` downstream
— cheaper than maintaining a header allowlist that will drift with
every new client format.

ponytail: one regex, no tokenizer, no Arena-specific format negotiation.
Split-card names like ``Fire // Ice`` parse correctly because the
comment marker is anchored to the start of the line, not mid-string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["DeckEntry", "normalize_card_name", "parse_decklist"]


_QTY_CAP = 99
# Bug.28: the qty-marker alternation is split into three explicit branches
# instead of Bug.15's `[xX]?(?:\s+|(?=[A-Za-z]))`. That earlier shape let
# the `[xX]` marker be consumed on `"1 XA"` because the alphabetic
# look-ahead applied unconditionally — the `X` was gobbled as a marker
# and `A` became the name. The three branches are:
#   * `[xX]\s+`            — `4x Lightning Bolt` / `1X Sol Ring` (marker
#                            attached to the digits, whitespace after).
#   * `[xX](?=[A-Za-z])`   — `1xSol Ring` cubeware (Bug.15: marker glued
#                            to digits AND glued to name; no whitespace).
#   * `\s+`                — `4 Lightning Bolt` (no marker at all).
# Every marker branch requires the marker to be *directly* attached to
# the digits, so a bare `X` at the start of a name can never be misread.
#
# Bug.33: the first branch used to read `\s*[xX]\s+`, and that leading
# `\s*` allowed a DETACHED marker. `"X 0"` parses to name `"X 0"`, but
# `decklist/format.py` re-serializes it as `"1 X 0"`, which came back as
# name `"0"` — the `X` had been eaten as a multiplier. format → parse is
# a production round-trip (`/decks/*` hands back re-serialized lists), so
# a name-mangling asymmetry there corrupts real decklists.
#
# A narrower guard cannot fix this. `"X <anything>"` is genuinely
# ambiguous once a detached marker is legal, so for EVERY guard there is
# some name that round-trips wrong — which is exactly what the
# `st.text()` property test keeps finding. Requiring adjacency removes
# the ambiguity outright: a detached `x` is now always part of the name,
# so `"1 X foo"` -> `"X foo"` and the invariant holds for arbitrary text.
# Known cost: `"4 x Lightning Bolt"` (spaces both sides) now parses as
# name `"x Lightning Bolt"`. Exports write `4x` or `4 `; the spaced form
# is hand-typed and rare, and it is the only way to buy back a property
# that holds for every input.
_LINE_RE = re.compile(
    r"""
    ^\s*
    (?:(?P<qty>\d+)(?:[xX]\s+|[xX](?=[A-Za-z])|\s+))?
    (?P<name>\S.*?)
    \s*$
    """,
    re.VERBOSE,
)
_COMMENT_RE = re.compile(r"^\s*(#|//)")

# Bug.3: Arena/MTGO/MTGA exports append set-code / collector-number / foil
# markers to the name. Strip them off the tail so `Sol Ring (C21) 263 *F*`
# resolves to `Sol Ring`. Anchored to the end of the name so split-card
# names like `Fire // Ice` are preserved. Applied repeatedly so any
# combination of `(SET) NNN` + `[SET]` + `*F*` peels off in one pass.
#
# Bug.36 reshaped the two set-code branches, in opposite directions.
#
# The collector number is NOT always plain digits. A real 62-deck Moxfield
# sync produced `(PLST) C19-217`, `(PEOE) 260p`, `(HML) 32b`, `(PXLN) 46s`
# and `(PGRN) 123★` — none of which `\d+` matches, so the whole annotation
# stayed glued to the name on 235 index rows. The token is therefore any
# run of non-space, non-bracket characters that CONTAINS a digit; the digit
# requirement is what stops a trailing English word being eaten.
#
# And the parenthesised form now REQUIRES that collector, because
# `Hazmat Suit (Used)` is a genuine card name — the only one in the corpus
# shaped like a set code, verified against the live 34,593-card table — and
# the old pattern silently ate `(Used)`. Every annotation in every reference
# export carries a collector number, so demanding one costs nothing real.
# The bracket form keeps its collector optional because no card name
# contains `[XXXX]`, also verified; `1 Sol Ring [C21]` must keep working.
#
_COLLECTOR = r"(?=\S*\d)[^\s()\[\]]+"
_TAIL_ANNOTATION_RE = re.compile(
    rf"""
    (?:
        \s*\*[A-Za-z]\*                          # MTGA foil marker *F* / *E*
      | \s*\([A-Za-z0-9]{{2,6}}\)\s*{_COLLECTOR}    # (SET) collector — required
      | \s*\[[A-Za-z0-9]{{2,6}}\](?:\s*{_COLLECTOR})?  # [SET] [collector]
    )+
    \s*$
    """,
    re.VERBOSE,
)


# D19.8: `cards.name` spells split cards `Fire // Ice`, but exports write
# `Fire//Ice` (Deckbox) or `Fire/Ice` variants. This used to live twice —
# `inventory/deckbox.py:_clean_card_name` and
# `decklist/importers.py:_clean_name` — and two copies of a name-matching
# rule are exactly what drifts. It now lives here, applied by
# `parse_decklist` itself, so EVERY decklist source inherits it including
# plain pasted text, which never had it before.
#
# The `" // " not in name` guard is what makes it idempotent, and
# idempotence is load-bearing: `/decks/*` responses re-serialize parsed
# lists and hand them back, so a name that changed on every pass would
# corrupt decklists exactly the way Bug.33 did.


def normalize_card_name(raw_name: str) -> str:
    """Trim a card name and canonicalise split-card spacing.

    ``Fire//Ice`` -> ``Fire // Ice``. A name that already contains the
    canonical `` // `` separator is returned untouched, which keeps the
    function idempotent.
    """
    name = raw_name.strip()
    if "//" in name and " // " not in name:
        name = " // ".join(part.strip() for part in name.split("//")).strip()
    return name


@dataclass(frozen=True)
class DeckEntry:
    """One resolved line of decklist input."""

    qty: int
    name: str


def parse_decklist(text: str) -> list[DeckEntry]:
    """Parse a plaintext decklist into `DeckEntry` rows.

    Behavior:
      * Comments (lines starting with ``#`` or ``//``) and blank lines
        are dropped.
      * Missing quantity defaults to 1.
      * Quantities are clamped to ``[1, 99]`` per line; ``0`` drops the
        line entirely (the user explicitly said "none of this card").
      * Whitespace around name is trimmed; interior whitespace
        (including the ``//`` separator inside split-card names) is
        preserved.
    """
    out: list[DeckEntry] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        if _COMMENT_RE.match(raw_line):
            continue
        m = _LINE_RE.match(raw_line)
        if not m:
            continue
        qty_str = m.group("qty")
        qty = int(qty_str) if qty_str is not None else 1
        if qty <= 0:
            continue
        if qty > _QTY_CAP:
            qty = _QTY_CAP
        name = m.group("name").strip()
        name = _TAIL_ANNOTATION_RE.sub("", name).strip()
        # After the tail strip, so `Fire//Ice (MH2) 290` loses the
        # annotation first and only then gets its spacing fixed.
        name = normalize_card_name(name)
        if not name:
            continue
        out.append(DeckEntry(qty=qty, name=name))
    return out
