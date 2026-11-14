"""Rules-layer classifier — template-classifier-plan.md Step 2.

Pure regex matches against ``oracle_text``. Returns multilabel score
``[0, 1]`` per category. Multi-faced cards (adventures, MDFCs, splits,
transforms, flips) are handled by running rules on every face in
``card_faces`` and taking ``max(score)`` per label, per R25.

Patterns encode the rulings in ``docs/design/template-categories.md``:

* **CA** strict — pure draw ≥ 2 (Rule 1), engine triggers (Rule 3),
  wheels (R8). Single-card draw, tutors, cantrips, loot-net-0,
  one-shot recursion don't match — they fall through to ``plan``.
* **Targeted** — destroy/exile/counter target; burn-as-removal; R14
  edicts ("target player sacrifices", "each opponent sacrifices a
  creature"); R12 silver-bullet graveyard hate.
* **Mass** — destroy/exile all; "each player sacrifices all"; damage
  to each creature/opponent; overload (R-style mass-via-mode).
* **Plan** — residue: assigned when no functional label crosses 0.5.

ponytail: stdlib ``re``. No NLP, no tokenizer. ~6 patterns per label.
Known gaps (overload-as-targeted vs mass, modal removal that's mass in
one mode and targeted in another) are partially handled by the
overload keyword bump; rest is the KNN layer's job in Step 3.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

LABELS: tuple[str, ...] = (
    "ramp",
    "card_advantage",
    "targeted_disrupt",
    "mass_disrupt",
    "plan",
)

# Per §6.7: template slot targets for `POST /decks/analyze` deltas.
# Sums to 72 over the five labels; the remaining 28 of a 100-card EDH
# deck is the structural land/commander slot (see README — land is
# reported in `counts` but not graded against `targets`).
TARGETS: dict[str, int] = {
    "ramp": 12,
    "card_advantage": 12,
    "targeted_disrupt": 12,
    "mass_disrupt": 6,
    "plan": 30,
}

# "draw N cards" where N >= 2. Excludes "draw a card" (single).
_NUM2 = r"(two|three|four|five|six|seven|eight|nine|ten|x|\d+)"

RAMP_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("ramp:rock", re.compile(r"add \{[wubrgc]\}", re.I), 1.0),
    ("ramp:add-mana-n", re.compile(r"add (one|two|three|four|five|x) mana", re.I), 1.0),
    ("ramp:land-fetch", re.compile(r"search your library for .{0,40}\bland\b", re.I), 0.95),
    ("ramp:treasures", re.compile(r"\bcreate.{0,30}\btreasure\b", re.I), 0.85),
    ("ramp:tap-add", re.compile(r"\{t\}: add\b", re.I), 0.9),
    ("ramp:cost-reduce", re.compile(r"spells you cast.{0,40}cost \{\d+\} less", re.I), 0.85),
]

CA_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("ca:draw-n", re.compile(rf"draws? {_NUM2} cards?", re.I), 0.95),
    (
        "ca:engine-trigger",
        re.compile(
            r"(at the beginning of (your |each )?(upkeep|end step|draw step|combat)|"
            r"whenever .{0,120}(deals? combat damage|enters?( the battlefield)?|"
            r"casts? (a|an|their|your)|sacrifices? (a|an|another|one|two|\d+)|"
            r"you sacrifice|attacks?|cycles?)|"
            r"landfall|magecraft).{0,160}draw",
            re.I | re.S,
        ),
        0.9,
    ),
    (
        "ca:wheel",
        re.compile(r"discard.{0,40}hand.{0,40}draw seven cards?", re.I | re.S),
        0.95,
    ),
    (
        "ca:sac-feeder",
        re.compile(
            r"sacrifice (a|an|another|one|two|\d+) (creature|permanent)"
            r".{0,30}:.{0,80}draw .{0,30}cards?",
            re.I | re.S,
        ),
        0.85,
    ),
    (
        "ca:play-from-top",
        re.compile(
            r"\b(play|cast)\b[^.,]{0,40}\bfrom the top of your library|"
            r"\b(play|cast) the top card of your library",
            re.I,
        ),
        0.85,
    ),
    ("ca:cascade", re.compile(r"\b(cascade|discover (\d+|x))\b", re.I), 0.85),
    (
        "ca:tap-draw",
        # Sacrifice in the cost = one-shot; discard = R5' break-even loot.
        re.compile(
            r"\{t\}(?:(?!sacrifice|discard)[^:\n]){0,40}:[^\n]{0,120}draw a card(?!, then discard)",
            re.I,
        ),
        0.85,
    ),
]

TARGETED_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    (
        "targeted:destroy",
        re.compile(
            r"destroy target (creature|planeswalker|nonland|artifact|"
            r"enchantment|permanent|land)",
            re.I,
        ),
        1.0,
    ),
    (
        "targeted:exile",
        re.compile(
            r"exile target (creature|planeswalker|nonland|artifact|"
            r"enchantment|permanent|spell|card)",
            re.I,
        ),
        1.0,
    ),
    ("targeted:counter", re.compile(r"counter target", re.I), 0.95),
    (
        "targeted:edict",
        re.compile(
            r"(target player|each opponent) sacrifices (a|one|1|two|three|\d+) "
            r"(creature|permanent|artifact|enchantment)",
            re.I,
        ),
        0.85,
    ),
    (
        "targeted:bounce",
        re.compile(
            r"return target (creature|nonland|permanent|artifact|enchantment) "
            r".{0,60}(owner'?s? hand|hand)",
            re.I | re.S,
        ),
        0.85,
    ),
    (
        "targeted:burn",
        re.compile(
            r"deals? \d+ damage to (any target|target creature|"
            r"target creature or player|target creature, player, or planeswalker)",
            re.I,
        ),
        0.85,
    ),
    (
        "targeted:gy-hate",
        re.compile(
            r"exile (all cards from )?target (player'?s? |opponent'?s? )?graveyard",
            re.I,
        ),
        0.85,
    ),
]

MASS_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    (
        "mass:destroy-all",
        re.compile(
            r"destroy (all|each) (\w+ )?(creatures?|nonland|permanents?|"
            r"artifacts?|enchantments?|lands?)",
            re.I,
        ),
        1.0,
    ),
    (
        "mass:exile-all",
        re.compile(
            r"exile (all|each) (\w+ )?(creatures?|nonland|permanents?|"
            r"artifacts?|enchantments?)",
            re.I,
        ),
        1.0,
    ),
    (
        "mass:sac-all",
        re.compile(
            r"each (player|opponent) sacrifices (all|their|\w+) "
            r"(creatures|permanents)",
            re.I,
        ),
        0.85,
    ),
    (
        "mass:damage-each",
        re.compile(
            r"deals? (\d+|x|that much) damage to each (other )?(creature|opponent|player)",
            re.I,
        ),
        0.8,
    ),
    (
        "mass:bounce-all",
        re.compile(
            r"return all (\w+ )?(creatures?|nonland|permanents?) "
            r".{0,40}(owner'?s? hand|hand)",
            re.I | re.S,
        ),
        0.8,
    ),
    (
        "mass:minus-each",
        re.compile(
            r"(-\d+/-\d+|\+?-?\d+/-?\d+ counter) (to|on) each "
            r"(other )?creature",
            re.I,
        ),
        0.8,
    ),
    ("mass:overload", re.compile(r"\boverload\b", re.I), 0.7),
]


# D15.3: human-readable rule-id → description map, sourced by
# `GET /cards/:id/explain` and the web tooltip.
RULE_DESCRIPTIONS: dict[str, str] = {
    "ramp:rock": "Adds {W/U/B/R/G/C} mana",
    "ramp:add-mana-n": "Adds N mana of a color",
    "ramp:land-fetch": "Searches library for a land",
    "ramp:treasures": "Creates Treasure tokens",
    "ramp:tap-add": "Taps to add mana",
    "ramp:cost-reduce": "Makes your spells cost less",
    "ca:draw-n": "Draws two or more cards",
    "ca:engine-trigger": "Recurring draw trigger (upkeep/ETB/combat/etc.)",
    "ca:wheel": "Discards hand, then draws seven",
    "ca:sac-feeder": "Sacrifice payoff draws multiple cards",
    "ca:play-from-top": "Plays cards from the top of your library",
    "ca:tap-draw": "Tap ability that draws a card",
    "ca:cascade": "Cascade or discover: a free extra spell",
    "targeted:destroy": "Destroys a single target",
    "targeted:exile": "Exiles a single target",
    "targeted:counter": "Counters a target spell",
    "targeted:edict": "Forces opponent to sacrifice",
    "targeted:bounce": "Returns a single target to hand",
    "targeted:burn": "Deals damage to a single target",
    "targeted:gy-hate": "Exiles a graveyard",
    "mass:destroy-all": "Destroys all/each permanents of a type",
    "mass:exile-all": "Exiles all/each permanents of a type",
    "mass:sac-all": "Forces each player to sacrifice",
    "mass:damage-each": "Deals damage to each creature/opponent",
    "mass:bounce-all": "Returns all permanents of a type to hand",
    "mass:minus-each": "Minus-counters on each creature",
    "mass:overload": "Overload — spell becomes mass when paid",
}


_BUCKETS: tuple[tuple[list[tuple[str, re.Pattern[str], float]], str], ...] = (
    (RAMP_PATTERNS, "ramp"),
    (CA_PATTERNS, "card_advantage"),
    (TARGETED_PATTERNS, "targeted_disrupt"),
    (MASS_PATTERNS, "mass_disrupt"),
)


def _score_face_ex(text: str) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Score one face; return (per-label max score, per-label fired rule ids)."""
    scores: dict[str, float] = {lab: 0.0 for lab in LABELS if lab != "plan"}
    reasons: dict[str, list[str]] = {lab: [] for lab in LABELS if lab != "plan"}
    for patterns, lab in _BUCKETS:
        for rule_id, pat, weight in patterns:
            if pat.search(text):
                reasons[lab].append(rule_id)
                if weight > scores[lab]:
                    scores[lab] = weight
    return scores, reasons


