"""Behavioral tests for the rules-layer classifier.

One assertion per ruling — keeps regressions on R-numbered rulings
(R5, R6, R7, R10, R14, R25, R26, R27) easy to attribute when a future
pattern tweak breaks something.
"""

from __future__ import annotations

from biblioplex.classify.rules import (
    RULE_DESCRIPTIONS,
    classify_by_rules,
    classify_by_rules_ex,
)


def _winner(card: dict) -> tuple[str, dict[str, float]]:
    s = classify_by_rules(card)
    return max(s, key=s.__getitem__), s


def test_ramp_rock_tap_add() -> None:
    lab, _ = _winner({"oracle_text": "{T}: Add {C}{C}."})
    assert lab == "ramp"


def test_ramp_land_fetch() -> None:
    lab, _ = _winner({"oracle_text": (
        "Search your library for a basic land card, "
        "put it onto the battlefield tapped, then shuffle."
    )})
    assert lab == "ramp"


def test_ca_pure_draw_two_is_ca() -> None:
    """Rule 1 — drawing N >= 2 cards qualifies as CA."""
    lab, _ = _winner({"oracle_text": "Draw two cards."})
    assert lab == "card_advantage"


def test_ca_engine_upkeep_trigger() -> None:
    """Rule 3 — engine carve-out (Phyrexian Arena pattern)."""
    lab, _ = _winner({"oracle_text": (
        "At the beginning of your upkeep, you draw a card and you lose 1 life."
    )})
    assert lab == "card_advantage"


def test_r6_single_draw_is_not_ca() -> None:
    """R6 — single-card draw / cantrips fall through to plan."""
    lab, scores = _winner({"oracle_text": "Draw a card."})
    assert lab == "plan", scores


def test_r7_tutor_is_not_ca() -> None:
    """R7 — non-land tutors are plan, not CA. Our patterns simply don't
    match tutors as CA, so they fall through."""
    lab, scores = _winner({"oracle_text": (
        "Search your library for a card, put it into your hand, then shuffle."
    )})
    assert lab == "plan", scores


def test_r10_etb_draws_one_is_plan() -> None:
    """R10 — Elvish-Visionary-style ETB-draws-1 is plan, not CA."""
    lab, scores = _winner({"oracle_text": (
        "When this creature enters the battlefield, draw a card."
    )})
    # Pattern requires >= 2 cards or an across-turn trigger; single ETB draw
    # is intentionally NOT caught by the engine pattern.
    assert lab == "plan", scores


def test_targeted_destroy_creature() -> None:
    lab, _ = _winner({"oracle_text": "Destroy target creature."})
    assert lab == "targeted_disrupt"


def test_targeted_counterspell() -> None:
    lab, _ = _winner({"oracle_text": "Counter target spell."})
    assert lab == "targeted_disrupt"


def test_targeted_burn_is_removal() -> None:
    """Burn-as-removal — Lightning-Bolt-shape qualifies as targeted."""
    lab, _ = _winner({
        "oracle_text": "Lightning Bolt deals 3 damage to any target."
    })
    assert lab == "targeted_disrupt"


def test_r14_target_player_edict() -> None:
    lab, _ = _winner({"oracle_text": "Target player sacrifices a creature."})
    assert lab == "targeted_disrupt"


def test_r14_each_opponent_edict_is_targeted_not_mass() -> None:
    """Liliana's Triumph shape — each opponent sacs A creature → targeted."""
    lab, scores = _winner({"oracle_text": (
        "Each opponent sacrifices a creature, then you lose 1 life "
        "for each card in your hand."
    )})
    assert lab == "targeted_disrupt", scores
    # Must NOT be mass — the mass pattern requires "each ... sacrifices all/their/X".
    assert scores["mass_disrupt"] < scores["targeted_disrupt"]


def test_r12_silver_bullet_gy_hate() -> None:
    lab, _ = _winner({"oracle_text": (
        "Exile all cards from target player's graveyard."
    )})
    assert lab == "targeted_disrupt"


def test_mass_wrath() -> None:
    lab, _ = _winner({"oracle_text": (
        "Destroy all creatures. They can't be regenerated."
    )})
    assert lab == "mass_disrupt"


def test_mass_pyroclasm_damage_each_creature() -> None:
    lab, _ = _winner({
        "oracle_text": "Pyroclasm deals 2 damage to each creature."
    })
    assert lab == "mass_disrupt"


