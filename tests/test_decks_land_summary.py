"""Deck analysis: manabase summary built from each land's profile."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from biblioplex.decklist.resolve import ResolvedDeckRow
from biblioplex.routers.decks._common import _land_summary, _tally


def _card(name: str, qty: int, profile: dict | None) -> dict:
    return {"name": name, "qty": qty, "is_land": profile is not None, "land_profile": profile}


def test_summary_counts_by_quantity() -> None:
    cards = [
        _card("Forest", 10, {"colors": "mono", "etb": "untapped", "utility": []}),
        _card("Bojuka Bog", 1, {"colors": "mono", "etb": "tapped",
                                "utility": ["targeted_disrupt"]}),
        _card("Watery Grave", 1, {"colors": "dual", "etb": "conditional", "utility": []}),
        _card("Sol Ring", 1, None),
    ]
    s = _land_summary(cards)
    assert s["etb"] == {"untapped": 10, "tapped": 1, "conditional": 1}
    assert s["colors"]["mono"] == 11 and s["colors"]["dual"] == 1
    assert s["utility"] == [{"name": "Bojuka Bog", "qty": 1, "utility": ["targeted_disrupt"]}]


def test_unprofiled_lands_are_counted_as_unknown() -> None:
    s = _land_summary([{"name": "Old Land", "qty": 2, "is_land": True, "land_profile": None}])
    assert s["unknown"] == 2


def test_tally_carries_land_profile_into_cards() -> None:
    profile = {"colors": "colorless", "etb": "untapped", "utility": []}
    row = SimpleNamespace(
        id=uuid4(), name="Wastes", type_line="Basic Land", layout="normal",
        card_faces=None, predicted_labels=[], cmc=0, mana_cost="", land_profile=profile,
    )
    entry = SimpleNamespace(name="Wastes", qty=3)
    t = _tally([entry], [ResolvedDeckRow(input_name="Wastes", row=row)], [])
    assert t.cards_out[0]["land_profile"] == profile


def _p(colors: str, etb: str = "untapped", utility: list | None = None) -> dict:
    return {"colors": colors, "etb": etb, "utility": utility or []}


def test_lands_fall_into_mana_base_categories() -> None:
    cards = [
        _card("Forest", 8, _p("mono")),
        _card("Snow-Covered Island", 2, _p("mono")),
        _card("Wastes", 1, _p("colorless")),
        _card("Polluted Delta", 1, _p("none", utility=["fetch"])),
        _card("Myriad Landscape", 1, _p("colorless", "tapped", ["ramp", "fetch"])),
        _card("Watery Grave", 1, _p("dual", "conditional")),
        _card("Zagoth Triome", 1, _p("tri", "tapped")),
        _card("Command Tower", 1, _p("five")),
        _card("Bojuka Bog", 1, _p("mono", "tapped", ["targeted_disrupt"])),
        _card("Reliquary Tower", 1, _p("colorless")),
    ]
    cat = _land_summary(cards)["categories"]
    assert cat == {
        "basic": 11, "fetch": 2, "dual": 1, "tri": 1, "rainbow": 1,
        "utility": 1, "colorless": 1,
    }


def test_plain_taplands_skip_ones_that_pay_for_it() -> None:
    cards = [
        _card("Dimir Guildgate", 3, _p("dual", "tapped")),
        _card("Zagoth Triome", 1, _p("tri", "tapped")),       # tri: worth it
        _card("Path of Ancestry", 1, _p("five", "tapped")),   # rainbow: worth it
        _card("Bojuka Bog", 1, _p("mono", "tapped", ["targeted_disrupt"])),
        _card("Watery Grave", 1, _p("dual", "conditional")),
    ]
    assert _land_summary(cards)["taplands"] == 3


def test_modal_spell_lands_count_as_part_of_a_land() -> None:
    cards = [
        {"name": "Bala Ged Recovery", "qty": 1, "is_land": False,
         "land_profile": _p("mono", "tapped")},
        {"name": "Shatterskull Smashing", "qty": 1, "is_land": False,
         "land_profile": _p("mono", "conditional")},
        {"name": "Sol Ring", "qty": 1, "is_land": False},
    ]
    assert _land_summary(cards)["mdfc"] == 1.12  # 0.38 + 0.74


def test_tally_keeps_the_land_face_profile_of_modal_spells() -> None:
    profile = _p("mono", "tapped")
    row = SimpleNamespace(
        id=uuid4(), name="Bala Ged Recovery", type_line="Sorcery // Land",
        layout="modal_dfc", card_faces=[{"type_line": "Sorcery"}, {"type_line": "Land"}],
        predicted_labels=[], cmc=3, mana_cost="{2}{G}", land_profile=profile,
    )
    entry = SimpleNamespace(name="Bala Ged Recovery", qty=1)
    t = _tally([entry], [ResolvedDeckRow(input_name="Bala Ged Recovery", row=row)], [])
    assert t.cards_out[0]["is_land"] is False
    assert t.cards_out[0]["land_profile"] == profile
