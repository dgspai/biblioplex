# Zero-shot multilabel classify experiment — 2026-06-22

## Question

Can the existing MiniLM 384-dim embeddings classify cards into the four
archetypal effect categories — **card advantage / ramp / targeted
disruption / mass disruption** (plus a residual **plan** = none-of-the-above
class) — via zero-shot cosine against short hand-written probe strings,
clearing macro-F1 ≥ 0.75 against Scryfall `oracle_tags` as gold?

Success bar: macro-F1 ≥ 0.75 → keep. 0.60–0.75 → demote to KNN-vote.
< 0.60 → encoder ceiling, kill track.

## Apparatus

`scripts/eval_classify.py`. Run inside `biblioplex-api-1`. Pulls
`(embedding, gold flags)` for every card with a non-null embedding via
one SQL: pgvector `<=>` cosine against 4 probe vectors + a CTE that
joins `oracle_tags` and `bool_or`s the relevant tag set per
`oracle_id`. Sweeps τ ∈ [0.05, 0.95] per label on an 80% train split
(seed 42), reports P/R/F1 on the held-out 20%.

No schema change. No model change. Same MiniLM (`all-MiniLM-L6-v2`),
same embeddings on disk.

Gold tag mapping:

| label | otag set |
|---|---|
| card-advantage | `card-advantage` |
| ramp | `ramp`, `mana ramp`, `acceleration` |
| targeted-disrupt | `spot-removal` |
| mass-disrupt | `sweeper`, `boardwipe`, `wipe`, `mass removal`, `wrath of god` |

Probe strings:

| label | probe |
|---|---|
| card-advantage | `draw cards card advantage` |
| ramp | `add mana search your library for a basic land` |
| targeted-disrupt | `destroy target creature exile target creature` |
| mass-disrupt | `destroy all creatures exile all creatures` |

Corpus: 34 213 cards (every card with an embedding). Train 27 370, test 6 843.

## Results

| label | τ* | P | R | F1 | support (test) |
|---|---|---|---|---|---|
| card-advantage   | 0.20 | 0.507 | 0.681 | **0.581** | 1268 |
| ramp             | 0.28 | 0.198 | 0.499 | 0.284 | 465 |
| targeted-disrupt | 0.38 | 0.449 | 0.408 | 0.427 | 964 |
| mass-disrupt     | 0.43 | 0.304 | 0.179 | **0.225** | 196 |
| **macro-F1**     |      |       |       | **0.379** | |
| plan (residue)   | —    | 0.770 | 0.709 | 0.738 | 4222 |

## Findings

1. **Below the kill bar.** Macro-F1 0.379 vs target 0.75 vs kill-line
   0.60. Zero-shot probe-name cosine on MiniLM does not work for this
   taxonomy. No threshold choice rescues it: τ\* are the *in-class
   optima* on the train split and the test-set F1s are still where they
   are.
2. **The failure mode is not the same for every label.** Card-advantage
   is the only label MiniLM cluster-shapes reasonably; mass-disrupt is
   the worst by ~2.5× — the encoder genuinely doesn't pull sweepers
   close to `"destroy all creatures"`.
3. **Top FPs reveal gold/definition mismatch, not just encoder error.**
   - **Ramp FPs are all lands** — Reflecting Pool, Terramorphic Expanse,
     Evolving Wilds, Vibrant Cityscape, Warped Landscape. Scryfall does
     not tag fetch/utility lands as `ramp`, but they are mana-fixing by
     definition. A user-facing classifier almost certainly *should* call
     these ramp. Score the encoder credit, dock the gold.
   - **Card-advantage FPs are helper / token entries** — `"Red Mana"`,
     `"Poison Counter"`, `"Storm Counter"`, `"Experience"`,
     `"Double-Faced Substitute Card"`. The corpus carries them as
     `layout=normal`, they have no oracle text, and their embed_source
     is essentially the type line. They float near the probe centroid
     because they're undifferentiated. Filter at ingest, not at eval.