def test_mass_armageddon_destroy_all_lands() -> None:
    lab, _ = _winner({"oracle_text": "Destroy all lands."})
    assert lab == "mass_disrupt"


def test_plan_residue_payoff_anthem() -> None:
    """Cards with no functional pattern hit fall to plan."""
    lab, _ = _winner({"oracle_text": "Creatures you control get +1/+1."})
    assert lab == "plan"


def test_r25_adventure_unions_face_labels() -> None:
    """Bonecrusher Giant // Stomp — Stomp's burn pattern fires off the
    adventure face, the body face contributes nothing functional."""
    card = {
        "card_faces": [
            {"oracle_text": (
                "When a player casts a spell, if Bonecrusher Giant "
                "is on the battlefield, it deals 2 damage to that player."
            )},
            {"oracle_text": "Stomp deals 2 damage to any target."},
        ]
    }
    scores = classify_by_rules(card)
    assert scores["targeted_disrupt"] >= 0.85, scores


def test_ex_returns_fired_rule_ids() -> None:
    """D15.3 — `classify_by_rules_ex` returns per-label rule ids that fired."""
    scores, reasons = classify_by_rules_ex(
        {"oracle_text": "Destroy target creature."}
    )
    assert scores["targeted_disrupt"] == 1.0
    assert reasons["targeted_disrupt"] == ["targeted:destroy"]
    # Non-firing labels have empty lists.
    assert reasons["ramp"] == []


def test_ex_dedups_across_faces() -> None:
    """Multi-face card firing the same rule twice → dedup preserves order."""
    _, reasons = classify_by_rules_ex({
        "card_faces": [
            {"oracle_text": "Destroy target creature."},
            {"oracle_text": "Destroy target creature."},
        ]
    })
    assert reasons["targeted_disrupt"] == ["targeted:destroy"]


def test_rule_descriptions_cover_every_rule_id() -> None:
    """Every id emitted by classify_by_rules_ex must have a description."""
    from biblioplex.classify.rules import (
        CA_PATTERNS,
        MASS_PATTERNS,
        RAMP_PATTERNS,
        TARGETED_PATTERNS,
    )
    all_ids = {
        rid
        for bucket in (RAMP_PATTERNS, CA_PATTERNS, TARGETED_PATTERNS, MASS_PATTERNS)
        for rid, _, _ in bucket
    }
    missing = all_ids - RULE_DESCRIPTIONS.keys()
    assert not missing, f"rule ids missing descriptions: {missing}"


def test_classify_by_rules_score_only_matches_ex() -> None:
    """Score-only wrapper matches the ex-variant's scores exactly."""
    card = {"oracle_text": "Draw two cards. Destroy target creature."}
    scores_only = classify_by_rules(card)
    scores_ex, _ = classify_by_rules_ex(card)
    assert scores_only == scores_ex


def test_r26_mdfc_front_face_ramp_back_face_land() -> None:
    """Tangled-Florahedron-shape — front-face creature ramp, back-face
    land. The front-face ramp must show up even with an empty land back."""
    card = {
        "card_faces": [
            {"oracle_text": "{T}: Add one mana of any color."},
            {"oracle_text": ""},
        ]
    }
    scores = classify_by_rules(card)
    assert scores["ramp"] >= 0.9, scores


def _labels(text: str) -> set[str]:
    return {k for k, v in classify_by_rules({"oracle_text": text}).items() if v >= 0.5}


def test_r28_cost_reducer_is_ramp() -> None:
    """R28 — Foundry Inspector / Cloud Key shape: spells you cast cost less."""
    assert _labels("Artifact spells you cast cost {1} less to cast.") == {"ramp"}
    assert _labels(
        "Spells you cast of the chosen type cost {1} less to cast."
    ) == {"ramp"}


def test_r28_self_cost_reduction_is_not_ramp() -> None:
    """A card that only discounts itself accelerates nothing else."""
    assert _labels(
        "This spell costs {1} less to cast for each artifact you control."
    ) == {"plan"}


def test_r29_play_from_top_is_ca_engine() -> None:
    """R29 — Future-Sight shape (Rule 3 engine): play off the library top."""
    assert _labels(
        "You may play lands and cast spells from the top of your library."
    ) == {"card_advantage"}
    assert _labels(
        "As long as this attacked this turn, you may play the top card of "
        "your library."
    ) == {"card_advantage"}
    assert _labels("You may cast artifact spells from the top of your library.") == {
        "card_advantage"
    }


