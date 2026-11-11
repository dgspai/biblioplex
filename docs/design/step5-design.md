# Step 5 design — persist + expose predicted labels

Status: **design, no code yet.** Pre-implementation walk-through to
catch surprises before they cost a rebuild.

## Goal

After every refresh, each row in `cards` carries the rules-layer
classifier's verdict. Agents and the UI can:

- request the verdict alongside any card query (`?projection=…,predicted_labels`);
- filter by predicted label in the DSL (`pred:ramp c:r`, parallel to `otag:`).

## 1. Schema

```sql
ALTER TABLE cards
  ADD COLUMN predicted_labels text[]      DEFAULT '{}'::text[],
  ADD COLUMN label_scores     jsonb       DEFAULT '{}'::jsonb;

CREATE INDEX cards_predicted_labels_gin
  ON cards USING gin (predicted_labels);
```

- `predicted_labels` = the subset of LABELS with score ≥ 0.5 (the
  same cut `eval_template.py` uses). text[] for `@>` membership +
  GIN index.
- `label_scores` = full `{label: float}` map for downstream tuning
  + UI display.
- Defaults are non-null so freshly inserted rows (post-collapse,
  pre-classify) won't blow up the projection layer mid-refresh.
- GIN index only on `predicted_labels` — `label_scores` is jsonb
  read-only, no DSL access.

**`cards_staging` stays unchanged.** Classification happens
post-collapse; staging never carries the new columns.

### Deployment

Two-track per the README "known gaps" note:

- **New deployments** — `schema.sql` updated; fresh `docker compose
  up` picks it up.
- **Existing deployments** — run the ALTER manually via
  `docker exec biblioplex-db-1 psql -U biblioplex -d biblioplex
  -f /schema-patch.sql` (or inline). No `down -v` needed; the next
  refresh populates the new columns. Adds a tech-note to the README
  alongside the other schema-change advisories (D1.9, oracle tags,
  D10.10).

## 2. Refresh runner

Slot the new step **between Txn 2 (embed) and Txn 3 (HNSW + truncate)**.
Embed is the slow one (~10–30 min); classify is pure Python regex
over `oracle_text` + `card_faces`, so trivial cost in comparison.
Putting it after embed keeps the existing txn ordering intact and
the new code path inside the existing connection.

```python
async def default_classify(engine: _Engine) -> int:
    """Score every card via classify_by_rules; write predicted_labels + label_scores."""
    ...
```

Implementation:

1. Select `id, oracle_text, card_faces` from `cards` in pages of
   2048 (no embedding model load needed — pure regex).
2. For each row, call `classify_by_rules(row)`; build the two
   payloads.
3. `UPDATE cards SET predicted_labels = :pl, label_scores = :ls
   WHERE id = :id` via `executemany` per page.
4. Total runtime estimate: 30k cards × ~5 patterns × ~6 labels
   regex compile-cached = sub-minute on a single core.

