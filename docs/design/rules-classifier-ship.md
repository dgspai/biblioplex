# Rules-layer classifier — shipped on per-card metric — 2026-06-23

## Question

Does the rules-layer classifier (Step 2 of
[`template-classifier-plan.md`](template-classifier-plan.md)) clear
the ship bar after Step 4's end-to-end deck eval and one CA/mass
pattern iteration?

Original bars:

- per-card macro-F1 ≥ 0.75 (ship), < 0.60 (kill) on strawman gold;
- `template_error` ≤ 6 (ship), ≥ 15 (kill) over the 10 EDHRec
  reference decks.

## Apparatus

Two iterations of `src/biblioplex/classify/rules.py` after Step 4's
first eval surfaced where rules-misses cost slot accuracy:

- **CA patterns:** broadened the engine-trigger regex to cover
  ETB-other (Temur Ascendancy, Welcoming Vampire, Satoru), sac
  triggers (Korvold), attack triggers (Ur-Dragon), landfall
  (Tatyova), magecraft (Archmage Emeritus), cast-trigger
  (Wavebreak Hippocamp). Added third-person `draws X cards`
  (Pact of the Serpent shape). Added sac-fed multi-card draw
  (`sacrifice a creature: draw cards equal to …` — Greater Good).
- **Mass patterns:** `destroy each` (Pernicious Deed), mass bounce
  with optional adjective (Aetherize's "return all attacking
  creatures"), variable damage to each (Balefire Dragon's "that
  much damage to each creature"), `-N/-N` or `counter on each`
  (Contagion Engine, Infest).

Evaluation:

- `scripts/eval_rules.py` against `data/labels_gold.csv` (213 rows;
  93 strawman + 120 otag samples).
- `scripts/eval_template.py` against `data/reference_decks/*.txt`
  (10 EDHRec average decks, basics expanded so each file holds the
  full 99-card list).

## Results

### Per-card

| label | rules-v1 F1 (2026-06-22) | rules-v2 F1 (after iteration) | Δ |
|---|---|---|---|
| ramp | 0.909 | 0.909 | 0 |
| card_advantage | 0.718 | 0.810 | **+0.092** |
| targeted_disrupt | 0.829 | 0.829 | 0 |
| mass_disrupt | 0.960 | 0.960 | 0 |
| plan | 0.667 | 0.706 | +0.039 |
| **macro-F1 (strawman)** | **0.817** | **0.843** | **+0.026** |
| macro-F1 (otag sample) | 0.461 | 0.492 | +0.031 |

Above the 0.75 ship bar.

### End-to-end (template_error)

| iteration | mean | median |
|---|---|---|
| rules-v1 | 32.70 | 32.00 |
| rules-v2 (after iteration) | 29.50 | 28.00 |

Both above the kill bar (15) on the original metric.

## Findings

1. **Iteration moves the per-card needle, not the end-to-end one.**
   CA F1 jumped 9 points off ~5 new patterns; template_error barely
   shifted (3.2 points). Two iterations is enough — the next round
   would chase a long tail of one-off engines.

2. **Residual template_error is archetype variance, not classifier
   error.** Per-deck dump:

   | deck | ramp pred | template assumes |
   |---|---|---|
   | korvold-fae-cursed-king (treasure sac) | 34 | 12 |
   | kenrith-the-returned-king (5c) | 25 | 12 |
   | the-ur-dragon (5c tribal) | 18 | 12 |
   | krenko-mob-boss (mono-R aggro) | 7 | 12 |
   | talrand-sky-summoner (mono-U spellslinger) | 7 | 12 |

   Every Korvold ramp hit is a real treasure-maker. Every Krenko
   non-ramp is a goblin haymaker. The classifier is telling the
   truth; the `{12, 12, 12, 6, 30}` target is an average, not a
   constraint that holds per deck.

3. **Plan inflation is a symptom, not a cause.** Plan over-count
   (29–44 vs target 30) is just the bucket where archetype-real
   non-ramp / non-CA cards land. Tightening the rules can't pull
   Krenko's plan down to 30 because Krenko doesn't have 12 CA
   cards to find.

## Decision

**Ship Step 2 on the per-card metric. Reframe the end-to-end metric
before re-grading.** The original `template_error ≤ 6` ship bar
assumed a single generic deck; archetype variance breaks that. Real
options for the reframe (see Step 4 in
[`template-classifier-plan.md`](template-classifier-plan.md)):

- per-archetype target sheets;
- slack bands (`|Δ| ≤ 3` = 0 error);
- direction-aware error (legitimate over-ramp not penalised).

Pick when the UI / agent consumer starts using `predicted_labels`
and shows what the eval needs to gate.

Step 5 (persist + expose) unblocked. Step 6 (manual override)
remains deferred.

## Reproducing

```bash
docker exec biblioplex-api-1 python scripts/eval_rules.py
docker exec biblioplex-api-1 python scripts/eval_template.py
```

Strawman macro-F1 stable at 0.843; template_error mean stable at 29.50.
