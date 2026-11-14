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