def test_r30_repeatable_tap_draw_is_ca_engine() -> None:
    """R30 — Iron Lad shape: a {T} ability that draws is a Rule 3 engine."""
    assert _labels(
        "{T}: Reveal the top card of your library. If it's an artifact card, "
        "draw a card."
    ) == {"card_advantage"}


def test_r30_tap_loot_and_sac_draw_stay_plan() -> None:
    """R5' break-even loot and one-shot sacrifice draw are not engines."""
    assert _labels("{T}: Draw a card, then discard a card.") == {"plan"}
    assert _labels("{1}, {T}, Sacrifice this artifact: Draw a card.") == {"plan"}


def test_r29_peeking_at_the_top_is_not_ca() -> None:
    """Looking at the top card is selection, not an extra card."""
    assert _labels(
        "Whenever you cast a spell, look at the top card of your library."
    ) == {"plan"}


def test_r29_cascade_reminder_is_not_play_from_top() -> None:
    """Cascade's reminder text isn't a play-off-the-top engine."""
    _, reasons = classify_by_rules_ex({"oracle_text": (
        "Cascade (When you cast this spell, exile cards from the top of your "
        "library until you exile a nonland card that costs less.)"
    )})
    assert "ca:play-from-top" not in reasons["card_advantage"]


def test_r30_tap_rummage_stays_plan() -> None:
    """R5' — discard-to-draw on a tap is break-even, not an engine."""
    assert _labels("{T}, Discard a card: Draw a card.") == {"plan"}


def test_r31_one_shot_cascade_and_discover_are_plan() -> None:
    """R31 — a single free spell is card flow, not an enlarged hand."""
    assert _labels("Cascade") == {"plan"}
    assert _labels("When this creature enters, discover 3.") == {"plan"}


def test_r31_granting_cascade_is_a_ca_engine() -> None:
    """R31 — a repeating cascade source is a Rule 3 engine."""
    assert _labels("Nontoken creature spells you cast have cascade.") == {
        "card_advantage"
    }
    assert _labels("The first spell you cast each turn has cascade.") == {
        "card_advantage"
    }
    assert _labels(
        "Whenever ~ attacks, you may tap two untapped creatures you control. "
        "If you do, discover 3."
    ) == {"card_advantage"}
    assert _labels("{T}, Exile a creature card from your graveyard: Discover X.") == {
        "card_advantage"
    }
    assert _labels(
        "Colorless spells you cast from your hand with mana value 7 or greater "
        'have "Cascade, cascade."'
    ) == {"card_advantage"}
    assert _labels("As you cast spells from your hand this turn, they gain cascade.") == {
        "card_advantage"
    }


def test_r32_repeating_impulse_to_hand_is_ca() -> None:
    """R32 — a recurring look-at-top-N, put-one-in-hand trigger is an engine."""
    assert _labels(
        "Whenever equipped creature attacks, look at the top six cards of your "
        "library. You may reveal an artifact card from among them and put it "
        "into your hand."
    ) == {"card_advantage"}
    assert _labels(
        "When this creature enters, look at the top four cards of your library. "
        "Put one of them into your hand."
    ) == {"plan"}


def test_r33_single_target_protection_is_targeted() -> None:
    assert _labels(
        "Target creature you control gets +2/+2 and gains hexproof until end of turn."
    ) == {"targeted_disrupt"}
    assert _labels("Change the target of target spell or ability with a single target.") == {
        "targeted_disrupt"
    }


def test_r33_one_shot_board_protection_is_targeted_too() -> None:
    """It answers one moment, not many permanents (Teferi's Protection)."""
    assert _labels("All permanents you control phase out.") == {"targeted_disrupt"}
    assert _labels("Creatures you control gain indestructible until end of turn.") == {
        "targeted_disrupt"
    }


def test_r33_static_hexproof_is_not_protection() -> None:
    assert _labels("Hexproof") == {"plan"}
    assert _labels("Equipped creature has hexproof and haste.") == {"plan"}


def test_card_name_in_its_own_text_never_fires_a_rule() -> None:
    """Plasma Cascade names itself; that isn't the cascade keyword."""
    card = {
        "name": "Plasma Cascade",
        "oracle_text": "Plasma Cascade deals 3 damage to any target.",
    }
    assert classify_by_rules(card)["card_advantage"] == 0.0


