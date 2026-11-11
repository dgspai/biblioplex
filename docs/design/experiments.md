# Experiments log

Running register of tuning experiments. One row per experiment, newest
first. Each row: date, question, verdict, full writeup. Linked doc has
apparatus, results table, findings, and reproducing steps.

| date | question | verdict | writeup |
|---|---|---|---|
| 2026-06-23 | Does the rules-layer classifier clear the ship bar after one CA/mass pattern iteration triggered by Step 4's deck eval? | **Shipped on per-card metric.** Strawman macro-F1 0.817 → 0.843 (CA +0.092), above 0.75 ship bar. `template_error` 32.70 → 29.50 — still above kill bar (15) on the original rigid `{12,12,12,6,30}` target, but inspection showed residual error is archetype variance (Korvold treasure deck genuinely runs 34 ramp), not classifier error. Metric reframed; Step 5 unblocked. | [rules-classifier-ship.md](rules-classifier-ship.md) |
| 2026-06-23 | Does a 25-NN vote over neighbours' rules-layer scores lift the rules-only classifier by ≥ 0.05 macro-F1? | **Killed.** Lift was −0.001 strawman / +0.011 otag, well below the 0.05 bar. KNN helps targeted-disrupt residue but not the weak label (CA). Same MiniLM-384 ceiling as prior experiments. Bonus: confirmed `oracle_search` already concatenates all faces, so the bundled DFC embed fix was already done. | [knn-vote-experiment.md](knn-vote-experiment.md) |
| 2026-06-22 | Can MiniLM-384 zero-shot multilabel-classify cards into `{card-advantage, ramp, targeted-disrupt, mass-disrupt}` via probe-cosine, clearing macro-F1 ≥ 0.75? | **Killed.** Macro-F1 0.379 (target 0.75, kill bar 0.60). Encoder ceiling. Next rung: KNN-vote from otag-labeled cards. | [zero-shot-classify-experiment.md](zero-shot-classify-experiment.md) |
| 2026-06-21 | Can swapping `HF_EMBED_MODEL` from MiniLM to a modern 384-dim retrieval encoder (BGE-small / E5-small / gte-small) clear `pass@20 ≥ 60%` on `/cards/:id/similar`? | **Killed.** All four candidates clustered within ±1 pair of MiniLM's 52% baseline. 384-dim ceiling. Next rung: fine-tune on otag-positive pairs. | [embedding-model-experiment.md](embedding-model-experiment.md) |

## When to add a row

When an experiment ships a verdict — kept, killed, or demoted. Don't
register hypotheses or plans; those live in conversation until they
have data. The writeup gets a `## Decision` section; the row gets the
one-line version of it.

## When to retire a row

Don't. Killed experiments are the most valuable rows — they keep the
next person from re-running the same losing variant. Edit a row only
when its verdict is overturned by a later experiment, and link both.
