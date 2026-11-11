"""D19.2 — section-aware decklist parsing.

Ground truth for the fixture pins was measured directly off the files in
`data/example_deck_exports/`, not derived from this module. Counts are
pinned PER FILE and never asserted equal across files: the Moxfield
exports of the same deck genuinely disagree, because the Arena export
drops what is not Arena-legal (77 names / 114 cards plain vs 51 / 88).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from biblioplex.decklist.sections import DeckSections, parse_sections

EXPORTS = Path(__file__).resolve().parent.parent / "data" / "example_deck_exports"


def _cards(entries: list) -> int:
    return sum(e.qty for e in entries)


# ---- heading recognition ----


def test_no_headings_puts_everything_in_main() -> None:
    """The plain Moxfield/MTGO shape has no headings at all."""
    s = parse_sections("1 Sol Ring\n4 Lightning Bolt\n")
    assert [e.name for e in s.main] == ["Sol Ring", "Lightning Bolt"]
    assert s.commander == []
    assert s.sideboard == []
    assert s.name is None


def test_commander_heading_splits_commander_out() -> None:
    """The commander is tracked separately AND kept in the card pool."""
    s = parse_sections("Commander\n1 Hope of Ghirapur\n\nMainboard\n1 Sol Ring\n")
    assert [e.name for e in s.commander] == ["Hope of Ghirapur"]
    assert [e.name for e in s.main] == ["Hope of Ghirapur", "Sol Ring"]


def test_commander_section_keeps_both_partners() -> None:
    """Two commanders is a legal deck — the parser must not collapse them."""
    s = parse_sections("Commander\n1 Thrasios, Triton Hero\n1 Tymna the Weaver\n")
    assert len(s.commander) == 2


def test_sideboard_heading_tolerates_a_trailing_colon() -> None:
    """`Sideboard:` is what Deckbox writes — the colon is load-bearing.

    Without it the line parses as a card, which is the entire difference
    between `deckbox/iroh-gift.txt` and its CSV sibling.
    """
    s = parse_sections("1 Sol Ring\nSideboard:\n4 Grapeshot\n")
    assert [e.name for e in s.main] == ["Sol Ring"]
    assert [e.name for e in s.sideboard] == ["Grapeshot"]


def test_headings_are_case_insensitive() -> None:
    s = parse_sections("COMMANDER\n1 Hope of Ghirapur\nsideboard\n1 Grapeshot\n")
    assert [e.name for e in s.commander] == ["Hope of Ghirapur"]
    assert [e.name for e in s.sideboard] == ["Grapeshot"]


def test_deck_and_mainboard_are_both_main() -> None:
    """Arena writes `Deck`, Archidekt writes `Mainboard`."""
    assert [e.name for e in parse_sections("Deck\n1 Sol Ring\n").main] == ["Sol Ring"]
    assert [e.name for e in parse_sections("Mainboard\n1 Sol Ring\n").main] == ["Sol Ring"]


def test_companion_is_its_own_bucket() -> None:
    """D19.10: a companion is tracked in its own right, not as sideboard.

    It also lands in `main`, because it is a card the deck contains.
    """
    s = parse_sections("Companion\n1 Lurrus of the Dream-Den\nDeck\n1 Sol Ring\n")
    assert [e.name for e in s.companion] == ["Lurrus of the Dream-Den"]
    assert s.sideboard == []
    assert [e.name for e in s.main] == ["Lurrus of the Dream-Den", "Sol Ring"]


def test_maybeboard_and_tokens_are_dropped() -> None:
    """Neither is part of the deck — dropping them is semantics, not loss."""
    s = parse_sections(
        "Deck\n1 Sol Ring\nMaybeboard\n1 Mana Crypt\nTokens\n1 Treasure\n"
    )
    assert [e.name for e in s.main] == ["Sol Ring"]
    assert s.sideboard == []
    assert s.commander == []


# ---- the card-vs-heading boundary ----


def test_unlisted_heading_shaped_line_stays_a_card() -> None:
    """A bare card name has the same shape as a heading.

    Only the fixed allowlist is treated as a heading, so a quantity-less
    `Sol Ring` — legal input per §6.7 — is still a card.
    """
    s = parse_sections("Sol Ring\nLightning Bolt\n")
    assert [e.name for e in s.main] == ["Sol Ring", "Lightning Bolt"]


def test_heading_word_with_a_quantity_is_a_card() -> None:
    """`1 Sideboard` is a quantity line, so it can never be a heading."""
    s = parse_sections("1 Sideboard\n1 Sol Ring\n")
    assert [e.name for e in s.main] == ["Sideboard", "Sol Ring"]
    assert s.sideboard == []


def test_comments_and_blank_lines_still_drop() -> None:
    s = parse_sections("# a note\n\n1 Sol Ring\n// another\nSideboard\n1 Grapeshot\n")
    assert [e.name for e in s.main] == ["Sol Ring"]
    assert [e.name for e in s.sideboard] == ["Grapeshot"]


def test_set_and_collector_annotations_still_peel() -> None:
    """Bug.3 tail-stripping is inherited, not reimplemented."""
    s = parse_sections("Deck\n1 Sol Ring (C21) 263 *F*\n")
    assert [e.name for e in s.main] == ["Sol Ring"]


# ---- the Arena `About` preamble ----


def test_about_block_yields_the_deck_name_and_drops_the_rest() -> None:
    s = parse_sections("About\nName Big Boys\n\nCommander\n1 Radagast of Rhosgobel\n")
    assert s.name == "Big Boys"
    assert [e.name for e in s.commander] == ["Radagast of Rhosgobel"]
    assert [e.name for e in s.main] == ["Radagast of Rhosgobel"]


def test_name_line_outside_an_about_block_is_a_card() -> None:
    """`Name …` is Arena preamble syntax, meaningless anywhere else."""
    s = parse_sections("Deck\n1 Name Sticker Goblin\n")
    assert [e.name for e in s.main] == ["Name Sticker Goblin"]


# ---- real exports ----

# (path, deck name, commander, companion, main, sideboard) as names/cards.
#
# D19.10 re-measured these off the files, not by arithmetic. `main` now
# INCLUDES the commander, so the two files carrying a Commander section
# moved: archidekt 99/99 -> 100/100 and arena 50/87 -> 51/88. The Deckbox
# and Moxfield files have no Commander section and are unchanged, which is
# itself the control showing the change is scoped to what it should be.
_FIXTURES = [
    ("archidekt/colorless_arsenal.txt", None, (1, 1), (0, 0), (100, 100), (0, 0)),
    ("deckbox/iroh-gift.txt", None, (0, 0), (0, 0), (81, 81), (8, 19)),
    ("moxfield/big-boys-plain.txt", None, (0, 0), (0, 0), (77, 114), (0, 0)),
    ("moxfield/big-boys-moxfield.txt", None, (0, 0), (0, 0), (77, 114), (0, 0)),
    ("moxfield/big-boys-mtgo.txt", None, (0, 0), (0, 0), (77, 114), (0, 0)),
    ("moxfield/big-boys-20260817-200848.txt", None, (0, 0), (0, 0), (77, 114), (0, 0)),
    ("moxfield/big-boys-arena.txt", "Big Boys", (1, 1), (0, 0), (51, 88), (0, 0)),
]


@pytest.mark.parametrize(("rel", "name", "cmd", "comp", "main", "side"), _FIXTURES)
def test_real_export_sections(
    rel: str,
    name: str | None,
    cmd: tuple[int, int],
    comp: tuple[int, int],
    main: tuple[int, int],
    side: tuple[int, int],
) -> None:
    s = parse_sections((EXPORTS / rel).read_text())
    assert s.name == name
    assert (len(s.commander), _cards(s.commander)) == cmd
    assert (len(s.companion), _cards(s.companion)) == comp
    assert (len(s.main), _cards(s.main)) == main
    assert (len(s.sideboard), _cards(s.sideboard)) == side


def test_deckbox_txt_and_csv_agree_once_the_colon_is_handled() -> None:
    """The txt export is the CSV's `Section` column in prose form.

    81 main + 19 sideboard = 100 cards on both sides. Before the colon
    rule the txt read as 101, the extra "card" being the `Sideboard:`
    line itself — the exact class of guess the export README warns about.
    """
    s = parse_sections((EXPORTS / "deckbox" / "iroh-gift.txt").read_text())
    assert _cards(s.main) == 81
    assert _cards(s.sideboard) == 19
    assert _cards(s.main) + _cards(s.sideboard) == 100


def test_moxfield_variants_of_one_deck_are_not_assumed_equal() -> None:
    """The Arena export is a strictly smaller deck — pin the gap, don't hide it."""
    plain = parse_sections((EXPORTS / "moxfield" / "big-boys-plain.txt").read_text())
    arena = parse_sections((EXPORTS / "moxfield" / "big-boys-arena.txt").read_text())
    assert _cards(plain.main) == 114
    # `main` already includes the commander (D19.10), so adding the
    # commander list back in would double-count it.
    assert _cards(arena.main) == 88


