# Architecture notes

## Architecture

FastAPI + Uvicorn in front of PostgreSQL 17 with the `pgvector` and `pg_trgm` extensions. One row per `oracle_id`; reprints collapse to the first/oldest printing (`released_at` ASC). Multi-face text (`transform`, `modal_dfc`, `split`, `flip`, `adventure`, `meld`, …) is flattened into `name_search`/`type_search`/`oracle_search`/`mana_cost_search`/`colors_eff` so the DSL works against the whole card, not just the front face. `reversible_card` is the exception — each face becomes its own row.

The ingest pipeline (`POST /admin/refresh`) runs as a FastAPI `BackgroundTask` guarded by `pg_advisory_lock(42)`: Scryfall bulk-data discovery → `httpx.stream` of the JSON dump → `ijson.items_async` parse → reversible-split + multi-face flatten → batched `cards_staging` INSERT → collapse via `INSERT … SELECT DISTINCT ON (oracle_id) … ORDER BY released_at ASC NULLS LAST` → SentenceTransformer encodes `embed_source` into the `vector(D)` column in `EMBED_BATCH`-sized pages → `DROP`/`CREATE INDEX cards_embedding_hnsw` (bulk-then-index is ~10× faster than incremental) → `TRUNCATE cards_staging`.

### Refresh stage dependencies

Stages run in parallel per the DAG: `tags` kicks off alongside `ingest`, and once `ingest`+COLLAPSE commits, `embed→hnsw`, `classify`, `combos`, and `deck_cards` fan out concurrently. Wall time drops to roughly `max(ingest, tags) + max(embed+hnsw, classify, combos)`. Best-effort stages (`tags`, `classify`, `combos`, `deck_cards`) still log-and-swallow exceptions; `ingest` and `embed→hnsw` failures still abort the run and release the advisory lock.

| Stage      | Reads                                                          | Writes                                             | Depends on                    |
|------------|----------------------------------------------------------------|----------------------------------------------------|-------------------------------|
| `ingest`   | Scryfall `default_cards` bulk (HTTP stream)                    | `cards_staging` → `cards` (via COLLAPSE)           | —                             |
| `embed`    | `cards.embed_source` where `embedding IS NULL`                 | `cards.embedding`                                  | `ingest`                      |
| `classify` | `cards.oracle_text`, `cards.card_faces`                        | `cards.predicted_labels`, `label_scores`, `label_reasons` | `ingest`               |
| `hnsw`     | `cards.embedding`                                              | `cards_embedding_hnsw` index; TRUNCATE `cards_staging` | `embed`                   |
| `tags`     | Scryfall oracle-tags bulk (HTTP)                               | `oracle_tags` (own table, no cards join)           | — (fully independent)         |
| `combos`   | Commander Spellbook `/variants/` (HTTP, paginated); `cards.name → oracle_id` | `combos` (own table)                     | `ingest`                      |
| `deck_cards` | `deck_versions.decklist` (current version per deck)            | `deck_cards` (derived index)                       | `ingest`                      |

DAG:

```
                 ┌── embed ── hnsw
                 ├── classify
ingest ──────────┼── combos
                 └── deck_cards

tags (independent — can run anytime, even alongside ingest)
```

`embed` and `classify` both `UPDATE cards` but on disjoint columns (HOT updates); running them concurrently roughly doubles WAL churn on `cards`. The advisory lock is session-scoped on one outer connection; each parallel branch opens its own pool conn for actual DB work so branches don't contend on the lock holder.

## Layout

```
src/biblioplex/    package source
  config.py        env-backed Settings (pydantic-settings)
  db.py            async engine factories (read pool w/ 5s statement_timeout)
  main.py          FastAPI app + lifespan
  routers/         HTTP routers (health, admin, cards, catalog, combos,
                   commanders, deck_library, inventory, wishlist,
                   uploads; stateless deck analysis under decks/)
  ingest/          bulk-ingest pipeline (discovery, stream parse, split,
                   flatten, collapse, tags, combos, refresh runner)
  classify/        rules-layer role classifier (pred: labels)
  decklist/        decklist parser + Moxfield/Archidekt/Deckbox importers
  decks/           deck library service (versions, branches, evaluation)
  scryfall/        Scryfall catalog client
  dsl/             tokenizer + parser + AST compiler for /cards/search
  embed/           SentenceTransformer loader + batch encoder
  projection/      ?projection= parser + dict-walk projector
  inventory/       Deckbox CSV fetch/parse + owned-collection and wishlist
                   persistence (`wishlist.py` reuses the same parser)
tests/             pytest suite (~1,800 tests)
schema.sql         DDL bootstrap (extensions, cards + cards_staging,
                   indexes; HNSW built post-ingest)
scripts/smoke.sh   end-to-end smoke against a running compose stack
docker-compose.yml pgvector pg17 + api + web services
web/               vanilla-JS UI, served by nginx
Dockerfile         api image
```

