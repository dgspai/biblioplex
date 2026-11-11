"""Tests for multi-face flatten (D1.4, tech_spec.md §3.3)."""

from __future__ import annotations

from typing import Any

from biblioplex.ingest.flatten import flatten_card


def _single_face_normal() -> dict[str, Any]:
    return {
        "id": "x",
        "oracle_id": "ox",
        "name": "Llanowar Elves",
        "type_line": "Creature — Elf Druid",
        "oracle_text": "{T}: Add {G}.",
        "mana_cost": "{G}",
        "colors": ["G"],
        "layout": "normal",
    }


def _transform_card() -> dict[str, Any]:
    """Transform / TDFC: Scryfall populates top-level fields from front face only."""
    return {
        "id": "t",
        "oracle_id": "ot",
        "name": "Delver of Secrets // Insectile Aberration",
        "type_line": "Creature — Human Wizard",
        "oracle_text": "At the beginning of your upkeep...",
        "mana_cost": "{U}",
        "colors": ["U"],
        "layout": "transform",
        "card_faces": [
            {
                "name": "Delver of Secrets",
                "type_line": "Creature — Human Wizard",
                "oracle_text": "At the beginning of your upkeep...",
                "mana_cost": "{U}",
                "colors": ["U"],
            },
            {
                "name": "Insectile Aberration",
                "type_line": "Creature — Human Insect",
                "oracle_text": "Flying",
                "mana_cost": "",
                "colors": ["U"],
            },
        ],
    }


def _split_card() -> dict[str, Any]:
    return {
        "id": "s",
        "oracle_id": "os",
        "name": "Fire // Ice",
        "type_line": "Instant // Instant",
        "oracle_text": None,
        "mana_cost": None,
        "colors": None,
        "layout": "split",
        "card_faces": [
            {
                "name": "Fire",
                "type_line": "Instant",
                "oracle_text": "Fire deals 2 damage divided as you choose.",
                "mana_cost": "{1}{R}",
                "colors": ["R"],
            },
            {
                "name": "Ice",
                "type_line": "Instant",
                "oracle_text": "Tap target permanent. Draw a card.",
                "mana_cost": "{1}{U}",
                "colors": ["U"],
            },
        ],
    }


def _modal_dfc() -> dict[str, Any]:
    return {
        "id": "m",
        "oracle_id": "om",
        "name": "Valki, God of Lies // Tibalt, Cosmic Impostor",
        "type_line": "Legendary Creature — God",
        "oracle_text": "When Valki enters...",
        "mana_cost": "{1}{B}",
        "colors": ["B"],
        "layout": "modal_dfc",
        "card_faces": [
            {
                "name": "Valki, God of Lies",
                "type_line": "Legendary Creature — God",
                "oracle_text": "When Valki enters...",
                "mana_cost": "{1}{B}",
                "colors": ["B"],
            },
            {
                "name": "Tibalt, Cosmic Impostor",
                "type_line": "Legendary Planeswalker — Tibalt",
                "oracle_text": "Play lands and cast spells from exile.",
                "mana_cost": "{7}{R}",
                "colors": ["R"],
            },
        ],
    }


def _adventure_card() -> dict[str, Any]:
    return {
        "id": "a",
        "oracle_id": "oa",
        "name": "Brazen Borrower // Petty Theft",
        "type_line": "Creature — Faerie Rogue",
        "oracle_text": "Flash. Flying.",
        "mana_cost": "{1}{U}{U}",
        "colors": ["U"],
        "layout": "adventure",
        "card_faces": [
            {
                "name": "Brazen Borrower",
                "type_line": "Creature — Faerie Rogue",
                "oracle_text": "Flash. Flying.",
                "mana_cost": "{1}{U}{U}",
                "colors": ["U"],
            },
            {
                "name": "Petty Theft",
                "type_line": "Instant — Adventure",
                "oracle_text": "Return target nonland permanent...",
                "mana_cost": "{1}{U}",
                "colors": ["U"],
            },
        ],
    }


