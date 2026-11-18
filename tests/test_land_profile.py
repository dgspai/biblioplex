"""Land profile: colours made, how it enters, and whether it has a real job."""

from __future__ import annotations

from biblioplex.classify.lands import land_profile


def _p(text: str, produced: list[str] | None, labels: list[str] | None = None,
       type_line: str = "Land") -> dict | None:
    card = {"type_line": type_line, "oracle_text": text, "produced_mana": produced}
    return land_profile(card, labels or ["plan"])


def test_non_lands_have_no_profile() -> None:
    assert _p("{T}: Add {C}{C}.", ["C"], type_line="Artifact") is None


def test_colour_buckets_come_from_produced_mana() -> None:
    assert _p("{T}: Add {C}.", ["C"])["colors"] == "colorless"
    assert _p("({T}: Add {G}.)", ["G"], type_line="Basic Land — Forest")["colors"] == "mono"
    assert _p("{T}: Add {C}.\n{T}: Add {U} or {B}.", ["B", "C", "U"])["colors"] == "dual"
    assert _p("({T}: Add {W}, {U}, or {B}.)", ["B", "U", "W"])["colors"] == "tri"
    tower = "{T}: Add one mana of any color in your commander's color identity."
    assert _p(tower, ["B", "G", "R", "U", "W"])["colors"] == "five"
    fetch = "{T}, Pay 1 life, Sacrifice this land: Search your library for an Island card."
    assert _p(fetch, None)["colors"] == "none"


def test_etb_buckets() -> None:
    assert _p("{T}: Add {C}.", ["C"])["etb"] == "untapped"
    assert _p("This land enters tapped.\n{T}: Add {G} or {W}.", ["G", "W"])["etb"] == "tapped"
    shock = "As this land enters, you may pay 2 life. If you don't, it enters tapped."
    assert _p(shock, ["U", "B"])["etb"] == "conditional"
    check = "This land enters tapped unless you control an Island or a Swamp."
    assert _p(check, ["U", "B"])["etb"] == "conditional"


def test_plain_lands_have_no_utility() -> None:
    triome = "({T}: Add {W}, {U}, or {B}.)\nThis land enters tapped.\nCycling {3}"
    assert _p(triome, ["B", "U", "W"])["utility"] == []
    gain = "This land enters tapped.\nWhen this land enters, you gain 1 life.\n{T}: Add {G}."
    assert _p(gain, ["G"])["utility"] == []
    temple = "This land enters tapped.\nWhen this land enters, scry 1.\n{T}: Add {U} or {R}."
    assert _p(temple, ["U", "R"])["utility"] == []


def test_utility_from_roles() -> None:
    bog = "This land enters tapped.\nWhen this land enters, exile target player's graveyard."
    assert _p(bog, ["B"], ["targeted_disrupt"])["utility"] == ["targeted_disrupt"]
    assert _p("{T}: Add {C}{C}.", ["C"], ["ramp"])["utility"] == ["ramp"]


def test_utility_kinds_beyond_roles() -> None:
    manland = "{T}: Add {C}.\n{1}{W}{U}: This land becomes a 2/3 Bird creature with flying."
    assert _p(manland, ["C", "W", "U"])["utility"] == ["creature_land"]
    tower = "{T}, Sacrifice a creature: Add {B}{B}."
    assert "sac_outlet" in _p(tower, ["B"], ["ramp"])["utility"]
    fomori = (
        "{T}: Add {C}.\n{3}, {T}, Discard a card: Look at the top X cards of your "
        "library. Put one of those cards into your hand."
    )
    assert _p(fomori, ["C"])["utility"] == ["selection"]


def test_modal_spell_land_profiles_its_land_face() -> None:
    card = {
        "type_line": "Instant // Land",
        "produced_mana": ["R"],
        "card_faces": [
            {"type_line": "Instant", "oracle_text": "Deal 1 damage to any target."},
            {"type_line": "Land", "oracle_text": "This land enters tapped.\n{T}: Add {R}."},
        ],
    }
    assert land_profile(card, ["targeted_disrupt"]) == {
        "colors": "mono", "etb": "tapped", "utility": []
    }


def test_scry_riding_on_a_mana_ability_is_not_selection() -> None:
    path = (
        "This land enters tapped.\n{T}: Add one mana of any color in your commander's "
        "color identity. When that mana is spent to cast a creature spell that shares "
        "a creature type with your commander, scry 1."
    )
    assert _p(path, ["W", "U", "B", "R", "G"])["utility"] == []


def test_fetch_lands() -> None:
    wilds = (
        "{T}, Sacrifice this land: Search your library for a basic land card, "
        "put it onto the battlefield tapped, then shuffle."
    )
    assert _p(wilds, None)["utility"] == ["fetch"]
    delta = (
        "{T}, Pay 1 life, Sacrifice this land: Search your library for an Island or "
        "Swamp card, put it onto the battlefield, then shuffle."
    )
    assert _p(delta, None)["utility"] == ["fetch"]
    verge = (
        "This land enters tapped.\n{T}: Add {C}.\n{2}, {T}, Sacrifice this land: Search "
        "your library for a Forest card and a Plains card, put them onto the battlefield tapped."
    )
    assert _p(verge, ["C"])["utility"] == ["fetch"]
    armory = (
        "{T}: Add {W}.\n{1}{R}{R}{W}, {T}, Sacrifice this land: Search your library for "
        "an Aura card and/or an Equipment card, reveal them, put them into your hand."
    )
    assert "fetch" not in _p(armory, ["W"])["utility"]