## First-boot tech notes

Non-obvious choices a fresh reader will trip on:

- **PG17 immutability.** Generated columns require `IMMUTABLE` expressions. `lower(text) LIKE …` replaces `ILIKE`, `regexp_replace(lower(name), …)` replaces the `'i'` flag, and the `immutable_array_join(text[], text)` SQL wrapper in `schema.sql` works around `array_to_string` being `STABLE` in PG17.
- **psycopg3 jsonb adaptation.** `dict`/`list` parameters are not auto-adapted to `jsonb`. The staging row builder wraps `legalities`, `card_faces`, `all_parts`, `raw` in `psycopg.types.json.Jsonb` before `executemany`.
- **ijson `use_float=True`.** ijson defaults numeric values to `Decimal`, which the `Jsonb` encoder cannot serialize. The stream parser is invoked with `use_float=True`.
- **Reversible face ids.** The design calls for a synthetic fallback id when a face omits `id`. Schema column is `uuid`, so the fallback is `uuid5(NAMESPACE_OID, f"{parent_id}:{index}")`, not the literal `<parent>:<i>`.
- **Refresh runner txn split.** The embed step opens its own pool connection. The runner therefore commits COLLAPSE before calling embed, then handles the HNSW rebuild in two hops: a scratch-name `CREATE INDEX CONCURRENTLY cards_embedding_hnsw_new` on a fresh `isolation_level="AUTOCOMMIT"` connection (CONCURRENTLY forbids running inside a txn), then a tiny swap txn on the lock-holding conn that drops the served name and renames the scratch onto it. The currently-serving index stays live throughout the build, so `/similar` is available even during a mid-refresh reindex. A session-level advisory lock spans every txn to keep the refresh lock semantics intact. The unlock step rolls back the lock-holding connection before opening its own txn so a prior aborted-transaction state cannot strand `pg_advisory_unlock(42)` — lock release is independent of any earlier txn's outcome.
- **Schema bootstrap is single-pass.** `schema.sql` is mounted into `/docker-entrypoint-initdb.d/`, which only runs on a fresh data volume. Schema changes (e.g. `edhrec_rank` + `price_usd` were added after the first release; oracle-tag ingest added the `oracle_tags` table; `cards_name_lower_idx` + `cards_name_sort_key_trgm` were added for `/cards/named` exact / fuzzy lookups; the role classifier added `predicted_labels` + `label_scores` + `cards_predicted_labels_gin`) require existing deployments to `docker compose down -v` before the next `docker compose up --build` — otherwise the new objects are missing and the depending endpoints (e.g. `/cards/search?order=edhrec|usd`, `otag:`, `pred:`) will fail at query time. The classifier columns can also be applied in-place without losing the volume: `docker exec biblioplex-db-1 psql -U biblioplex -d biblioplex -c "ALTER TABLE cards ADD COLUMN IF NOT EXISTS predicted_labels text[] NOT NULL DEFAULT '{}'::text[], ADD COLUMN IF NOT EXISTS label_scores jsonb NOT NULL DEFAULT '{}'::jsonb; CREATE INDEX IF NOT EXISTS cards_predicted_labels_gin ON cards USING gin (predicted_labels);"` then trigger a refresh (or call `biblioplex.ingest.refresh.default_classify` directly) to backfill.
- **Embed loop partial index.** `WHERE embedding IS NULL ORDER BY id LIMIT :batch` in `embed/batch.py` seq-scans an increasingly-late NOT-NULL prefix as embeddings populate. `cards_embedding_pending_idx` (partial btree on `(id) WHERE embedding IS NULL`) fixes it. Existing deployments can apply in place: `docker exec biblioplex-db-1 psql -U biblioplex -d biblioplex -c "CREATE INDEX IF NOT EXISTS cards_embedding_pending_idx ON cards (id) WHERE embedding IS NULL;"` — the next refresh's embed pass picks it up automatically.
- **Autocomplete prefix index.** `/cards/autocomplete` fires `LOWER(name) LIKE :prefix || '%'`. The existing `cards_name_lower_idx` uses the default operator class and cannot back LIKE prefix patterns — the planner would seq-scan ~30k rows per keystroke. `cards_name_lower_pattern_idx` (btree on `lower(name) text_pattern_ops`) turns the LIKE into an index range scan. Existing deployments: `docker exec biblioplex-db-1 psql -U biblioplex -d biblioplex -c "CREATE INDEX IF NOT EXISTS cards_name_lower_pattern_idx ON cards (lower(name) text_pattern_ops);"` — used automatically by the planner on the next query.
- **Data-driven "any number of copies".** `decklist/legality.py` used to gate the copy-cap exemption (Rat Colony, Persistent Petitioners, Seven Dwarves, Nazgûl, Hare Apparent, Dragon's Approach, Slime Against Humanity, Templar Knight, Shadowborn Apostle, …) on a curated `frozenset[str]` that bit-rotted with each new Standard set. Replaced with `cards.has_unlimited_copies` — a `GENERATED ALWAYS AS ... STORED` boolean whose expression matches both `A deck can have any number of cards named X` AND `A deck can have up to <word> cards named X` phrasings via one `~*` regex. New "any number" cards inherit the exemption automatically after the next `/admin/refresh`. Existing deployments: `docker exec biblioplex-db-1 psql -U biblioplex -d biblioplex -c "ALTER TABLE cards ADD COLUMN IF NOT EXISTS has_unlimited_copies boolean GENERATED ALWAYS AS (COALESCE(oracle_search, '') ~* 'a deck can have (any number of|up to [a-z]+) cards named') STORED;"` — one full-table pass to backfill (~1 min on 30k rows), then the legality endpoint picks up the flag automatically.
- **Delta refresh.** The embed pass now skips rows whose `oracle_search` text hasn't changed since last embed. `cards.oracle_text_hash` records `md5(oracle_search)` at each embed; `SELECT_PENDING_SQL` picks up `WHERE embedding IS NULL OR oracle_text_hash IS DISTINCT FROM md5(oracle_search)`. Cold boot re-embeds every row (all-NULL embeddings); steady-state refreshes touch only cards Scryfall actually re-worded — typically a single-digit percentage per week. Existing deployments: `docker exec biblioplex-db-1 psql -U biblioplex -d biblioplex -c "ALTER TABLE cards ADD COLUMN IF NOT EXISTS oracle_text_hash text NOT NULL DEFAULT '';"` — the next refresh re-embeds every row exactly once (empty hash IS DISTINCT FROM any real md5), then subsequent refreshes short-circuit.

## Scryfall data sources

Biblioplex pulls from three Scryfall data sources, all documented under [scryfall.com/docs/api](https://scryfall.com/docs/api):

- **Bulk data — cards.** `default_cards` is the ingest source. Single-row-per-`oracle_id` collapse happens in our own staging table; we do not consume Scryfall's per-`oracle_id` bulk.
- **Catalogs — `/catalog/*`.** Plain string arrays (`{object,"catalog",uri,total_values,data:[]}`) used as canonical token whitelists. The keyword catalogs (`keyword-abilities`, `keyword-actions`, `ability-words`) are fetched once at app startup and feed `is:frenchvanilla`'s tokenised oracle-text check (see `src/biblioplex/scryfall/catalogs.py`). Fetch failures fall back to a punctuation heuristic so search keeps working offline.
- **Oracle tags — wired.** Scryfall publishes community-sourced ([Tagger](https://tagger.scryfall.com/)) **oracle tags** (functional role — `removal`, `ramp`, …) as a daily bulk file alongside the card data. Each tagging joins to a card via `oracle_id`. Parent tags have no direct taggings — `src/biblioplex/ingest/tags.py` walks `child_ids` once at ingest and writes the fully-flattened `(oracle_id, slug)` pairs into the `oracle_tags` side table, including every entry in each tag's `aliases`. The DSL exposes `otag:` and the long-form alias `function:` (e.g. `otag:removal c:r`); the web UI surfaces an "Oracle tags" chip strip on the card detail pane (`GET /cards/:id/tags`). Weight (`very_strong`/`strong`/`median`/`weak`) is intentionally dropped — Scryfall's own `otag:` is boolean. **Illustration tags** (`atag:`, joined via `illustration_id`) are still rejected with `422 unsupported_operator`. Caveat: community-moderated data; if a slug ever needs hiding, ship a per-tag toggle.

Non-Scryfall data source:

- **Commander Spellbook combos — on by default.** Default: `COMBOS_SOURCE_URL=https://json.commanderspellbook.com/variants.json.gz` — Spellbook's published bulk file (`{timestamp, version, variants, aliases}`), which Spellbook asks clients to use instead of paging through `/variants/`. It is downloaded in one GET, kept gzipped in a temp file, streamed with `ijson` over `variants.item`, and each variant is mapped (`id`, `uses[*].card.name` → cards, `identity` → color_identity, `notablePrerequisites` or `easyPrerequisites` → prereqs, `description` → steps, `produces[*].feature.name` → results). A source with no `variants` raises. `src/biblioplex/ingest/combos.py` fetches on refresh, resolves each combo's card names against `cards.name` (case-insensitive; combos referencing any unresolved card are dropped silently), and upserts the `combos` side table (`id text PK, oracle_ids uuid[], color_identity text[], prereqs text, steps text, results text[]`) in batches of 5,000 — `INSERT ... ON CONFLICT (id) DO UPDATE` per batch (non-`id` columns overwritten so edited combos land), then a single `DELETE FROM combos WHERE NOT (id = ANY(seen_ids))` at the end reconciles removals. Rows become queryable during the run instead of after; a crash mid-run leaves the previous rows plus whatever batches already landed, and the next successful run reconciles. Best-effort — a fetch or parse failure aborts the reconcile-DELETE and the cards refresh stays committed, mirroring the oracle-tag ingest posture. The runner step is a no-op when `COMBOS_SOURCE_URL` is set empty, so a deployment that opts out pays zero overhead per refresh. Schema addition ships as `IF NOT EXISTS`; existing deployments can apply in place via `docker exec biblioplex-db-1 psql -U biblioplex -d biblioplex -c "CREATE TABLE IF NOT EXISTS combos (id text PRIMARY KEY, oracle_ids uuid[] NOT NULL, color_identity text[] NOT NULL DEFAULT '{}', prereqs text, steps text, results text[] NOT NULL DEFAULT '{}'); CREATE INDEX IF NOT EXISTS combos_oracle_ids_gin ON combos USING gin (oracle_ids); CREATE INDEX IF NOT EXISTS combos_color_identity_gin ON combos USING gin (color_identity); CREATE INDEX IF NOT EXISTS combos_prereqs_trgm ON combos USING gin (prereqs gin_trgm_ops); CREATE INDEX IF NOT EXISTS combos_steps_trgm ON combos USING gin (steps gin_trgm_ops);"` then set the env var and trigger a refresh. The two trgm indexes back `/combos/search?q=` — `WHERE COALESCE(prereqs, '') ILIKE :q OR COALESCE(steps, '') ILIKE :q` otherwise seq-scans the whole table.

## Similarity eval + re-embed

```bash
make eval-similarity   # python scripts/eval_similarity.py
make reembed           # docker exec biblioplex-api-1 python scripts/reembed.py
```

`scripts/eval_similarity.py` hits `/cards/:id/similar` for ~15 known mirror groups (counterspells, ramp tutors, wraths, mana doublers, GY hate, …) and reports `pass@20`, mean rank, and mean similarity. Use it to compare tuning iterations apples-to-apples — bumping `HF_EMBED_MODEL`, `HYBRID_ALPHA`, or the `embed_source` expression. Override base URL with `BIBLIOPLEX_URL` env. See [`docs/design/embedding-model-experiment.md`](design/embedding-model-experiment.md) for the 2026-06-21 swap series (BGE-small / E5-small / gte-small vs MiniLM baseline — 384-dim ceiling sits at 52%).

`scripts/reembed.py` clears every `cards.embedding`, runs the embed batcher, and rebuilds the HNSW index — no Scryfall download. `make reembed` runs it inside the api container so the SentenceTransformer cache stays hot. Use after editing `embed_source`'s generated expression or swapping `HF_EMBED_MODEL`.

`scripts/eval_classify.py` zero-shot multilabel-classifies every card into `{card-advantage, ramp, targeted-disrupt, mass-disrupt}` via cosine against 4 hand-written probe strings, scored against `oracle_tags` as gold. Run with `docker exec biblioplex-api-1 python scripts/eval_classify.py` (one round-trip SQL + per-label τ sweep + top FP/FN). See [`docs/design/zero-shot-classify-experiment.md`](design/zero-shot-classify-experiment.md) for the 2026-06-22 run — macro-F1 0.379, encoder ceiling decisive, next rung is KNN-vote.

`scripts/eval_rules.py` grades the rules-layer classifier (`src/biblioplex/classify/rules.py`) against `data/labels_gold.csv` (strawman + otag samples), reporting per-label P/R/F1 + macro-F1 per source. `scripts/eval_template.py` runs the same classifier over `data/reference_decks/*.txt` (one EDHRec average deck per file, expanded basic-land quantities) and reports per-deck `template_error = Σ |target_c − predicted_c|` against the template `{12 ramp, 12 CA, 12 targeted, 6 mass, 30 plan}` (lands excluded — counted but not graded). Both run inside the api container. See [`docs/design/template-classifier-plan.md`](design/template-classifier-plan.md) for the 6-step plan and accept/kill bars.

**Predicted labels in the wire.** Every card response (search / by-id / named / similar) carries `predicted_labels: text[]` and `label_scores: {label: float}` alongside the Scryfall payload — populated by `ingest/refresh.py:default_classify` post-embed (best-effort: classify failure leaves stale predictions; cards refresh stays committed). The DSL gains `pred:<label>` (e.g. `pred:ramp c:g`), backed by a GIN index on `predicted_labels`; unknown labels surface as 422. The projection whitelist accepts both new fields. Score map is read-only — no `pred_score:` range clause yet (YAGNI; add when the UI asks).

[`docs/design/experiments.md`](design/experiments.md) is the running register — one row per experiment with verdict + link to the full writeup.

## Limitations

- Notable behaviors: `is:frenchvanilla` is catalog-backed (see Scryfall data sources above); `game:` reads the per-oracle union of every printing's `games` array; `order=color` follows the canonical WUBRG cycle (colorless → mono → 2c allied → 2c enemy → shards → wedges → 4c → 5c, encoded as `color_sort_key` 0–31 in `ingest/color_sort.py`); the projection whitelist is regen-able via `python scripts/regen_projection_whitelist.py` (samples Scryfall random cards + curated rare-field set).
- `order=usd` sorts by the **cheapest** USD printing seen at last refresh; refresh again to refresh prices.
- First-boot HNSW build runs at the end of the first refresh and adds noticeable time over the embed pass. Subsequent refreshes rebuild the index CONCURRENTLY on a scratch name and swap it in under a tiny txn — `/similar` stays available on the previous index throughout the build.
- The SentenceTransformer model loads from the local cache when present (the `hfcache` volume, `HF_HOME=/models`). HuggingFace is contacted only when the model isn't cached, normally the first start, which needs outbound HF access.
- Every API start also fetches three Scryfall keyword catalogs (`/catalog/keyword-abilities`, `/catalog/keyword-actions`, `/catalog/ability-words`); failures fall back to a heuristic.
- One row per card, not per printing. Printing-level fields that are searchable (`usd`, `set`, `year`, `released`, `artist`, `flavor`, `frame`) read the oldest printing, except `usd`, which is the cheapest. Collector numbers, foil, language, rulings and watermarks are not modelled.
- No code/number lookups and no multiverse/mtgo/arena/tcgplayer/cardmarket id lookups. `/cards/random`, `/cards/collection` and `/cards/autocomplete` are shipped.
- An interrupted combo import does not resume. If the API restarts partway through the Commander Spellbook pull (about 1 h 20 min), the combos already imported stay and nothing marks them as partial; `AUTO_REFRESH_IF_EMPTY` only fires when cards or embeddings are missing. Re-run it from **Settings → Card data → Advanced: choose which steps to run**, ticking only **Combos** (it needs card data, which is already there).
