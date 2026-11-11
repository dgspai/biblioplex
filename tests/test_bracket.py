"""Pure-function tests for :mod:`biblioplex.decklist.bracket` (D14.4, D15.25).

Bracket assignment is a small ladder. Each test constructs a synthetic
signal set (name-based `game_changers` from the curated GAME_CHANGERS
frozenset + tag-based counts for the other 4 signals as of D15.25) that
trips exactly the signals for the target bracket.
"""

from __future__ import annotations

from biblioplex.decklist.bracket import (
    GAME_CHANGERS,
    SIGNAL_TAG_SLUGS,
    estimate_bracket,
    to_payload,
)


def _padded(names: list[str], size: int = 100) -> list[str]:
    """Pad a name list up to `size` with filler basics (no signals)."""
    return names + ["forest"] * (size - len(names))


# D15.25 — helper factories for the tag-based signal counts. Tests
# construct signal maps directly rather than injecting cards whose tags
# would be looked up in the DB (the router does that; the pure fn just
# reads pre-computed counts).
def _tag_counts(
    fast_mana: int = 0,
    mld: int = 0,
    tutors: int = 0,
    extra_turns: int = 0,
) -> dict[str, int]:
    return {
        "fast_mana": fast_mana,
        "mld": mld,
        "tutors": tutors,
        "extra_turns": extra_turns,
    }


def test_bracket_1_no_signals() -> None:
    """Empty deck (or filler-only) → Bracket 1 (Exhibition)."""
    e = estimate_bracket(_padded([]), combos=0)
    assert e.bracket == 1
    assert e.signals["game_changers"] == 0
    assert e.signals["combos"] == 0
    assert "no bracket-shifting signals" in e.notes[0]


def test_bracket_2_single_tutor() -> None:
    """One any-target tutor → Bracket 2 (Core)."""
    e = estimate_bracket(
        _padded([]), combos=0, tag_signal_counts=_tag_counts(tutors=1)
    )
    assert e.bracket == 2
    assert e.signals["tutors"] == 1


def test_bracket_2_single_fast_mana() -> None:
    """Sol Ring alone → Bracket 2 (Core)."""
    e = estimate_bracket(
        _padded([]), combos=0, tag_signal_counts=_tag_counts(fast_mana=1)
    )
    assert e.bracket == 2
    assert e.signals["fast_mana"] == 1


def test_bracket_3_game_changer() -> None:
    """One game-changer card lifts to Bracket 3 (Upgraded)."""
    e = estimate_bracket(_padded(["rhystic study"]), combos=0)
    assert e.bracket == 3
    assert e.signals["game_changers"] >= 1


def test_bracket_3_extra_turn() -> None:
    """Any extra-turn spell → Bracket 3."""
    e = estimate_bracket(
        _padded([]), combos=0, tag_signal_counts=_tag_counts(extra_turns=1)
    )
    assert e.bracket == 3


def test_bracket_3_two_fast_mana() -> None:
    """Two fast-mana pieces (still no combos / game changers) → Bracket 3."""
    e = estimate_bracket(
        _padded([]), combos=0, tag_signal_counts=_tag_counts(fast_mana=2)
    )
    assert e.bracket == 3


def test_bracket_3_three_tutors() -> None:
    """Three tutors sans game-changer content → Bracket 3."""
    e = estimate_bracket(
        _padded([]), combos=0, tag_signal_counts=_tag_counts(tutors=3)
    )
    assert e.bracket == 3


def test_bracket_4_one_combo() -> None:
    """One combo present → Bracket 4 (Optimized) immediately."""
    e = estimate_bracket(_padded([]), combos=1)
    assert e.bracket == 4
    assert "1 combo" in e.notes[0]


def test_bracket_4_mld() -> None:
    """MLD floor: even one mass land destruction spell → Bracket 4."""
    e = estimate_bracket(
        _padded([]), combos=0, tag_signal_counts=_tag_counts(mld=1)
    )
    assert e.bracket == 4


def test_bracket_4_four_game_changers() -> None:
    """Four game-changers → Bracket 4."""
    e = estimate_bracket(
        _padded([
            "rhystic study",
            "smothering tithe",
            "mana crypt",
            "cyclonic rift",
        ]),
        combos=0,
    )
    assert e.bracket == 4


def test_bracket_5_two_combos() -> None:
    """Two combos → Bracket 5 (cEDH), regardless of other signals."""
    e = estimate_bracket(_padded([]), combos=2)
    assert e.bracket == 5