Wire into `RefreshRunner.__init__` as `classify: ClassifyFn =
_noop_classify` (mirrors the `embed=_noop_embed` and
`tags=_noop_tags` shape so tests aren't dragged in).

Add a top-level `default_classify` factory in `refresh.py` so
production wiring (likely in `main.py` where `default_embed` is
bound to the loaded model) gets the same DI shape.

**Failure semantics:** classify failure should be best-effort like
tags — cards refresh stays committed, just with stale predictions.
Wrap in `try/except`; log; continue.

## 3. Read path — projection

Two endpoints' patterns matter:

- **`/cards/:id` + `/cards/named` + `/cards/search`** read `raw`
  from the DB and return it verbatim (`/search` then runs
  `project()` if `?projection=` set).
- **`/cards/:id/similar`** already has the "decorate raw with
  computed fields" precedent — `entry = dict(row["raw"]);
  entry["similarity"] = ...` (lines 815–819).

Apply the same pattern: each card-returning endpoint's SELECT pulls
`predicted_labels` + `label_scores` alongside `raw`, then injects
them into a `dict(raw)` copy before return / before `project()`.

Helper to centralize:

```python
def _decorate(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row["raw"])
    out["predicted_labels"] = list(row["predicted_labels"] or [])
    out["label_scores"]     = dict(row["label_scores"] or {})
    return out
```

Used by all five card-returning endpoints. Existing `similarity`
injection in `/similar` continues to layer on top.

Projection whitelist additions:

```python
TOP_LEVEL_FIELDS = frozenset({
    ..., "predicted_labels", "label_scores",
})
```

So `?projection=name,predicted_labels` works.

## 4. Read path — DSL `pred:`

Parallel to `_compile_otag`. Simpler since the column is on the
`cards` row itself (no side-table join).

In `dsl/compiler.py`:

```python
_PRED_KEYS = frozenset({"pred"})

def _compile_pred(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`pred:X` — rules-layer predicted label (membership).

    Matches when `X` is a member of `cards.predicted_labels`. GIN
    index on the column makes this a constant-time lookup.
    """
    value = node.value.strip().lower()
    if value not in LABELS:
        raise UnsupportedOperatorError(f"unknown predicted label: {value}")
    target = sa.cast(postgresql.array([value]), postgresql.ARRAY(sa.Text))
    return cards.c.predicted_labels.op("@>")(target)
```

Wire into the `_compile_kv` `if key in _PRED_KEYS:` branch.

Validate against `LABELS` from `biblioplex.classify.rules` so a typo
(`pred:rmap`) becomes `422 unknown predicted label`, not a silent
empty result.

**Out of scope:** range queries on label_scores (`pred_score:ramp>0.8`).
YAGNI until the consumer asks. The boolean predicate is what the
plan called for.

## 5. Tests

- `tests/test_classify_persist.py` — round-trip: stub refresh →
  classify step writes predicted_labels + label_scores; SELECT
  matches `classify_by_rules` output.
- `tests/test_dsl_pred.py` — `pred:ramp` compiles to
  `predicted_labels @> ARRAY['ramp']`; bad label → 422.
- `tests/test_projection_pred.py` — `?projection=name,predicted_labels`
  returns both fields; missing values default to `[]`/`{}`.
- `tests/test_routers_cards.py` — every endpoint return includes
  the two fields by default (regression: don't accidentally drop them
  in one of the five fetch paths).

## 6. Backfill on the current dev volume

After landing code:

```bash
# 1. Apply schema patch to the running DB.
docker exec biblioplex-db-1 psql -U biblioplex -d biblioplex \
  -c "ALTER TABLE cards ADD COLUMN IF NOT EXISTS predicted_labels text[] DEFAULT '{}'::text[], ADD COLUMN IF NOT EXISTS label_scores jsonb DEFAULT '{}'::jsonb;
      CREATE INDEX IF NOT EXISTS cards_predicted_labels_gin ON cards USING gin (predicted_labels);"

# 2. Run the new classify step standalone against the populated cards table.
docker exec biblioplex-api-1 python -c "
import asyncio
from biblioplex.config import Settings
from biblioplex.ingest.refresh import default_classify
from sqlalchemy.ext.asyncio import create_async_engine
async def main():
    s = Settings()
    e = create_async_engine(s.database_url)
    n = await default_classify(e)
    print(f'classified {n} cards')
    await e.dispose()
asyncio.run(main())
"
```

No re-ingest needed.

## 7. Open questions

1. **Always return predicted_labels at top level, or projection-only?**
   The plan says projection-gated. Cost of always-on is +~200 bytes
   per row (a 5-element text array + 5-key jsonb). Recommendation:
   always-on (simpler), projection just hides them when requested.
   Override if bandwidth matters.

2. **Refresh-runner test rewiring.** Adding a fourth injected callable
   (`classify=_noop_classify`) means every existing `RefreshRunner(...)`
   call site in tests gets a keyword tweak. Mechanical but wide.

3. **Multi-faced score storage.** `label_scores` stores the post-union
   map only (already `max` across faces inside `classify_by_rules`).
   No per-face breakdown. If the UI ever wants "this label came from
   the back face", that's a future column — out of scope.

## Risks

- **Embedding model lifecycle.** `default_embed` is bound to a loaded
  `SentenceTransformer`; `default_classify` doesn't need the model
  and shouldn't reload it. Wire the DI separately so the classify
  callable is independent of the embedder.
- **GIN index rebuild cost.** Per-refresh `CREATE INDEX` adds a few
  seconds. Acceptable — refresh is already ~10–30 min.
- **DSL key collision.** `pred` isn't currently a Scryfall DSL key,
  so no conflict. Future-proof against if Scryfall adds one.

## Out of plan

Step 6 (manual override table) remains deferred. Per-archetype
template targets stay in the Step 4 reframe queue.
