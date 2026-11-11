"""D12.2 — decklist parser (`POST /decks/analyze` §6.7)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from biblioplex.decklist.parser import DeckEntry, parse_decklist


def test_single_entry_no_qty() -> None:
    assert parse_decklist("Sol Ring") == [DeckEntry(qty=1, name="Sol Ring")]


def test_single_entry_with_qty() -> None:
    assert parse_decklist("4 Lightning Bolt") == [
        DeckEntry(qty=4, name="Lightning Bolt"),
    ]


def test_x_suffix_lowercase() -> None:
    assert parse_decklist("4x Lightning Bolt") == [
        DeckEntry(qty=4, name="Lightning Bolt"),
    ]


def test_x_suffix_uppercase() -> None:
    assert parse_decklist("1X Sol Ring") == [DeckEntry(qty=1, name="Sol Ring")]


def test_multiple_entries() -> None:
    text = "4 Lightning Bolt\n1 Sol Ring\n2 Counterspell"
    assert parse_decklist(text) == [
        DeckEntry(qty=4, name="Lightning Bolt"),
        DeckEntry(qty=1, name="Sol Ring"),
        DeckEntry(qty=2, name="Counterspell"),
    ]


def test_hash_comment_skipped() -> None:
    assert parse_decklist("# my deck\n1 Sol Ring") == [
        DeckEntry(qty=1, name="Sol Ring"),
    ]


def test_double_slash_comment_skipped() -> None:
    assert parse_decklist("// notes\n1 Sol Ring") == [
        DeckEntry(qty=1, name="Sol Ring"),
    ]


def test_blank_lines_skipped() -> None:
    assert parse_decklist("\n\n1 Sol Ring\n\n") == [
        DeckEntry(qty=1, name="Sol Ring"),
    ]


def test_whitespace_only_line_skipped() -> None:
    assert parse_decklist("   \n\t\n1 Sol Ring") == [
        DeckEntry(qty=1, name="Sol Ring"),
    ]


def test_empty_input_returns_empty_list() -> None:
    assert parse_decklist("") == []


def test_split_card_name_preserved() -> None:
    # `//` in the middle of a name is the split-card separator, not a comment.
    assert parse_decklist("4 Fire // Ice") == [DeckEntry(qty=4, name="Fire // Ice")]


def test_indented_comment_still_skipped() -> None:
    assert parse_decklist("   // notes\n1 Sol Ring") == [
        DeckEntry(qty=1, name="Sol Ring"),
    ]


def test_trailing_whitespace_trimmed() -> None:
    assert parse_decklist("4 Lightning Bolt   ") == [
        DeckEntry(qty=4, name="Lightning Bolt"),
    ]


def test_qty_zero_drops_line() -> None:
    assert parse_decklist("0 Mystic Reflection\n1 Sol Ring") == [
        DeckEntry(qty=1, name="Sol Ring"),
    ]


def test_qty_over_cap_clamped() -> None:
    [entry] = parse_decklist("9999 Sol Ring")
    assert entry == DeckEntry(qty=99, name="Sol Ring")


def test_tab_between_qty_and_name() -> None:
    assert parse_decklist("4\tLightning Bolt") == [
        DeckEntry(qty=4, name="Lightning Bolt"),
    ]


def test_section_header_flows_through_as_name() -> None:
    # `Sideboard` etc. aren't filtered — they end up unresolvable downstream.
    assert parse_decklist("Sideboard\n1 Sol Ring") == [
        DeckEntry(qty=1, name="Sideboard"),
        DeckEntry(qty=1, name="Sol Ring"),
    ]


@pytest.mark.parametrize(
    "raw,expected_name",
    [
        # Arena: "(SET) NUMBER" trailing annotation.
        ("1 Sol Ring (C21) 263", "Sol Ring"),
        # MTGO/tabletop: "[SET]" bracketed.
        ("1 Sol Ring [C21]", "Sol Ring"),
        # MTGA foil marker.
        ("1 Sol Ring *F*", "Sol Ring"),
        # Combined Arena set-code + collector + foil.
        ("1 Sol Ring (C21) 263 *F*", "Sol Ring"),
        # Split-card name preserved when suffix present.
        ("1 Fire // Ice (APC) 128", "Fire // Ice"),
        # Set code + collector no marker.
        ("2 Lightning Bolt (2X2) 117", "Lightning Bolt"),
        # Bug.36: collector numbers are NOT always plain digits. These four
        # shapes all appear in a real 62-deck Moxfield sync and every one of
        # them left the annotation glued to the name, so the card could only
        # ever be rescued by the fuzzy fallback in `resolve_names` — which
        # the derived `deck_cards` index does not use, so it counted them
        # unresolved and quietly understated coverage on 235 rows.
        ("1 Lightning Greaves (PLST) C19-217", "Lightning Greaves"),
        ("1 Uthros, Titanic Godcore (PEOE) 260p", "Uthros, Titanic Godcore"),
        ("1 Memory Lapse (HML) 32b", "Memory Lapse"),
        ("1 Arcane Adaptation (PXLN) 46s", "Arcane Adaptation"),
        # The star marks a special printing and rides on the collector token.
        ("1 Beast Whisperer (PGRN) 123\u2605 *F*", "Beast Whisperer"),
        # A non-numeric collector still peels when combined with a foil flag.
        ("1 Sol Ring (PLST) C18-222 *F*", "Sol Ring"),
    ],
)
def test_strips_arena_mtgo_annotations(raw: str, expected_name: str) -> None:
    """Bug.3: Arena/MTGO/MTGA export annotations must not fold into the name."""
    assert parse_decklist(raw) == [DeckEntry(qty=int(raw.split()[0]), name=expected_name)]


@pytest.mark.parametrize(
    "raw,expected_qty,expected_name",
    [
        # Bug.15: qty followed by `x` with NO space before the name.
        ("1xSol Ring", 1, "Sol Ring"),
        ("4xLightning Bolt", 4, "Lightning Bolt"),
        ("2XCounterspell", 2, "Counterspell"),
    ],
)
def test_bug15_no_space_after_x(raw: str, expected_qty: int, expected_name: str) -> None:
    """Bug.15: `4xSol Ring` (no space) parses as qty + name, not one name blob."""
    assert parse_decklist(raw) == [DeckEntry(qty=expected_qty, name=expected_name)]


@pytest.mark.parametrize(
    "raw,expected_qty,expected_name",
    [
        # Bug.28: bare `X`/`x` at the start of a name (space between qty and name)
        # must NOT be eaten as the qty marker — the character preceding `X` here
        # is whitespace, not a digit, so it's the first letter of the name.
        ("1 XA", 1, "XA"),
        ("1 Xantcha, Sleeper Agent", 1, "Xantcha, Sleeper Agent"),
        ("1 Xenagos, the Reveler", 1, "Xenagos, the Reveler"),
        ("2 xylem", 2, "xylem"),
        # No-qty forms — the whole line is just a name that happens to start
        # with an X. The (optional) qty group must fail cleanly.
        ("XA", 1, "XA"),
        ("Xantcha, Sleeper Agent", 1, "Xantcha, Sleeper Agent"),
    ],
)
def test_bug28_bare_x_prefix_is_name_not_marker(
    raw: str, expected_qty: int, expected_name: str
) -> None:
    """Bug.28: names beginning with a bare `X` must not lose the `X` to the qty marker."""
    assert parse_decklist(raw) == [DeckEntry(qty=expected_qty, name=expected_name)]


@pytest.mark.parametrize(
    "raw",
    [
        "1 XA",
        "1 Xantcha, Sleeper Agent",
        "4 Lightning Bolt",
        "1xSol Ring",
        "4xLightning Bolt",
        "2XCounterspell",
        "1X Sol Ring",
        "4x Lightning Bolt",
    ],
)
def test_bug28_round_trip_fixed_point(raw: str) -> None:
    """Bug.28: parse -> serialize -> parse must be a fixed point on these seeds.

    Pins the exact case Hypothesis's `test_idempotent_on_str_roundtrip` shrinks
    to (`"XA"`), plus every Bug.15 cubeware seed to prove the fix didn't
    regress the no-space-after-x path.
    """
    first = parse_decklist(raw)
    re_serialized = "\n".join(f"{e.qty} {e.name}" for e in first)
    assert parse_decklist(re_serialized) == first


@pytest.mark.parametrize(
    "raw,expected_name",
    [
        # Bug.14: Scryfall docs allow up to 6-char set codes. Prior `{2,5}`
        # would silently leave a 6-char annotation glued to the name.
        ("1 Sol Ring (ABCDEF) 12", "Sol Ring"),
        ("1 Sol Ring [ABCDEF]", "Sol Ring"),
        # Bug.36 changed the third case, deliberately: a PAREN set code with
        # no collector number no longer strips. Bug.14's subject is the 6-char
        # LENGTH, which the two cases above still cover in both bracket forms;
        # this case only ever encoded collector-optionality, and that is the
        # exact shape that collides with the real card `Hazmat Suit (Used)`.
        ("1 Sol Ring (ABCDEF)", "Sol Ring (ABCDEF)"),
    ],
)
def test_bug14_six_char_set_codes_stripped(raw: str, expected_name: str) -> None:
    """Bug.14: `_TAIL_ANNOTATION_RE` set-code group must accept 6-char codes."""
    assert parse_decklist(raw) == [DeckEntry(qty=1, name=expected_name)]


def test_bug36_bare_paren_set_code_no_longer_strips() -> None:
    """The priced cost of Bug.36, pinned so it is visible, not accidental.

    Requiring a collector number after a parenthesised set code is what
    saves `Hazmat Suit (Used)`. It costs the bare `(SET)` form, which no
    reference export in `data/example_deck_exports/` produces — measured,
    zero occurrences across all nine files — and which the fuzzy fallback in
    `resolve_names` would still rescue for `/decks/analyze` anyway.

    The failure mode is also the right way round: a missed strip leaves a
    visibly odd name in `unresolved`, while the old behaviour silently
    turned a real card into one that does not exist.
    """
    assert parse_decklist("1 Sol Ring (C21)")[0].name == "Sol Ring (C21)"
    # The bracket form is unaffected: no card name contains `[XXXX]`.
    assert parse_decklist("1 Sol Ring [C21]")[0].name == "Sol Ring"


@pytest.mark.parametrize("text", ["", "# comment only", "//\n//\n//", "   \n\n"])
def test_no_entries_when_only_noise(text: str) -> None:
    assert parse_decklist(text) == []


# --- Hypothesis property tests -----------------------------------------------


@given(st.text())
def test_idempotent_on_str_roundtrip(text: str) -> None:
    """Running the parser on its own re-serialized output yields the same entries."""
    first = parse_decklist(text)
    re_serialized = "\n".join(f"{e.qty} {e.name}" for e in first)
    assert parse_decklist(re_serialized) == first


@given(st.text())
def test_never_emits_blank_or_comment_names(text: str) -> None:
    for entry in parse_decklist(text):
        assert entry.name.strip() == entry.name
        assert entry.name != ""
        assert not entry.name.startswith(("#", "//"))


_NAME_ALPHA = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters=" ',-",
    ),
    min_size=1,
    max_size=30,
)


@given(st.integers(min_value=0, max_value=10000), _NAME_ALPHA)
def test_qty_always_clamped_to_range(qty: int, name: str) -> None:
    name = name.strip().lstrip("0123456789#/")
    if not name:
        return
    entries = parse_decklist(f"{qty} {name}")
    if qty == 0:
        assert entries == []
    else:
        [e] = entries
        assert 1 <= e.qty <= 99


# Bug.33 — a detached `x` multiplier must not eat a name that starts with
# "X ". `decklist/format.py` re-serializes as `f"{qty} {name}"`, so the
# parser has to survive its own output.
@pytest.mark.parametrize(
    "line, expected",
    [
        ("X 0", DeckEntry(qty=1, name="X 0")),
        ("1 X 0", DeckEntry(qty=1, name="X 0")),
        ("1 X :", DeckEntry(qty=1, name="X :")),
        ("1 X Sol Ring", DeckEntry(qty=1, name="X Sol Ring")),
        ("1 xfoo", DeckEntry(qty=1, name="xfoo")),
    ],
)
def test_bug33_detached_x_is_part_of_the_name(line: str, expected: DeckEntry) -> None:
    assert parse_decklist(line) == [expected]


@pytest.mark.parametrize(
    "line, expected",
    [
        ("4x Lightning Bolt", DeckEntry(qty=4, name="Lightning Bolt")),
        ("1X Sol Ring", DeckEntry(qty=1, name="Sol Ring")),
        ("1xSol Ring", DeckEntry(qty=1, name="Sol Ring")),
        ("4 Lightning Bolt", DeckEntry(qty=4, name="Lightning Bolt")),
    ],
)
def test_bug33_attached_multiplier_forms_still_parse(line: str, expected: DeckEntry) -> None:
    assert parse_decklist(line) == [expected]


def test_bug33_spaced_multiplier_is_the_documented_cost() -> None:
    """`4 x Bolt` (spaces both sides) is the price of a round-trip that holds
    for arbitrary input. Pinned so the trade-off is visible, not accidental."""
    assert parse_decklist("4 x Lightning Bolt") == [
        DeckEntry(qty=4, name="x Lightning Bolt")
    ]


# ---- D19.8: split-card normalisation moved into the parser ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 Fire//Ice", "Fire // Ice"),
        ("1 Fire // Ice", "Fire // Ice"),
        ("1 Fire //Ice", "Fire // Ice"),
        ("1 Fire// Ice", "Fire // Ice"),
        ("Fire//Ice", "Fire // Ice"),
        # Already-canonical spacing anywhere in the name means hands off, so
        # a partially-spaced name is left as-is rather than half-rewritten.
        ("1 Fire // Ice//Wind", "Fire // Ice//Wind"),
        # Tail annotations still peel first (Bug.3), then spacing is fixed.
        ("1 Fire//Ice (MH2) 290", "Fire // Ice"),
    ],
)
def test_split_card_spacing_is_normalised(raw: str, expected: str) -> None:
    """`cards.name` spells split cards `Fire // Ice`; exports often do not.

    D19.8 moved this out of the two per-source copies
    (`inventory/deckbox.py`, `decklist/importers.py`) and into the parser,
    so every decklist source gets it — including plain pasted text, which
    never had it before.
    """
    assert parse_decklist(raw)[0].name == expected


def test_split_card_normalisation_is_idempotent() -> None:
    """Required by the format -> parse round-trip the /decks/* responses do."""
    once = parse_decklist("1 Fire//Ice")
    twice = parse_decklist("\n".join(f"{e.qty} {e.name}" for e in once))
    assert once == twice


def test_normalize_card_name_is_the_single_shared_definition() -> None:
    """The inventory CSV path never touches `parse_decklist`.

    `parse_deckbox_csv` builds ParsedDeckboxItems and resolves them
    against `cards` directly, so it cannot inherit the parser's
    normalisation implicitly — it has to import the same callable. Pinning
    identity here is what stops the two from drifting apart again.
    """
    from biblioplex.decklist.parser import normalize_card_name
    from biblioplex.inventory import deckbox as inv_deckbox

    assert inv_deckbox.normalize_card_name is normalize_card_name
    assert normalize_card_name("Fire//Ice") == "Fire // Ice"
    assert normalize_card_name("  Sol Ring  ") == "Sol Ring"


# ---- Bug.36: a parenthesised token is only an annotation if a collector
# number follows it. `Hazmat Suit (Used)` is a REAL card name, and the only
# one in the corpus shaped like a set code (verified against the live
# 34,593-card table). Every annotation in every reference export carries a
# collector number, so requiring one costs nothing and stops the parser
# eating part of a legitimate name. The bracket form keeps its optional
# collector because NO card name contains `[XXXX]` — also verified.


@pytest.mark.parametrize(
    "raw,expected_name",
    [
        ("1 Hazmat Suit (Used)", "Hazmat Suit (Used)"),
        ("4 Hazmat Suit (Used) *F*", "Hazmat Suit (Used)"),
        # …but a real annotation after it still peels, name intact.
        ("1 Hazmat Suit (Used) (UST) 54", "Hazmat Suit (Used)"),
    ],
)
def test_parenthesised_name_parts_survive(raw: str, expected_name: str) -> None:
    """Bug.36: a bare `(Word)` with no collector number is part of the name."""
    assert parse_decklist(raw)[0].name == expected_name


def test_bracket_form_still_strips_without_a_collector() -> None:
    """The asymmetry is deliberate: no card name contains `[SET]`."""
    assert parse_decklist("1 Sol Ring [C21]")[0].name == "Sol Ring"