def test_r34_reminder_text_is_not_classified() -> None:
    """Parenthesised reminder text describes a keyword; it is not the card's own text."""
    assert _labels("({T}: Add {B} or {R}.)\nThis land enters tapped.") == {"plan"}
    assert _labels(
        "Echo {G}{G} (At the beginning of your upkeep, sacrifice it unless you pay its echo "
        "cost.)\nWhen this creature enters, draw a card."
    ) == {"plan"}
    assert _labels(
        "Spells you cast have affinity for artifacts. (They cost {1} less to cast for each "
        "artifact you control.)"
    ) == {"plan"}


def test_r34_basic_landcycling_and_powerstone_stay_ramp() -> None:
    assert _labels(
        "Basic landcycling {2} ({2}, Discard this card: Search your library for a basic land "
        "card, reveal it, put it into your hand, then shuffle.)"
    ) == {"ramp"}
    assert _labels(
        "When this creature enters, create a tapped Powerstone token. (It's an artifact with "
        '"{T}: Add {C}. This mana can\'t be spent to cast a nonartifact spell.")'
    ) == {"ramp"}


def test_r34_clue_and_blood_makers_are_card_advantage() -> None:
    assert _labels("When this creature enters, investigate. (Create a Clue token.)") == {
        "card_advantage"
    }
    assert _labels("Whenever you cast a spell, create a Blood token.") == {"card_advantage"}
    assert _labels("Whenever this creature attacks, it connives. (Draw a card, then discard a card.)") == {
        "plan"
    }


def test_r15_destroy_with_adjectives_is_targeted() -> None:
    assert _labels("Destroy target nonblack attacking creature.") == {"targeted_disrupt"}
    assert _labels("Destroy up to one target creature.") == {"targeted_disrupt"}
    assert _labels("Destroy target nonland, nontoken permanent an opponent controls.") == {
        "targeted_disrupt"
    }


def test_r15_destroying_your_own_permanent_is_not_removal() -> None:
    assert _labels("{T}: Destroy target permanent you own.") == {"plan"}
    assert _labels("{B}: Destroy target artifact, creature, or land you control.") == {"plan"}


def test_r9_graveyard_recursion_is_not_bounce() -> None:
    """Raise Dead / Gravedigger return a card from the graveyard; that is plan."""
    assert _labels("Return target creature card from your graveyard to your hand.") == {"plan"}
    assert _labels(
        "When this creature enters, return target creature card from your graveyard to your hand."
    ) == {"plan"}


def test_bounce_still_targeted() -> None:
    assert _labels("Return target creature to its owner's hand.") == {"targeted_disrupt"}
    assert _labels(
        "Return target creature defending player controls with toughness 2 or less to its "
        "owner's hand."
    ) == {"targeted_disrupt"}
    # Own-permanent save is protection (R33), still targeted.
    assert _labels("Return target permanent you control to its owner's hand.") == {
        "targeted_disrupt"
    }


def test_r35_minus_x_minus_x_is_removal() -> None:
    assert _labels("Target creature gets -X/-X until end of turn.") == {"targeted_disrupt"}
    assert _labels("When this creature enters, target creature an opponent controls gets -1/-1 until end of turn.") == {
        "targeted_disrupt"
    }
    assert _labels("Put three -1/-1 counters on target creature.") == {"targeted_disrupt"}


def test_r35_minus_n_zero_and_own_creature_are_not_removal() -> None:
    assert _labels("Target creature gets -3/-0 until end of turn.") == {"plan"}
    assert _labels("When this creature enters, put a -1/-1 counter on target creature you control.") == {
        "plan"
    }
    assert _labels("Target creature you control gets -1/-1 until end of turn.") == {"plan"}


def test_r36_damage_equal_to_and_x_damage_are_removal() -> None:
    assert _labels("~ deals X damage to any target.") == {"targeted_disrupt"}
    assert _labels("When this creature enters, it deals damage equal to its power to any target.") == {
        "targeted_disrupt"
    }
    assert _labels("Target creature you control deals damage equal to its power to target creature you don't control.") == {
        "targeted_disrupt"
    }


def test_r36_fight_is_removal() -> None:
    assert _labels("Target creature you control fights target creature you don't control.") == {
        "targeted_disrupt"
    }
    assert _labels("When this creature enters, it fights up to one target creature you don't control.") == {
        "targeted_disrupt"
    }
    assert _labels("{2}: This creature fights another target creature you control.") == {"plan"}