def _meld_card() -> dict[str, Any]:
    """Meld parents are normal until melded; the meld result has card_faces."""
    return {
        "id": "ml",
        "oracle_id": "oml",
        "name": "Bruna, the Fading Light",
        "type_line": "Legendary Creature — Angel Horror",
        "oracle_text": "When you cast Bruna...",
        "mana_cost": "{5}{W}{W}",
        "colors": ["W"],
        "layout": "meld",
    }


def test_flatten_single_face_no_faces() -> None:
    """Card with no card_faces: search fields equal the top-level fields."""
    card = _single_face_normal()
    out = flatten_card(card)
    assert out["name_search"] == "Llanowar Elves"
    assert out["type_search"] == "Creature — Elf Druid"
    assert out["oracle_search"] == "{T}: Add {G}."
    assert out["mana_cost_search"] == "{G}"
    assert out["colors_eff"] == ["G"]


def test_flatten_transform_includes_back_face_text() -> None:
    """Transform: search fields concatenate top-level + each face."""
    card = _transform_card()
    out = flatten_card(card)
    # Top-level name + both face names, space-joined.
    assert "Delver of Secrets // Insectile Aberration" in out["name_search"]
    assert "Insectile Aberration" in out["name_search"]
    # Back-face oracle text reachable via oracle_search.
    assert "Flying" in out["oracle_search"]
    # Mana cost from front carried, empty face mana_cost coalesced to ''.
    assert "{U}" in out["mana_cost_search"]
    # colors comes from top-level (Scryfall populates it for transform).
    assert out["colors_eff"] == ["U"]


def test_flatten_split_aggregates_per_face_colors() -> None:
    """Split with null top-level colors: colors_eff = union of face colors."""
    card = _split_card()
    out = flatten_card(card)
    # Both halves searchable via type / oracle / mana fields.
    assert "Fire" in out["name_search"] and "Ice" in out["name_search"]
    assert "Instant" in out["type_search"]
    assert "draw a card" in out["oracle_search"].lower()
    assert "{1}{R}" in out["mana_cost_search"]
    assert "{1}{U}" in out["mana_cost_search"]
    # colors_eff is the deduped UNION of face colors.
    assert sorted(out["colors_eff"]) == ["R", "U"]


def test_flatten_modal_dfc_back_face_searchable() -> None:
    """MDFC: back-face type (planeswalker) reachable via type_search."""
    card = _modal_dfc()
    out = flatten_card(card)
    assert "Planeswalker" in out["type_search"]
    assert "Tibalt" in out["name_search"]
    # Top-level colors set by Scryfall to {B} (front only); colors_eff uses it.
    assert out["colors_eff"] == ["B"]


def test_flatten_adventure_back_face_searchable() -> None:
    """Adventure: petty-theft text reachable via oracle_search."""
    card = _adventure_card()
    out = flatten_card(card)
    assert "Petty Theft" in out["name_search"]
    assert "nonland permanent" in out["oracle_search"]


def test_flatten_meld_no_faces_works() -> None:
    """Meld parent has no card_faces array — degenerate to single-face behavior."""
    card = _meld_card()
    out = flatten_card(card)
    assert out["name_search"] == "Bruna, the Fading Light"
    assert "Angel" in out["type_search"]
    assert out["colors_eff"] == ["W"]


def test_flatten_coalesces_nulls_to_empty_string() -> None:
    """Null top-level fields → '' in the joined search column, not literal None."""
    card = {
        "id": "n",
        "oracle_id": "on",
        "name": "Nameless",
        "type_line": None,
        "oracle_text": None,
        "mana_cost": None,
        "colors": None,
        "layout": "normal",
    }
    out = flatten_card(card)
    # Type/oracle/mana stay empty strings (not 'None' substrings).
    assert "None" not in out["type_search"]
    assert "None" not in out["oracle_search"]
    assert "None" not in out["mana_cost_search"]
    # NOT NULL contract — value is a string, not None.
    assert isinstance(out["type_search"], str)
    assert isinstance(out["oracle_search"], str)
    assert isinstance(out["mana_cost_search"], str)
    # colors null with no faces → empty list.
    assert out["colors_eff"] == []


