# WOTC Commander bracket estimator

Rough 1–5 bracket estimate for a decklist per WOTC's Commander bracket
system (announced Sept 2024). Endpoint: `POST /decks/bracket`. Pure
logic lives in `src/biblioplex/decklist/bracket.py`.

## Heuristic estimate

Treat the number as a heuristic estimate, not a ruling. Known limitation: 2 or more detected combos map straight to bracket 5, even in casual decks, because the ladder counts combos without weighing their size, prerequisites or whether the deck can assemble them quickly. A casual stompy deck with two incidental Commander Spellbook combos can come out as bracket 5. The estimate also moves while the combo import is still running, since it counts only the combos imported so far. There is no plan to change the algorithm yet.

## Not authoritative

WOTC's system is a spirit-of-the-format guideline without hard
thresholds. The signals + thresholds encoded here are a defensible
first-cut interpretation, not a definitive scoring rubric. Two players
running the same deck may reasonably disagree with our bracket output;
that's expected and cannot be fixed without WOTC publishing a scoring
formula.

## Signals

| Signal | Source | Notes |
| --- | --- | --- |
| `game_changers` | Hand-curated list of ~40 cards from WOTC's Sept-2024 announcement | Kept as a `frozenset` at module import — WOTC's list is authoritative, no Tagger equivalent exists. Additive with the other lists — a card can be both a game-changer and fast mana. |
| `fast_mana` | Scryfall Tagger `fast-mana` slug, replacing an earlier curated list | Sol Ring, Mana Crypt, Ancient Tomb, Chrome Mox, Jeweled Lotus, Lion's Eye Diamond, ... New printings are picked up automatically via the next oracle-tag ingest. |
| `mld` | Scryfall Tagger `mass-land-destruction` slug | Armageddon, Ravages of War, Jokulhaups, Obliterate, ... |
| `tutors` | Scryfall Tagger `tutor` slug | Broader than the earlier curated list — includes narrow tutors that Tagger community classifies as tutors. |
| `extra_turns` | Scryfall Tagger `extra-turn` slug | Time Warp, Temporal Manipulation, Alrund's Epiphany, ... |
| `combos` | Count from the combos table, filtered to combos whose full card set is a subset of the deck (`combos.oracle_ids <@ deck_oracle_ids`) | Only surfaces when `COMBOS_SOURCE_URL` is set + a refresh has completed. Combo count is the strongest single signal in the ladder below. |

## Bracket ladder (first match wins)

```
combos >= 2                                → 5 (cEDH)
combos >= 1 AND fast_mana >= 3             → 5 (cEDH)
combos >= 1                                → 4 (Optimized)
game_changers >= 4                         → 4 (Optimized)
mld >= 1                                   → 4 (Optimized)
game_changers >= 1                         → 3 (Upgraded)
fast_mana >= 2                             → 3 (Upgraded)
tutors >= 3                                → 3 (Upgraded)
extra_turns >= 1                           → 3 (Upgraded)
tutors >= 1                                → 2 (Core)
fast_mana >= 1                             → 2 (Core)
(no signals)                               → 1 (Exhibition)
```

## Migration to `otag:`

Four of the five signals (`fast_mana`, `mld`, `tutors`, `extra_turns`)
migrated from curated `frozenset[str]` to Scryfall Tagger slugs against
the `oracle_tags` side table. `GAME_CHANGERS` stays curated — WOTC's
list is authoritative and Tagger has no equivalent.

Slug map lives at `src/biblioplex/decklist/bracket.py:SIGNAL_TAG_SLUGS`:

```python
SIGNAL_TAG_SLUGS: dict[str, str] = {
    "fast_mana": "fast-mana",
    "mld": "mass-land-destruction",
    "tutors": "tutor",
    "extra_turns": "extra-turn",
}
```

The router (`routers/decks.py:decklist_bracket`) fires ONE aggregate
alongside the existing combo count:

```sql
SELECT
  COUNT(*) FILTER (WHERE tag = :slug_fast_mana) AS fast_mana,
  COUNT(*) FILTER (WHERE tag = :slug_mld) AS mld,
  COUNT(*) FILTER (WHERE tag = :slug_tutors) AS tutors,
  COUNT(*) FILTER (WHERE tag = :slug_extra_turns) AS extra_turns
FROM oracle_tags
WHERE oracle_id = ANY(CAST(:deck_oids AS uuid[]))
  AND tag IN (:slug_fast_mana, :slug_mld, :slug_tutors, :slug_extra_turns)
```

`estimate_bracket` accepts the pre-computed `tag_signal_counts` dict
and blends it with the name-based `game_changers` count.

Operational notes:

* **Deployment dependency.** Signal detection now depends on the
  oracle-tag ingest having run at least once. On a fresh deployment
  where tags are empty, the 4 tag-based signals report 0 and the
  bracket falls back to `game_changers` + `combos` alone. Set up a
  scheduled refresh (or trigger `/admin/refresh?stages=tags` after
  the first `up`) to keep tags fresh.
* **Slug drift.** If Scryfall Tagger renames a slug (rare — the
  community-owned tags are stable), swap the value in
  `SIGNAL_TAG_SLUGS` and re-run `pytest`. No other code change.
* **False positives / negatives.** The Tagger community occasionally
  disagrees with a specific tagging (e.g. a marginal tutor slotted
  under `tutor`). Filing an issue on Tagger fixes it upstream; our
  ingest pass then picks it up on the next refresh.

## Response shape

```json
{
  "object": "deck_bracket",
  "bracket": 3,
  "signals": {
    "game_changers": 2,
    "fast_mana": 1,
    "mld": 0,
    "tutors": 0,
    "extra_turns": 1,
    "combos": 0
  },
  "game_changers": ["rhystic study", "smothering tithe"],
  "notes": [
    "2 game-changer card(s)",
    "1 extra-turn spell(s)"
  ],
  "unresolved": []
}
```

Notes are human-readable strings listing which signals triggered the
chosen bracket floor. Multiple notes fire when several signals
simultaneously met the same-bracket threshold.