4. **Top FNs show real semantic gaps the encoder cannot bridge.**
   Mass-disrupt FNs include `Massacre`, `Golden Demise`,
   `Curse of the Cabal`, `Seismic Wave`, `Heart of Bogardan` — all bona
   fide sweepers whose probe-cosine score sits in [0.13, 0.14]. The
   chosen τ for that label is 0.43. Even halving τ wouldn't recall
   them, and would shred precision. The encoder has no concept that
   `Massacre` plays like `Wrath of God`.
5. **Double-faced split / DFC cards consistently underperform.**
   Targeted-disrupt FNs include `Roaring Furnace // Steaming Sauna`
   and `Glassworks // Shattered Yard`. `embed_source` is computed from
   the front face's `oracle_search` only, so back-face effects are
   invisible to the encoder. Known issue, predates this experiment.
6. **The "plan" residue's 0.738 F1 is inflated by class prevalence.**
   62% of test cards have no positive gold label and predicted-plan is
   "no probe crossed τ". P=0.77 / R=0.71 means roughly 3 in 4 plan
   guesses are correct, but the absolute base rate is the same 0.62
   floor. The "plan" number is a sanity check, not a win.
7. **The earlier embedding-model swap experiment (2026-06-21) already
   foreshadowed this.** MiniLM at 384-dim couldn't pull Lightning Bolt
   to Lava Spike (1/12 at pass@20). Same encoder, same ceiling, same
   tail of cards-that-play-the-same-role-without-saying-so. Switching
   encoder candidates (BGE, E5, gte) gained ±1 pair on similarity —
   not the order of magnitude this would need.

## Decision

**Stop the zero-shot track.** Macro-F1 0.379 is decisively below 0.60.
The encoder is the ceiling; the probe wording isn't worth grinding when
the gap is this large. No code beyond `scripts/eval_classify.py` earns
its place. Script stays — it's the reusable apparatus for the next rung.

## Next levers (in cost order)

1. **KNN-vote from otag-labeled cards.** Free (HNSW index already
   exists). For each card, fetch top-K nearest neighbours whose
   `oracle_id` has otag in the relevant set; vote. Expected lift on
   the lands/ramp gold-definition gap is *negative* (KNN faithfully
   reproduces gold) but lift on the encoder-template-mismatch gap
   (Massacre, Golden Demise) is *positive* — neighbour cards' otags
   carry semantic info the probe string couldn't.
2. **Filter the corpus at ingest** to drop helper / token entries
   (`"Red Mana"`, `"Poison Counter"`, etc.). Independent of this
   experiment; ~10 line patch in `ingest/bulk.py`. Should land before
   the next classify run regardless of method.
3. **Embed both card faces** for DFC / split / adventure cards. Touches
   `embed_source` definition in `schema.sql`, requires a re-embed.
   Affects similarity too — strictly an upgrade, owe it a separate
   plan.
4. **Linear probe (sklearn `LogisticRegression` OvR)** on frozen
   embeddings. Adds sklearn or hand-rolls the closed-form, ~50 lines
   on top of `eval_classify.py`. Likely beats zero-shot but inherits
   the same encoder ceiling on mass-disrupt.
5. **Fine-tune the encoder on otag-positive pairs.** Same lever flagged
   in `docs/design/embedding-model-experiment.md §"Next lever"`. Multi-day,
   highest ceiling, last to try.

Out of scope (still):

- LLM rerank at classify time — same reason as on similarity: offline
  options not exhausted.
- New `cards.predicted_labels` column — no UI consumes it; YAGNI.

## Reproducing

```bash
# Script lives in the repo but isn't COPYd into the running image
# until the next api rebuild. For ad-hoc runs:
docker cp scripts/eval_classify.py biblioplex-api-1:/app/scripts/eval_classify.py
docker exec biblioplex-api-1 python -u scripts/eval_classify.py
```

Deterministic on seed 42. ~5 s wall (cold) once the model cache is warm
in the api container; the SQL is one round-trip.
