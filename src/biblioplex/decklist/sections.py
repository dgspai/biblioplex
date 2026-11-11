"""Section-aware decklist parsing (D19.2).

`parse_decklist` (§6.7) treats every line as a card, which is right for a
pasted list and wrong for a file exported from a deckbuilding site: those
carry section headings, and a heading parsed as a card lands in
`unresolved` and inflates the deck size. Measured on the reference exports
in `data/example_deck_exports/` before this module existed —
`archidekt/colorless_arsenal.txt` yielded phantom `Commander` and
`Mainboard` cards, `moxfield/big-boys-arena.txt` yielded four more.

This wraps `parse_decklist` rather than replacing it: the line grammar,
the quantity rules and Bug.33's round-trip property all stay in one place,
and every existing `/decks/*` caller is untouched. All this layer does is
decide which bucket a line belongs to, then hand each bucket's lines to
the existing parser.

A heading is recognised only from a fixed allowlist, because a heading and
a quantity-less card name have exactly the same shape — `Sol Ring` on its
own line is legal input, so anything not on the list stays a card. The
allowlist cannot drift into false positives (no Magic card is named
`Sideboard`), and a line carrying a quantity can never match it.

WARNING (D19.10): the returned buckets are NOT a partition. A commander
and a companion are cards the deck contains, so they appear in `main` AS
WELL AS in their own list. `main + sideboard` is the whole deck; adding
`commander` or `companion` on top double-counts them. `decklist_counts`
in `decks/service.py` is the canonical consumer to copy.

ponytail: one dict, one loop, no per-site format negotiation. Adding a
site means adding a word to `_SECTIONS`, not a handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from biblioplex.decklist.parser import DeckEntry, parse_decklist

__all__ = ["DeckSections", "parse_sections", "subtract_entries"]

# Heading -> bucket. `about` and `drop` are pseudo-buckets handled inline.
#
# `companion` is its own bucket (D19.10) rather than sideboard: a deck
# has at most one, it is tracked as its own field on `decks`, and folding
# it into the sideboard made it indistinguishable from an ordinary
# sideboard card. `maybeboard` and `tokens` are dropped because they are
# explicitly NOT part of the deck; that is semantics, not data loss.
_SECTIONS: dict[str, str] = {
    "about": "about",
    "commander": "commander",
    "commanders": "commander",
    "deck": "main",
    "main": "main",
    "mainboard": "main",
    "sideboard": "sideboard",
    "companion": "companion",
    "maybeboard": "drop",
    "tokens": "drop",
}

# Arena opens with an `About` block whose only useful line is `Name <deck>`.
# Scoped to that block on purpose: outside it, `Name …` is just a card whose
# name begins with the word (`Name Sticker Goblin`).
_NAME_PREFIX = "name "


@dataclass(frozen=True)
class DeckSections:
    """One decklist split into its parts.

    **These lists are not a partition.** `commander` and `companion`
    entries are ALSO present in `main`, because they are cards the deck
    contains — a 100-card Commander deck has 100 entries in `main`, one of
    which is the commander. So:

        whole deck  == main + sideboard          # correct
        whole deck  == commander + main + ...    # double-counts

    Getting this wrong inflates deck sizes by one or two cards, which is
    exactly the sort of off-by-one that survives review. See
    `decks/service.py:decklist_counts` for the canonical consumer.

    `commander` is a list, not a string: two-commander decks (partner,
    background, friends-forever) are legal and collapsing them here would
    lose one. `companion` is a list for symmetry only — a deck has at most
    one. Callers that need a single value decide how to join.
    """

    name: str | None = None
    commander: list[DeckEntry] = field(default_factory=list)
    companion: list[DeckEntry] = field(default_factory=list)
    main: list[DeckEntry] = field(default_factory=list)
    sideboard: list[DeckEntry] = field(default_factory=list)


def parse_sections(text: str) -> DeckSections:
    """Split a decklist into commander / companion / main / sideboard.

    Lines before any heading go to `main`, which is what makes the
    heading-less exports (plain Moxfield, MTGO) work unchanged. Headings
    match case-insensitively and tolerate a trailing colon — Deckbox
    writes `Sideboard:`, and without the colon rule that line reads as a
    card and inflates the deck by one.

    Commander and companion lines are appended to `main` as they are read,
    so `main` holds the full card pool in source order. See the class
    docstring: the result is deliberately not a partition.
    """
    buckets: dict[str, list[str]] = {
        "commander": [], "companion": [], "main": [], "sideboard": []
    }
    name: str | None = None
    current = "main"

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        target = _SECTIONS.get(stripped.rstrip(":").strip().lower())
        if target is not None:
            current = target
            continue

        if current == "about":
            if name is None and stripped.lower().startswith(_NAME_PREFIX):
                name = stripped[len(_NAME_PREFIX):].strip() or None
            continue
        if current == "drop":
            continue

        buckets[current].append(raw_line)
        # A commander/companion is also a card in the deck. Appended here,
        # while walking, so `main` keeps source order rather than getting
        # them prepended or tacked on the end.
        if current in ("commander", "companion"):
            buckets["main"].append(raw_line)

    return DeckSections(
        name=name,
        commander=parse_decklist("\n".join(buckets["commander"])),
        companion=parse_decklist("\n".join(buckets["companion"])),
        main=parse_decklist("\n".join(buckets["main"])),
        sideboard=parse_decklist("\n".join(buckets["sideboard"])),
    )


def subtract_entries(
    entries: list[DeckEntry], *removals: list[DeckEntry]
) -> list[DeckEntry]:
    """`entries` minus `removals`, matched by name and BY QUANTITY.

    `DeckSections` is not a partition — commander and companion sit inside
    `main` (D19.10) — so any consumer needing a true partition has to
    subtract them back out. Doing it by quantity rather than by name means
    a commander that is also a legal singleton elsewhere in the list keeps
    its other copy.

    Callers choose what to remove, deliberately: `/decks/legality` excludes
    only the commander (a companion occupies a sideboard slot, and folding
    it in here would silently change a shipped deck-size check), while
    `deck_cards` excludes both.
    """
    remaining: dict[str, int] = {}
    for group in removals:
        for entry in group:
            key = entry.name.lower()
            remaining[key] = remaining.get(key, 0) + entry.qty

    out: list[DeckEntry] = []
    for entry in entries:
        key = entry.name.lower()
        taken = min(remaining.get(key, 0), entry.qty)
        if taken:
            remaining[key] -= taken
        if entry.qty - taken > 0:
            out.append(DeckEntry(qty=entry.qty - taken, name=entry.name))
    return out