def test_bracket_5_combo_with_fast_mana() -> None:
    """Combo + heavy fast mana → Bracket 5."""
    e = estimate_bracket(
        _padded([]), combos=1, tag_signal_counts=_tag_counts(fast_mana=3)
    )
    assert e.bracket == 5


def test_matched_game_changer_names_returned() -> None:
    """The response surfaces WHICH game changers matched (for UI)."""
    e = estimate_bracket(
        _padded(["rhystic study", "smothering tithe"]), combos=0
    )
    assert "rhystic study" in e.game_changer_names
    assert "smothering tithe" in e.game_changer_names


def test_game_changer_names_preserve_original_casing() -> None:
    """Bug.8: when caller supplies `original_by_lower`, matched names
    surface in their original (proper) case rather than lowercased."""
    e = estimate_bracket(
        _padded(["cyclonic rift", "rhystic study"]),
        combos=0,
        original_by_lower={
            "cyclonic rift": "Cyclonic Rift",
            "rhystic study": "Rhystic Study",
        },
    )
    assert "Cyclonic Rift" in e.game_changer_names
    assert "Rhystic Study" in e.game_changer_names
    assert "cyclonic rift" not in e.game_changer_names


def test_to_payload_shape() -> None:
    """Wire-response shape carries object/bracket/signals/game_changers/notes."""
    e = estimate_bracket(
        _padded([]), combos=0, tag_signal_counts=_tag_counts(fast_mana=1)
    )
    payload = to_payload(e)
    assert payload["object"] == "deck_bracket"
    assert payload["bracket"] == 2
    assert "signals" in payload
    assert "game_changers" in payload
    assert "notes" in payload


def test_game_changers_catalog_non_empty() -> None:
    """Regression pin: the curated GAME_CHANGERS list isn't accidentally emptied."""
    assert len(GAME_CHANGERS) >= 20


# ---- D15.25: tag-slug migration ----


def test_signal_tag_slugs_covers_the_four_tag_based_signals() -> None:
    """SIGNAL_TAG_SLUGS names one Tagger slug per tag-based signal."""
    assert set(SIGNAL_TAG_SLUGS.keys()) == {
        "fast_mana",
        "mld",
        "tutors",
        "extra_turns",
    }
    # Sanity check the slugs — non-empty ASCII kebab-case.
    for slug in SIGNAL_TAG_SLUGS.values():
        assert slug
        assert slug.islower()


def test_tag_signal_counts_none_defaults_to_zero() -> None:
    """Missing tag_signal_counts leaves the 4 tag-based signals at 0."""
    e = estimate_bracket(_padded([]), combos=0)
    assert e.signals["fast_mana"] == 0
    assert e.signals["mld"] == 0
    assert e.signals["tutors"] == 0
    assert e.signals["extra_turns"] == 0


def test_tag_signal_counts_missing_key_defaults_to_zero() -> None:
    """Partial map: only `tutors` supplied — other signals fall through."""
    e = estimate_bracket(
        _padded([]), combos=0, tag_signal_counts={"tutors": 2}
    )
    assert e.signals["tutors"] == 2
    assert e.signals["fast_mana"] == 0


# ---- D15.18: combos_enabled note ----


def test_notes_combos_disabled() -> None:
    """D15.18: combos_enabled=False on a ≤3 bracket appends the disabled note.

    A user submitting a genuine cEDH decklist on a deployment where
    `COMBOS_SOURCE_URL` is empty would otherwise see bracket ≤3 with no
    hint that combos is a deployment-disabled signal, not zero.
    """
    e = estimate_bracket(
        _padded([]),
        combos=0,
        combos_enabled=False,
        tag_signal_counts=_tag_counts(fast_mana=1),
    )
    assert e.bracket == 2
    assert any("combos signal disabled" in n for n in e.notes)


def test_notes_combos_disabled_skipped_above_bracket_3() -> None:
    """D15.18: bracket ≥4 (via MLD/GC path) does not get the disabled note."""
    e = estimate_bracket(
        _padded([]),
        combos=0,
        combos_enabled=False,
        tag_signal_counts=_tag_counts(mld=1),
    )
    assert e.bracket == 4
    assert not any("combos signal disabled" in n for n in e.notes)


def test_notes_combos_enabled_no_disabled_note() -> None:
    """D15.18: default combos_enabled=True never emits the disabled note."""
    e = estimate_bracket(
        _padded([]), combos=0, tag_signal_counts=_tag_counts(fast_mana=1)
    )
    assert not any("combos signal disabled" in n for n in e.notes)
