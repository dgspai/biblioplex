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