def test_flatten_colors_eff_dedupes_face_union() -> None:
    """Multiple faces sharing colors yield a deduped union, not duplicates."""
    card = {
        "id": "d",
        "oracle_id": "od",
        "name": "Dup // Dup",
        "type_line": None,
        "oracle_text": None,
        "mana_cost": None,
        "colors": None,
        "layout": "split",
        "card_faces": [
            {"name": "A", "colors": ["U", "B"]},
            {"name": "B", "colors": ["B", "R"]},
        ],
    }
    out = flatten_card(card)
    assert sorted(out["colors_eff"]) == ["B", "R", "U"]


# ---- D10.2: colors_eff WUBRG-sorted (set-equality contract for c=COLORS) ----


def test_flatten_colors_eff_sorts_top_level_wubrg() -> None:
    """Top-level `colors=["G","U","R"]` must come out WUBRG-sorted as `["U","R","G"]`.

    Scryfall claims to ship `colors` in WUBRG order but multi-face / back-only
    colorings have been observed out-of-order; the cost of re-sorting is
    negligible. Source of truth for `c=COLORS` set equality (§5.4): the
    compiler sorts the query side, so ingest must sort the column side too.
    """
    card = {
        "id": "t1",
        "oracle_id": "ot1",
        "name": "Topgur",
        "type_line": "Creature",
        "oracle_text": None,
        "mana_cost": None,
        "colors": ["G", "U", "R"],
        "layout": "normal",
    }
    out = flatten_card(card)
    assert out["colors_eff"] == ["U", "R", "G"]


def test_flatten_colors_eff_sorts_face_union_wubrg() -> None:
    """Face union: face[0]=R, face[1]=U,W → WUBRG-sorted = ['W','U','R']."""
    card = {
        "id": "t2",
        "oracle_id": "ot2",
        "name": "FaceR // FaceUW",
        "type_line": None,
        "oracle_text": None,
        "mana_cost": None,
        "colors": None,
        "layout": "split",
        "card_faces": [
            {"name": "FaceR", "colors": ["R"]},
            {"name": "FaceUW", "colors": ["U", "W"]},
        ],
    }
    out = flatten_card(card)
    assert out["colors_eff"] == ["W", "U", "R"]


def test_flatten_colors_eff_empty_returns_empty_list() -> None:
    """Top-level null and no faces → []. NOT NULL contract preserved."""
    card = {
        "id": "t3",
        "oracle_id": "ot3",
        "name": "Colorless",
        "type_line": None,
        "oracle_text": None,
        "mana_cost": None,
        "colors": None,
        "layout": "normal",
    }
    out = flatten_card(card)
    assert out["colors_eff"] == []


def test_flatten_colors_eff_dedupes_across_faces_sorted() -> None:
    """Duplicates across faces collapse to one WUBRG-sorted occurrence."""
    card = {
        "id": "t4",
        "oracle_id": "ot4",
        "name": "Dup // Dup",
        "type_line": None,
        "oracle_text": None,
        "mana_cost": None,
        "colors": None,
        "layout": "split",
        "card_faces": [
            {"name": "A", "colors": ["G", "U", "B"]},
            {"name": "B", "colors": ["B", "G", "W"]},
        ],
    }
    out = flatten_card(card)
    assert out["colors_eff"] == ["W", "U", "B", "G"]


def test_flatten_colors_eff_drops_unknown_letters() -> None:
    """Defensive: letters outside WUBRG are filtered out before sort."""
    card = {
        "id": "t5",
        "oracle_id": "ot5",
        "name": "Garbage",
        "type_line": None,
        "oracle_text": None,
        "mana_cost": None,
        "colors": ["G", "X", "W"],
        "layout": "normal",
    }
    out = flatten_card(card)
    assert out["colors_eff"] == ["W", "G"]


def test_flatten_preserves_original_fields() -> None:
    """Flatten returns a copy that contains all original fields + the new ones."""
    card = _single_face_normal()
    out = flatten_card(card)
    # Original keys all present.
    for key in card:
        assert key in out
    # New search keys added.
    for key in ("name_search", "type_search", "oracle_search", "mana_cost_search", "colors_eff"):
        assert key in out
