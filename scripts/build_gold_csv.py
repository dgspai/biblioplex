"""Bootstrap gold-label CSV for the template classifier.

Writes a CSV combining:

1. **Strawman rows** — every card explicitly named in
   ``docs/design/template-categories.md`` with its rule-derived labels.
2. **Multi-faced rows** — MDFC / adventure / split exemplars exercising
   R25/R26/R27.
3. **Otag-sample rows** — random sample per label drawn from
   ``oracle_tags`` (excludes strawman names and, for ``ramp``, lands).
   Labels initialized as single-label = triggering category; user
   red-lines to add multilabel or flip wrong rows.

Run inside the api container so the DB connection lives:

    docker exec biblioplex-api-1 python scripts/build_gold_csv.py \\
      > data/labels_gold.csv

ponytail: stdlib csv. SQL pulls one query per sample-label. No
randomness baked into the strawman set — random seed only governs the
otag samples so re-runs are stable.
"""

from __future__ import annotations

import asyncio
import csv
import random
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from biblioplex.config import Settings

LABELS = ("ramp", "card_advantage", "targeted_disrupt", "mass_disrupt", "plan")

# (name, applicable-labels, source-note). Pulled from template-categories.md
# positives + R-rulings. Single-label by default; multilabel only where a
# ruling explicitly applies (R17, R27, MDFC R26 mixes structural + functional).
STRAWMAN: list[tuple[str, set[str], str]] = [
    # --- Ramp (§1)
    ("Sol Ring", {"ramp"}, "rock"),
    ("Arcane Signet", {"ramp"}, "rock"),
    ("Mana Crypt", {"ramp"}, "rock"),
    ("Mind Stone", {"ramp"}, "rock"),
    ("Cultivate", {"ramp"}, "land fetch"),
    ("Kodama's Reach", {"ramp"}, "land fetch"),
    ("Rampant Growth", {"ramp"}, "land fetch"),
    ("Nature's Lore", {"ramp"}, "land fetch"),
    ("Llanowar Elves", {"ramp"}, "creature ramp"),
    ("Birds of Paradise", {"ramp"}, "creature ramp"),
    ("Elvish Mystic", {"ramp"}, "creature ramp"),
    ("Bloom Tender", {"ramp"}, "creature ramp"),
    ("Smothering Tithe", {"ramp"}, "R4 treasure-maker"),
    ("Treasure Map // Treasure Cove", {"ramp"}, "treasure flip transform"),
    ("Wayfarer's Bauble", {"ramp"}, "rock-into-land"),
    # --- Card advantage (§2)
    ("Rhystic Study", {"card_advantage"}, "engine R3"),
    ("Mystic Remora", {"card_advantage"}, "engine R3"),
    ("Esper Sentinel", {"card_advantage"}, "engine R3"),
    ("Sylvan Library", {"card_advantage"}, "engine R3"),
    ("Phyrexian Arena", {"card_advantage"}, "engine R3"),
    ("Necropotence", {"card_advantage"}, "engine R3"),
    ("Sign in Blood", {"card_advantage"}, "draw 2 Rule 1"),
    ("Read the Bones", {"card_advantage"}, "draw 2+scry Rule 1"),
    ("Divination", {"card_advantage"}, "draw 2 Rule 1"),
    ("Concentrate", {"card_advantage"}, "draw 3 Rule 1"),
    ("Mulldrifter", {"card_advantage"}, "ETB draws 2 R10"),
    ("Wheel of Fortune", {"card_advantage"}, "R8 wheel"),
    ("Windfall", {"card_advantage"}, "R8 wheel"),
    ("Timetwister", {"card_advantage"}, "R8 wheel"),
    ("Cathartic Reunion", {"card_advantage"}, "R5' loot net+1"),
    ("Reconnaissance Mission", {"card_advantage"}, "combat-trigger engine"),
    ("Bident of Thassa", {"card_advantage"}, "combat-trigger engine"),
    ("Coastal Piracy", {"card_advantage"}, "combat-trigger engine"),
    ("Outpost Siege", {"card_advantage"}, "upkeep engine"),
    ("Risen Reef", {"card_advantage"}, "ETB-other engine"),
    ("Beast Whisperer", {"card_advantage"}, "ETB-other engine"),
    # --- Targeted disruption (§3)
    ("Swords to Plowshares", {"targeted_disrupt"}, "exile creature"),
    ("Path to Exile", {"targeted_disrupt"}, "exile creature"),
    ("Doom Blade", {"targeted_disrupt"}, "destroy creature"),
    ("Go for the Throat", {"targeted_disrupt"}, "destroy creature"),
    ("Counterspell", {"targeted_disrupt"}, "R11 counter"),
    ("Negate", {"targeted_disrupt"}, "R11 counter"),
    ("Mana Drain", {"targeted_disrupt"}, "R2 + R11 counter"),
    ("Force of Will", {"targeted_disrupt"}, "R11 counter"),
    ("Beast Within", {"targeted_disrupt"}, "destroy any permanent"),
    ("Generous Gift", {"targeted_disrupt"}, "destroy any permanent"),
    ("Anguished Unmaking", {"targeted_disrupt"}, "exile non-land"),
    ("Bojuka Bog", {"targeted_disrupt"}, "R12 silver-bullet (also land slot)"),
    ("Tormod's Crypt", {"targeted_disrupt"}, "R12 silver-bullet"),
    ("Relic of Progenitus", {"targeted_disrupt"}, "R12 silver-bullet"),
    ("Diabolic Edict", {"targeted_disrupt"}, "R14 edict"),
    ("Liliana's Triumph", {"targeted_disrupt"}, "R14 edict"),
    # --- Mass disruption (§4)
    ("Wrath of God", {"mass_disrupt"}, "creature wipe"),
    ("Damnation", {"mass_disrupt"}, "creature wipe"),
    ("Day of Judgment", {"mass_disrupt"}, "creature wipe"),
    ("Supreme Verdict", {"mass_disrupt"}, "uncounterable wipe"),
    ("Toxic Deluge", {"mass_disrupt"}, "X-cost wipe"),
    ("Cyclonic Rift", {"mass_disrupt"}, "bounce wipe"),
    ("Blasphemous Act", {"mass_disrupt"}, "R16 damage wipe"),
    ("Pyroclasm", {"mass_disrupt"}, "R16 damage wipe"),
    ("Vandalblast", {"mass_disrupt"}, "R19 artifact wipe"),
    ("Bane of Progress", {"mass_disrupt"}, "R19 artifact/ench wipe"),
    ("Austere Command", {"mass_disrupt"}, "modal wipe"),
    ("Armageddon", {"mass_disrupt"}, "R20 land wipe"),
    ("Ravages of War", {"mass_disrupt"}, "R20 land wipe"),
    # --- Plan (§5) — tutors, cantrips, recursion, finishers, payoffs
    ("Demonic Tutor", {"plan"}, "R7 tutor"),
    ("Vampiric Tutor", {"plan"}, "R7 tutor"),
    ("Worldly Tutor", {"plan"}, "R7 tutor"),
    ("Brainstorm", {"plan"}, "R6 cantrip"),
    ("Ponder", {"plan"}, "R6 cantrip"),
    ("Preordain", {"plan"}, "R6 cantrip"),
    ("Opt", {"plan"}, "R6 cantrip"),
    ("Eternal Witness", {"plan"}, "R9 one-shot recursion"),
    ("Regrowth", {"plan"}, "R9 one-shot recursion"),
    ("Reclaim", {"plan"}, "R9 one-shot recursion"),
    ("Elvish Visionary", {"plan"}, "R10 ETB draws 1"),
    ("Cloudkin Seer", {"plan"}, "R10 ETB draws 1"),
    ("Faithless Looting", {"plan"}, "R5' loot net 0"),
    ("Tormenting Voice", {"plan"}, "R5' loot net 0"),
    ("Craterhoof Behemoth", {"plan"}, "finisher"),
    ("Triumph of the Hordes", {"plan"}, "finisher"),
    ("Avenger of Zendikar", {"plan"}, "threat/payoff"),
    ("Doubling Season", {"plan"}, "payoff"),
    ("Propaganda", {"plan"}, "R13 pillowfort"),
    ("Ghostly Prison", {"plan"}, "R13 pillowfort"),
    ("Cathars' Crusade", {"plan"}, "payoff engine"),
    # --- Multi-faced exemplars (§6, R25/R26/R27)
    ("Tangled Florahedron // Tangled Vale", {"ramp"}, "R26 MDFC creature/land"),
    ("Sea Gate Restoration // Sea Gate, Reborn", {"card_advantage"}, "R26 MDFC draw/land"),
    ("Bonecrusher Giant // Stomp", {"targeted_disrupt", "plan"}, "R27 adventure"),
    ("Murderous Rider // Swift End", {"targeted_disrupt", "plan"}, "R27 adventure"),
    ("Brazen Borrower // Petty Theft", {"targeted_disrupt", "plan"}, "R27 adventure"),
    ("Fire // Ice", {"targeted_disrupt"}, "R25 split, both halves targeted"),
    ("Wear // Tear", {"targeted_disrupt"}, "R25 split, both halves targeted"),
]

