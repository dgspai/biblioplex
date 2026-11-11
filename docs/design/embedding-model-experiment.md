# Embedding-model swap experiment — 2026-06-21

## Question

Can `/cards/{id}/similar` clear `pass@20 ≥ 60%` by swapping
`HF_EMBED_MODEL` from `sentence-transformers/all-MiniLM-L6-v2` (MTEB ~56)
to a modern 384-dim retrieval encoder, without changing the rerank
weights / schema / DSL?

## Apparatus

`scripts/eval_similarity.py` — 16 mirror groups, 120 ordered pairs,
primary metric `pass@20`. Same groups across all variants (no `GROUPS`
edits mid-series). Each variant: `.env` swap → `docker compose up -d
--build api` → `make reembed` → eval. All on `master @ 6ae5f99`, no
source changes that survived.

## Results

| variant | pass@20 | mean rank (found) | mean sim | missing top-100 |
|---|---|---|---|---|
| MiniLM (baseline) | 62/120 (52%) | 9.1 | 0.853 | 48 |
| `BAAI/bge-small-en-v1.5` | 59/120 (49%) | 8.8 | 0.927 | 54 |
| `intfloat/e5-small-v2` (no prefix) | 63/120 (52%) | 9.0 | 0.959 | 49 |
| `intfloat/e5-small-v2` (+ `"passage: "`) | 59/120 (49%) | 7.7 | 0.964 | 58 |
| `thenlper/gte-small` | 63/120 (52%) | 10.0 | 0.967 | **46** |
| MiniLM (restored) | 62/120 (52%) | 9.2 | 0.853 | 48 |

E5 `"passage: "` prefix was a transient code patch in
`src/biblioplex/embed/batch.py` (hardcoded prepend before `model.encode`);
reverted after eval — not in tree.

## Per-group deltas (vs MiniLM baseline)

Only groups that moved on at least one variant:

| group | MiniLM | BGE | E5 | E5+pfx | gte |
|---|---|---|---|---|---|
| Urza | 1/2 | 1/2 | **2/2** | **2/2** | **2/2** |
| Etherium Sculptor | 9/12 | 7/12 | 7/12 | 8/12 | 7/12 |
| Abyssal Persecutor | 0/2 | 0/2 | **2/2** | **2/2** | 0/2 |
| Doom Blade | 12/12 | 11/12 | 12/12 | 12/12 | 12/12 |
| Cultivate | 7/12 | 8/12 | 8/12 | 8/12 | 8/12 |
| Divination | 5/12 | 4/12 | 4/12 | 4/12 | 4/12 |
| Lightning Bolt | 1/12 | 0/12 | 0/12 | 0/12 | 0/12 |
| Wrath of God | 5/12 | **11/12** | **9/12** | 5/12 | **9/12** |
| Bojuka Bog | 6/12 | 4/12 | 4/12 | 4/12 | 6/12 |
| Mana Reflection | 4/6 | 3/6 | 4/6 | 3/6 | 3/6 |
| Hangarback Walker | 2/2 | 0/2 | 1/2 | 1/2 | 2/2 |
| Iron Man / Repurposing Bay | 0/2 | 0/2 | 0/2 | 0/2 | 0/2 |
| Cavern of Souls | 0/2 | 0/2 | 0/2 | 0/2 | 0/2 |

## Findings

1. **384-dim ceiling sits at 52%.** Four general-purpose small encoders
   (MiniLM, BGE-small, E5-small, gte-small) all clustered within ±1
   pair of baseline on the 120-pair set.
2. **Persistent failures are text-content problems, not vector-recall
   problems.** Lightning Bolt (1/12 best), Cavern of Souls (0/2 on every
   variant), Iron Man / Repurposing Bay (0/2 on every variant), and
   Brainstorm (3/12 on every variant) never recovered on any model.
   Lava Spike's oracle text genuinely doesn't read like Lightning Bolt's;
   Cavern's flavor-only utility-land text has no semantic neighbours in
   the index; no general-purpose 384-dim encoder closes those gaps.
3. **Encoder choice trades off cluster shape, not score.** BGE-small
   lifted Wrath of God +6 but lost Hangarback (2→0) and Bojuka (6→4).
   E5-no-prefix won Abyssal Persecutor (0→2) and Urza (1→2) but lost
   Etherium (9→7). gte-small preserved tight clusters and posted the
   lowest miss count (46) but won fewer new pairs than E5.
4. **E5's `"passage: "` prefix made things worse here.** It tightened
   close clusters (lowest mean rank 7.7) but pushed +9 pairs out of
   top-100 entirely. Hypothesis: a constant leading-token prefix
   compresses the embedding space toward shared semantic mass, helping
   already-close pairs but hurting weak-but-present neighbours.
5. **HNSW + cosine + MTEB delta of ~5pp is too small to swing the test
   set.** The plan estimated 3–8pp lift from BGE-small; observed −3pp.
   MTEB gains on generic web/QA pairs don't transfer to MtG card
   semantics without domain adaptation.

## Decision

**Stop the model-swap track.** Reverted to MiniLM. No commit beyond
this doc — no source change earned its place.

## Next lever

Fine-tuning a small encoder on `otag:`-positive pairs (cards sharing
oracle_tags are positive, random cards are negative). MtG-specific
semantics are what the generic encoders are missing; teaching the
encoder which cards "play the same role" is what closes the gap on
Lightning Bolt-style failures. Deferred to its own plan.

Other levers explicitly NOT pursued in this series, in rough order of
expected cost/benefit:

- EDHREC deck co-occurrence as a *synergy* primitive — different
  product (`/synergy` endpoint), separate plan.
- Per-clause trgm decomposition for multi-ability cards — narrow
  scope, depends on a settled embedding choice first.
- LLM rerank at query time — defer until offline options are
  exhausted.
- Expand the 16-group eval set — comparable numbers require a stable
  set; expand only after a real shipping change lands.

## Reproducing

Per variant:

```bash
# 1. Set HF_EMBED_MODEL in .env (HF_EMBED_DIM stays 384 for all
#    candidates here; bge-base / nomic would need 768 + ALTER TABLE).
docker compose up -d --build api
until curl -fsS -o /dev/null http://localhost:8000/health; do sleep 1; done

# 2. Re-embed (first run downloads ~130 MB into the hfcache volume).
make reembed

# 3. Eval.
.venv/bin/python -u \
  scripts/eval_similarity.py > /tmp/eval_<label>.txt
tail -6 /tmp/eval_<label>.txt
```

E5 `"passage: "` variant required a one-line edit in
`src/biblioplex/embed/batch.py` (prepend `"passage: "` to each
`embed_source` before `model.encode`) plus the matching update to
`tests/test_embed_batch.py::test_embed_pending_single_batch`.
