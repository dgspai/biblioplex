# KNN-vote layer over the rules residue — 2026-06-23

## Question

Does a 25-NN vote over neighbours' rules-layer scores meaningfully lift
the rules-only classifier on cards where rules returned ``plan``
(no functional pattern fired)? Plan accept bar:
``macro-F1 lift ≥ 0.05`` and ``template_error reduction ≥ 2``.

Also resolved as a side-effect: ``oracle_search`` already concatenates
front + back face text (per `src/biblioplex/ingest/flatten.py`
``_join_field``), so the bundled "DFC back-face embed fix" (Deliverable A)
was a no-op. The embedding has been seeing both faces all along.
Step 3 reduced to KNN only.

## Apparatus

`src/biblioplex/classify/knn.py` (now removed) — for cards whose rules
plan score ≥ 0.5, pulled K = 25 neighbours via the existing pgvector
``<=>`` HNSW index, ran `classify_by_rules` on each, averaged per
label. Composite = ``max(rules, knn)`` per functional label, plan
re-derived as residue.

Graded via `scripts/eval_rules.py --with-knn` against
`data/labels_gold.csv` (213 rows; 93 strawman + 120 otag samples).
Same threshold (0.5) as the rules-only baseline.

## Results

### Strawman (signed-off gold)

| label | rules F1 | rules+KNN F1 | Δ |
|---|---|---|---|
| ramp | 0.909 | 0.909 | 0 |
| card_advantage | 0.718 | 0.700 | **−0.018** |
| targeted_disrupt | 0.829 | 0.857 | **+0.028** |
| mass_disrupt | 0.960 | 0.960 | 0 |
| plan | 0.667 | 0.654 | −0.013 |
| **macro-F1** | **0.817** | **0.816** | **−0.001** |

### Otag sample (noisy gold)

| label | rules F1 | rules+KNN F1 | Δ |
|---|---|---|---|
| ramp | 0.577 | 0.577 | 0 |
| card_advantage | 0.381 | 0.381 | 0 |
| targeted_disrupt | 0.638 | 0.694 | **+0.056** |
| mass_disrupt | 0.708 | 0.708 | 0 |
| plan | 0.000 | 0.000 | 0 |
| **macro-F1** | **0.461** | **0.472** | **+0.011** |

## Findings

1. **Macro-F1 lift below the 0.05 accept bar on both sets.**
   Strawman composite is flat (−0.001); otag composite gains 0.011.
   Doesn't meet the plan's ship criterion.
2. **KNN helps where the encoder cluster is tight and the rules
   templating is just unusual.** Targeted-disrupt picked up 1 strawman
   TP and 2 otag TPs — cards whose oracle text doesn't match the
   destroy/exile/counter/burn patterns but whose neighbours do.
3. **KNN does not help CA, the weakest label.** CA's 8 strawman FNs
   are engines with phrasing the regex misses (`Mystic Remora`,
   `Esper Sentinel`, etc.). Their neighbours mostly share the same
   templating problem — the encoder doesn't cluster them next to
   confident-CA engines like Phyrexian Arena, so the vote averages
   low. Different problem, different lever.
4. **Same encoder ceiling as the prior similarity / zero-shot
   experiments.** MiniLM-384 can't pull Lightning-Bolt-likes to
   Lava-Spike-likes, can't pull Massacre-shape sweepers to Wrath-shape
   sweepers, can't pull missed-engine CAs into the engine cluster.
   KNN built on that index inherits the same failure shape.
5. **Bonus discovery — DFC fix wasn't needed.** `oracle_search`
   already space-joins top-level + all `card_faces[*].oracle_text`,
   so `embed_source` already includes both faces. The plan's
   Deliverable A was based on a misread of the schema. No-op,
   commit `e2798e2` overstated the work involved.

## Decision

**Drop the KNN layer. Ship Step 2 alone.** Per the plan's kill rule:

> no measurable lift → drop KNN, ship Step 2 alone

`knn.py` removed from the tree. `eval_rules.py` reverted to the
rules-only path. Step 5 (persist + expose) will read directly from
`classify_by_rules` with no KNN composite.

The 8 strawman CA FNs remain the actionable signal — engines with
templating the regex misses. Right lever is **more CA patterns**, not
KNN smoothing. Defer to a Step 2 patterns iteration *after* the deck
eval in Step 4 surfaces which CA misses actually cost slot accuracy.

## Reproducing

KNN module is gone; reproducing requires reverting commit
``<this-commit>`` to get `knn.py` back. The `eval_rules.py
--with-knn` flag is also gone — only the rules-only path remains:

```bash
docker exec biblioplex-api-1 python scripts/eval_rules.py
```

Strawman macro-F1 stable at 0.817.