def test_returns_a_frozen_dataclass() -> None:
    s = parse_sections("1 Sol Ring\n")
    assert isinstance(s, DeckSections)
    with pytest.raises(AttributeError):
        s.name = "nope"  # type: ignore[misc]


def test_sections_are_not_a_partition() -> None:
    """D19.10 footgun, pinned so nobody "fixes" it back.

    Commander and companion appear in `main` AS WELL AS their own list, so
    summing every bucket double-counts them. `main + sideboard` is the
    whole deck; anything adding `commander` on top is wrong.
    """
    s = parse_sections(
        "Commander\n1 Hope of Ghirapur\n"
        "Companion\n1 Lurrus of the Dream-Den\n"
        "Deck\n38 Forest\n"
        "Sideboard\n4 Grapeshot\n"
    )
    assert _cards(s.main) + _cards(s.sideboard) == 44
    naive = _cards(s.commander) + _cards(s.companion) + _cards(s.main) + _cards(s.sideboard)
    assert naive == 46, "commander + companion counted twice, as documented"


def test_main_preserves_source_order() -> None:
    """Commander/companion sit where the file put them, not bolted on top."""
    s = parse_sections(
        "Deck\n1 Sol Ring\nCommander\n1 Hope of Ghirapur\nDeck\n1 Mana Crypt\n"
    )
    assert [e.name for e in s.main] == ["Sol Ring", "Hope of Ghirapur", "Mana Crypt"]


def test_companion_and_commander_are_independent_buckets() -> None:
    s = parse_sections(
        "Commander\n1 Hope of Ghirapur\nCompanion\n1 Lurrus of the Dream-Den\n"
    )
    assert [e.name for e in s.commander] == ["Hope of Ghirapur"]
    assert [e.name for e in s.companion] == ["Lurrus of the Dream-Den"]
    assert [e.name for e in s.main] == ["Hope of Ghirapur", "Lurrus of the Dream-Den"]
