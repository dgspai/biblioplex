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
    # R4: only when you create it ("opponent creates", "would create" don't ramp you).
    ("ramp:treasures", re.compile(r"(?<!would )\bcreate\b.{0,30}\btreasure\b", re.I), 0.85),
    ("ramp:tap-add", re.compile(r"\{t\}: add\b", re.I), 0.9),
    ("ramp:cost-reduce", re.compile(r"spells you cast.{0,40}cost \{\d+\} less", re.I), 0.85),
    (
        "ramp:land-onto-battlefield",
        re.compile(
            r"put (a|an additional|up to \w+) land cards? [^.]{0,40}onto the battlefield|"
            r"return (up to \w+ )?(target )?lands? cards? from your graveyard to the battlefield",
            re.I,
        ),
        0.85,
    ),
    ("ramp:extra-land-drop", re.compile(
        r"play an additional land|may play (up to )?\w+ additional lands", re.I), 0.85),
    (
        "ramp:land-type-fetch",
        # R39. Not a fetchland (R1: a land) and not typecycling (open question).
        re.compile(
            r"^(?!.*sacrifice this land)(?!.*cycling).*search your library for "
            r"(a|an|up to \w+|up to x)( basic)? (plains|island|swamp|mountain|forest)"
            r"( or (plains|island|swamp|mountain|forest))? cards?[^.]*"
            r"(onto the battlefield|into your hand)",
            re.I | re.S,
        ),
        0.9,
    ),
    (
        "ramp:mana-amount",
        # R38. A counterspell that refunds mana stays targeted-only (R2).
        re.compile(
            r"^(?!.*counter target).*(\badds? (an amount of|that much|an additional|"
            r"one additional) (\{[wubrgcs]\}|one mana|mana|two mana)|"
            r"produces twice as much|for mana, (it|that player|its controller) adds)",
            re.I | re.S,
        ),
        0.85,
    ),
    (
        "ramp:untap-land",
        # Needs {T} in the cost: "Untap up to three lands" on a counterspell is a refund.
        re.compile(r"\{t\}[^:\n]*:[^\n]{0,40}untap (target|another target) land", re.I),
        0.85,
    ),
    # R34: reminder text is stripped, so keywords whose only mana/land text sat in
    # the reminder are matched by name.
    ("ramp:basic-landcycling", re.compile(r"\bbasic landcycling\b", re.I), 0.85),
    ("ramp:powerstone", re.compile(r"\bcreate[^.]{0,30}powerstone", re.I), 0.85),
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
    (
        "ca:cascade-engine",
        # R31: a one-shot cascade/discover is plan; a repeating source is an engine.
        re.compile(
            r"\bspells?\b[^.]{0,80}\b(ha(s|ve)|gain) \"?cascade\b|"
            r"(\b(whenever|at the beginning of)\b|\{[^}\n]+\}[^:\n]{0,60}:)"
            r"[^\n]{0,160}\bdiscover (\d+|x)\b",
            re.I,
        ),
        0.85,
    ),
    (
        "ca:impulse-engine",
        re.compile(
            r"\b(whenever|at the beginning of)\b[^.]{0,120}look at the top \w+ cards?"
            r"[^.]{0,60}\.[^.]{0,120}\binto your hand",
            re.I,
        ),
        0.85,
    ),
    (
        "ca:clue-maker",
        # R34: Clue/Blood tokens are a draw for mana; the token text is reminder text.
        re.compile(r"\binvestigate\b|\bcreates?\b[^.]{0,30}\b(clue|blood) tokens?", re.I),
        0.85,
    ),
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
        # R15: adjectives ("nonblack attacking creature") and "up to one" count; your own
        # permanent (sacrifice outlets) does not.
        re.compile(
            r"destroy (up to \w+ )?target (?!player\b)([\w,-]+ ){0,3}?"
            r"(creature|planeswalker|artifact|enchantment|permanent|land|battle)s?\b"
            r"(?![^.\n]{0,40}\byou (control|own)\b)",
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
        # R9: "creature CARD from your graveyard" is recursion, not bounce.
        re.compile(
            r"return target (creature|nonland permanent|nonland|permanent|artifact|enchantment)"
            r"(?! card)\b(?![^.\n]{0,60}graveyard)[^.\n]{0,80}hand",
            re.I,
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
        "targeted:shrink",
        # R35: -X/-X on a target is removal; -N/-0 is a combat trick.
        re.compile(
            r"(target (\w+ ){0,3}creature|up to (one|two|three) target creatures?)"
            r"(?![^.\n]{0,40}\byou (control|own)\b)[^.\n]{0,60}gets? -(\d+|x)/-(?!0\b)(\d+|x)",
            re.I,
        ),
        0.85,
    ),
    (
        "targeted:minus-counters",
        re.compile(
            r"put (a|an|two|three|four|x|\d+) -1/-1 counters? on (up to one )?target creature"
            r"(?![^.\n]{0,40}\byou (control|own)\b)",
            re.I,
        ),
        0.85,
    ),
    (
        "targeted:damage-var",
        # R36: burn whose amount is X / "damage equal to ..." (targeted:burn needs digits).
        re.compile(
            r"deals? (x|that much|damage equal to [^.]{3,60}) (damage )?to (any target|"
            r"up to \w+ target creatures?|target creature|target (creature|planeswalker)"
            r"( or planeswalker)?)(?![^.\n]{0,40}\byou (control|own)\b)|"
            r"deals damage equal to [^.]{3,60} to (any target|target creature( or planeswalker)?)"
            r"(?![^.\n]{0,40}\byou (control|own)\b)",
            re.I,
        ),
        0.85,
    ),
    (
        "targeted:fight",
        re.compile(
            r"target creature you control (fights|deals damage equal to (its|twice its) power to)"
            r" (up to one )?(another )?target (creature|planeswalker) "
            r"(you don't control|an opponent controls)|"
            r"\bfights? (up to one )?(another )?target creature you don't control",
            re.I,
        ),
        0.85,
    ),
    (
        "targeted:pacify",
        # R40: Pacifism-style auras neutralise one creature.
        re.compile(
            r"enchanted creature (can't attack or block|can't attack, block|"
            r"doesn't untap during its controller's untap step)",
            re.I,
        ),
        0.85,
    ),
    (
        "targeted:steal",
        # R40: permanent control change; "until end of turn" (threaten) is not removal.
        re.compile(
            r"gain control of target (?!player)[^.\n]{0,50}(?<!until end of turn)(\.|$)"
            r"(?![^.\n]*until end of turn)|"
            r"you control enchanted (creature|permanent|artifact|planeswalker|land)\b",
            re.I,
        ),
        0.85,
    ),
    (
        "targeted:exile-that",
        # Dispatch: the exile refers back to an earlier "target creature".
        re.compile(r"\btarget creature\b[^\n]*\n[^\n]*\bexile that creature\.", re.I),
        0.85,
    ),
    (
        "targeted:protect",
        # R33: one-shot protection answers one moment, even board-wide.
        re.compile(
            r"target (creature|permanent|artifact|planeswalker) you control\b[^.]{0,40}"
            r"\bgains? (hexproof|indestructible|shroud|protection)|"
            r"(creatures|permanents) you control (also )?gain "
            r"(hexproof|indestructible|shroud|protection)|"
            r"change (the|a) target of target spell|"
            r"permanents you control phase out",
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
    "ca:clue-maker": "Investigates or makes Clue/Blood tokens",
    "ramp:extra-land-drop": "Lets you play additional lands",
    "ramp:land-type-fetch": "Searches for a Forest/Plains/etc. card",
    "ramp:mana-amount": "Adds an amount of mana, or an additional mana",
    "ramp:untap-land": "Taps to untap a land",
    "ramp:basic-landcycling": "Basic landcycling fetches a land",
    "ramp:powerstone": "Creates Powerstone tokens",
    "ca:cascade-engine": "Gives spells cascade, or discovers repeatedly",
    "ca:impulse-engine": "Recurring look-at-top, put one into hand",
    "ramp:land-onto-battlefield": "Puts extra land cards onto the battlefield",
    "targeted:exile-that": "Exiles a creature it targeted earlier",
    "targeted:protect": "One-shot protection or redirect",
    "targeted:destroy": "Destroys a single target",
    "targeted:shrink": "Gives a target creature -X/-X",
    "targeted:minus-counters": "Puts -1/-1 counters on a target creature",
    "targeted:damage-var": "Deals X or equal-to damage to a target",
    "targeted:pacify": "Aura that stops a creature attacking, blocking or untapping",
    "targeted:steal": "Gains permanent control of a target",
    "targeted:fight": "Fights or bites a target creature",
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


_REMINDER = re.compile(r"\([^)\n]*\)")


def _unname(text: str, name: Any) -> str:
    """Swap a card's own name for ``~`` so "Plasma Cascade deals" isn't cascade.

    Also drops parenthesised reminder text (R34): "({T}: Add {G}.)" on a basic-typed
    land or "Draw a card" inside a token's reminder is not the card's own effect.
    """
    if isinstance(name, str) and name:
        text = text.replace(name, "~")
    return _REMINDER.sub("", text)


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