# otag → sample-label mappings. plan is residue, no good otag → strawman only.
OTAG_PER_LABEL: dict[str, list[str]] = {
    "ramp": ["ramp", "mana ramp", "acceleration"],
    "card_advantage": ["card-advantage"],
    "targeted_disrupt": ["spot-removal"],
    "mass_disrupt": ["sweeper", "boardwipe", "wipe", "mass removal"],
}

OTAG_SAMPLE_SIZE = 30


SAMPLE_SQL = """
SELECT name FROM (
  SELECT DISTINCT c.name, c.type_line
  FROM cards c
  JOIN oracle_tags ot ON ot.oracle_id = c.oracle_id
  WHERE ot.tag = ANY(:tags)
    AND c.name <> ALL(:exclude)
) sq
WHERE NOT :exclude_lands OR sq.type_line !~* 'land'
ORDER BY random()
LIMIT :n
"""


async def main(out_path: str | None) -> None:
    settings = Settings()
    engine = create_async_engine(settings.database_url)
    rng = random.Random(42)
    strawman_names = [name for name, _, _ in STRAWMAN]

    rows: list[dict[str, str]] = []
    for name, labels, note in STRAWMAN:
        rows.append(_row(name, labels, "strawman", note))

    try:
        async with engine.begin() as conn:
            for label, tags in OTAG_PER_LABEL.items():
                # Re-seed per query so column order doesn't perturb sample.
                await conn.execute(text("SELECT setseed(:s)"), {"s": rng.random()})
                result = await conn.execute(
                    text(SAMPLE_SQL),
                    {
                        "tags": tags,
                        "exclude": strawman_names,
                        "n": OTAG_SAMPLE_SIZE,
                        "exclude_lands": label == "ramp",
                    },
                )
                for (sampled,) in result.all():
                    rows.append(
                        _row(sampled, {label}, "otag_sample", f"otag:{tags[0]}")
                    )
    finally:
        await engine.dispose()

    stream = open(out_path, "w", newline="") if out_path else sys.stdout
    try:
        writer = csv.DictWriter(
            stream,
            fieldnames=["name", *LABELS, "source", "note"],
        )
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if out_path:
            stream.close()

    print(
        f"[gold] wrote {len(rows)} rows "
        f"({sum(1 for r in rows if r['source'] == 'strawman')} strawman, "
        f"{sum(1 for r in rows if r['source'] == 'otag_sample')} otag samples)",
        file=sys.stderr,
    )


def _row(name: str, labels: set[str], source: str, note: str) -> dict[str, str]:
    base = {lab: ("1" if lab in labels else "0") for lab in LABELS}
    return {"name": name, **base, "source": source, "note": note}


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