def _unname(text: str, name: Any) -> str:
    """Swap a card's own name for ``~`` so "Plasma Cascade deals" isn't cascade."""
    return text.replace(name, "~") if isinstance(name, str) and name else text


def classify_by_rules_ex(
    card: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Multilabel score + per-label fired rule ids.

    Same behavior as :func:`classify_by_rules` for the score component;
    additionally returns a ``{label: [rule_id, ...]}`` map listing every
    rule pattern that matched (across all faces per R25). Dedup within
    a label preserves first-seen order.
    """
    texts: list[str] = []
    faces = card.get("card_faces")
    if isinstance(faces, list) and faces:
        for face in faces:
            if isinstance(face, dict):
                t = _unname(face.get("oracle_text") or "", face.get("name"))
                if t:
                    texts.append(t)
    if not texts:
        texts.append(_unname(card.get("oracle_text") or "", card.get("name")))

    scores: dict[str, float] = {lab: 0.0 for lab in LABELS}
    reasons: dict[str, list[str]] = {lab: [] for lab in LABELS if lab != "plan"}
    for text in texts:
        face_scores, face_reasons = _score_face_ex(text)
        for lab, s in face_scores.items():
            if s > scores[lab]:
                scores[lab] = s
        for lab, ids in face_reasons.items():
            for rid in ids:
                if rid not in reasons[lab]:
                    reasons[lab].append(rid)

    functional_max = max(scores[lab] for lab in LABELS if lab != "plan")
    scores["plan"] = 1.0 if functional_max < 0.5 else 0.0
    return scores, reasons


def classify_by_rules(card: Mapping[str, Any]) -> dict[str, float]:
    """Multilabel rules-layer score per category in ``[0, 1]`` (score-only)."""
    scores, _ = classify_by_rules_ex(card)
    return scores
