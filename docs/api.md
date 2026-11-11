# HTTP API reference

Live, always-current schema at `http://localhost:8000/docs` (FastAPI OpenAPI).

## Endpoints

All responses are JSON. The wire body for a card is the original Scryfall payload (read verbatim from `cards.raw`) — no Pydantic wrapper, no field renaming. Errors are Scryfall-shaped: `{"object":"error","status":<int>,"code":"<slug>","details":"<msg>"}`.

### `GET /health`

DB ping + per-ingestion counts (`cards`, `oracle_tags`, `embeddings`, `combos`) + live refresh status. `200` when `SELECT 1` succeeds, `503` only when the DB ping fails. Refresh state never flips the status — orchestrators read `refresh.running` and decide policy.

```bash
curl -fsS http://localhost:8000/health
# {"status":"ok","db":"up","version":"0.1.0","cards_count":31245,"oracle_tags_count":540871,
#  "embeddings_count":31245,"combos_count":12034,
#  "refresh":{"running":true,"started_at":"…","job_id":"…","planned_stages":["ingest","tags",…],"stages":[
#    {"name":"ingest","started_at":"…","finished_at":"…","rows":31245,"error":null},
#    {"name":"embed","started_at":"…","finished_at":null,"rows":null,"error":null}
#  ]}}
```

`embeddings_count` counts `cards.embedding IS NOT NULL` — so `< cards_count` means an embed pass is mid-flight or hasn't run yet. `oracle_tags_count` and `combos_count` are `pg_class.reltuples` estimates (fast O(1) catalog lookup, never full-scan), and are `null` on pre-migration deployments (respective table absent) or pre-`ANALYZE` (`reltuples = -1`); `/health` stays `200/ok` regardless. The web UI uses `oracle_tags_count` to surface an "Oracle tags ingesting" banner while a refresh holds `oracle_tags_count = 0`.

`refresh.planned_stages` lists the stages the current (or last) run was asked to execute, so clients can show "N of M steps". `refresh.stages` records per-stage telemetry for the most recent (or in-flight) run — `ingest`, `embed`, `classify`, `hnsw`, `tags`, `combos`, `deck_cards` in execution order. Each entry carries `started_at`, `finished_at` (`null` for the stage currently running — also identifies "which stage is now"), `rows` (count reported by the stage; `null` for `hnsw` and for stages that crashed before setting it), and `error` (`null` for a clean run, `"<ExcType>: <msg>"` when the stage body raised — lets callers distinguish "ran but crashed" from "ran clean" without log-scraping). List is reset on the next `mark_started`, preserved after `mark_finished` so post-mortem timings stay visible until the next refresh.

### `GET /health/live`

Sub-second liveness probe. Fires `SELECT 1`; returns `200 {"status":"ok"}` on success, `503 {"status":"error"}` on any DB error. Body carries only `status` — no `db`/`refresh`/`cards_count`. Use for k8s liveness probes that must not block on reltuples-side stats or refresh-state locks (that's `/health`'s job).

```bash
curl -fsS http://localhost:8000/health/live
# {"status":"ok"}
```

### `POST /admin/refresh`

Triggers a bulk re-ingest. Returns `202 {"status":"started"|"already_running","job_id":"<uuid4>"}`. Work runs in a background task gated by `pg_advisory_lock(42)`; concurrent POSTs return `already_running` with the in-flight `job_id`.

```bash
curl -fsS -XPOST http://localhost:8000/admin/refresh
# {"status":"started","job_id":"7f6b…"}
```

Optional `?stages=` scopes the run to a subset (ordering fixed, unknown names → `422`):

```bash
curl -fsS -XPOST 'http://localhost:8000/admin/refresh?stages=tags,combos'
```

Valid names: `ingest`, `embed`, `classify`, `hnsw`, `tags`, `combos`, `deck_cards`. Skipping `hnsw` after `ingest` leaves the staging table populated (`hnsw` is the stage that truncates it); the next `ingest` run then double-inserts into staging and relies on the DISTINCT-ON collapse to dedupe.

The web UI exposes the same trigger in the Settings tab's **Card data** panel: an *Update card data* button, plus an *Advanced: choose which steps to run* section with a checkbox per stage and `All` / `None`. The checkboxes follow the stage dependencies — checking a stage also checks the stages it needs; unchecking a stage unchecks the ones that depend on it. Full-selection posts with no query string (server default); a subset sends `?stages=…`. The banner shows `started` / `already_running` + `job_id` and the existing `/health` poll surfaces per-stage progress.

Combos ingest downloads Commander Spellbook's bulk file (`variants.json.gz`, ~32 MB gzipped, ~120k variants) in one GET, streams it with `ijson` (about 50 MB peak instead of 2.3 GB for a whole-file `json.loads`) and upserts in batches of 5,000 with `ON CONFLICT (id) DO UPDATE` (non-`id` columns are overwritten so Spellbook edits to existing combos land), each in its own transaction, so combos become queryable during the run. After the last batch a single `DELETE FROM combos WHERE NOT (id = ANY(seen))` reconciles the table with the source. A source with no `variants` (for example a stale URL pointing at the paginated `/variants/` API) raises, and the best-effort stage keeps the previous combos.

### `GET /cards/:id`

Single card by Scryfall UUID. Returns `raw` verbatim. `404 not_found` on miss; `422` on a non-UUID path param. Always fresh: `Cache-Control: no-cache`, no `ETag`, never `304`.

```bash
curl -fsS http://localhost:8000/cards/56ebc372-aabd-4174-a943-c7bf59e5028d
```

### `GET /commanders/suggest?tags=<slug,slug>&colors=<ci>`

Commander discovery. Filter `is_commander = true`; optional `colors` (WUBRG string) enforces CI-subset; optional comma-separated `tags` require every Scryfall Tagger slug to be present. Ordered by `edhrec_rank ASC NULLS LAST`. Unknown tag slugs silently return zero rows (agents commonly guess slugs).

```bash
curl -fsS 'http://localhost:8000/commanders/suggest?colors=RG&limit=10'
curl -fsS 'http://localhost:8000/commanders/suggest?tags=sacrifice-outlet,graveyard-recursion&colors=BG'
```

Response is a Scryfall `List` envelope. `has_more` is `len(data) == limit`. Errors: malformed `colors` → `422 bad_request`; `limit` out of `[1, 175]` → 422.

### `GET /cards/random?q=<optional dsl>`

Random card, optionally satisfying a DSL filter. Not cached by design.

```bash
curl -fsS 'http://localhost:8000/cards/random'
curl -fsS 'http://localhost:8000/cards/random?q=t:planeswalker'
curl -fsS 'http://localhost:8000/cards/random?q=c:g%20cmc%3C=3%20f:commander'
```

Errors: `q > 1000` chars → `422 bad_request "q too long"`; DSL parse error → `422 bad_request`; rejected (unsupported) key → `422 unsupported_operator`; `q` narrows to zero rows or `cards` is empty → `404 not_found`.

### `GET /tags?prefix=<slug>&limit=50`

Oracle-tag slug catalog. Backs the `otag:` DSL predicate — agents use this to autocomplete slugs (e.g. `boardw` → `boardwipe`) instead of guessing.

```bash
curl -fsS 'http://localhost:8000/tags?limit=10'
curl -fsS 'http://localhost:8000/tags?prefix=sacrifice'
```

Response is a Scryfall `List` envelope of `{slug, count}` sorted by count DESC. Weak ETag keyed on the refresh epoch — `If-None-Match` short-circuits to 304. `limit` clamped `[1, 500]`.

### `GET /keywords?prefix=<kw>&limit=50`

Scryfall keyword catalog with hit counts. Fans `raw->'keywords'` via `jsonb_array_elements_text`. Backs the `keyword:` DSL predicate.

```bash
curl -fsS 'http://localhost:8000/keywords?limit=10'
curl -fsS 'http://localhost:8000/keywords?prefix=fly'
```

Response envelope + cache posture mirror `/tags`.

### `GET /sets`

Full set catalog with `{code, name, released_at, count}`. Ordered by release date DESC. No prefix / limit knobs — bounded universe (<1k sets), one payload.

```bash
curl -fsS 'http://localhost:8000/sets' | jq '.data[:5]'
```

### `GET /cards/autocomplete?q=<fragment>`

Scryfall-parity name autocomplete. Prefix hits (case-insensitive `LOWER(name) LIKE :prefix`) rank first, then `pg_trgm` word-similarity for typo tolerance. Returns up to 20 names in a Scryfall `catalog` envelope.

```bash
curl -fsS 'http://localhost:8000/cards/autocomplete?q=light'
# {"object":"catalog","total_values":20,"data":["Lightning Bolt","Lightning Helix",...]}
```

Errors: empty/missing `q` → `422 bad_request`; `q > 100` chars → `422 bad_request "q too long"`.

### `POST /cards/search/validate`

DSL preflight — parses `q` through the same pipeline as `/cards/search` without touching the DB. Success → `{ok: true, ast_summary, applied_default_filter}`. Failure → 422 with `did_you_mean` populated when the failing key is a typo of a supported or deliberately rejected key.

```bash
curl -fsS -XPOST http://localhost:8000/cards/search/validate \
  -H 'content-type: application/json' -d '{"q":"typ:instant c:r"}'
# {"object":"error","status":422,"code":"unsupported_operator",
#  "message":"unsupported operator in q","details":"unsupported operator: typ",
#  "did_you_mean":["t","type"]}
```

### `POST /cards/collection`

Scryfall-parity bulk lookup — resolve up to 75 cards in one round-trip. Body carries any mix of `id` / `oracle_id` / `name` (case-insensitive) identifiers; response `data` preserves the request order, `not_found` echoes the exact identifier shape for misses.

```bash
curl -fsS -XPOST http://localhost:8000/cards/collection \
  -H 'content-type: application/json' \
  -d '{"identifiers":[{"name":"Sol Ring"},{"name":"Lightning Bolt"},{"id":"nope-nope-nope-nope-nope"}]}'
# {"object":"list","data":[...],"not_found":[{"id":"nope-nope-nope-nope-nope"}]}
```

Non-goal: printing-level identifiers (`set + collector_number`, `multiverse_id`, `mtgo_id`, `arena_id`) — Biblioplex stores one row per card, not per printing. Over-cap body (>75 identifiers) → 422.

### `POST /cards/tag-counts`

Per-tag card counts across a set of cards, in one aggregate query. Body `{"names": [<str>, …]}` (case-insensitive, ≤1000). Resolves the names to `oracle_id`s and returns `{"object":"tag_counts","counts":{"<tag>": <n>, …}}` where `n` is the number of distinct named cards carrying that oracle tag. Tag hierarchy is flattened (matches `otag:` and `/cards/:id/tags`), and reprints collapse. Empty `names` → `{"counts":{}}` with no DB round-trip. Backs the Deck Building tab's theme chips (how many deck cards serve each theme).

```bash
curl -fsS -X POST localhost:8000/cards/tag-counts \
  -H 'content-type: application/json' \
  -d '{"names":["Krenko, Mob Boss","Goblin Matron"]}'
# {"object":"tag_counts","counts":{"goblin synergy":2,"tribal-goblin":2,…}}
```

### `GET /cards/named?exact=<name>`

Case-insensitive equality on the top-level `name` (`LOWER(name) = LOWER(:name)`).

```bash
curl -fsS 'http://localhost:8000/cards/named?exact=Lightning%20Bolt'
```

### `GET /cards/named?fuzzy=<name>`

pg_trgm best-match against `name_sort_key`. Accepts the top result if `similarity >= 0.4`, else `404`.

```bash
curl -fsS 'http://localhost:8000/cards/named?fuzzy=lightning+blot'
```

Exactly one of `exact`/`fuzzy` is required. Both or neither → `400 bad_request` ("exactly one of exact|fuzzy required") before any DB round-trip.

### `GET /cards/search?q=<dsl>`

DSL search (see [search-syntax.md](search-syntax.md)); returns a Scryfall `List` envelope, 175 results/page. Always fresh: `Cache-Control: no-cache`, no `ETag`, never `304`, so collection and wishlist changes show in the next search.

Params: `q` (required, ≤1000 chars); `order=name|cmc|power|toughness|released|color|edhrec|rarity|usd|relevance` (default `name`); `dir=auto|asc|desc` (auto → desc for `released`/`usd`/`relevance`, asc otherwise); `page>=1`; `unique=cards|prints|art` (all alias to `cards`); `projection=<comma-list>`. Always tie-broken by `id ASC`. Primary order is `NULLS LAST` so unranked/unpriced/non-numeric power cards sink regardless of direction. `order=relevance` sorts by trigram word-similarity between the query's bare-word terms and `name_search` — best match first; filter-only queries (no bare-word terms) fall back to `name_sort_key asc`.

```bash
curl -fsS 'http://localhost:8000/cards/search?q=t:dragon+c:r&order=cmc'
```

Errors: empty/missing `q` → `422 q is required`; `q>1000` chars → `422 q too long`; `format=csv|text` → `400 unsupported_format`; parse failure / unsupported operators → `422`; `total_cards == 0` → `404 not_found`.

When `SEARCH_DEFAULT_FILTER` is set, successful responses carry an `X-Applied-Filter` header echoing the deployment-side DSL fragment that got AND-ed onto `q` — so agents/operators can see the shadow without diffing the raw URL against the rendered query.

### `GET /cards/:id/similar?limit=<n>`

Hybrid nearest-neighbour: HNSW vector top-K over `cards.embedding` (cosine), then re-rank by a weighted blend of six signals so substitutes outrank look-alike-but-unplayable matches. Over-fetch factor `HYBRID_OVERFETCH=5`; weights (sum ≈ 1.0): `HYBRID_W_VECTOR=0.45` (cosine), `HYBRID_W_LEXICAL=0.10` (`pg_trgm` over `oracle_search`), `HYBRID_W_COST=0.10` (`1/(1+|Δcmc|)`), `HYBRID_W_COLOR=0.15` (Jaccard over `color_identity`, 0.5 floor when both colorless), `HYBRID_W_TYPE=0.05` (exact `type_search` match = 1.0, supertype = 0.5, else 0.0), `HYBRID_W_TAGS=0.15` (Jaccard over Scryfall `oracle_tags`, the functional-intent labels). The pure-vector top-K alone misses same-effect / different-wording pairs (Iron Man / Repurposing Bay; Rhystic Study / Smothering Tithe); the structured rerank pulls them back up while pushing off-color / wrong-cmc / wrong-mechanic matches down. `limit` is `[1, 100]`, default `20`. Each entry's `raw` is decorated with `similarity = 1 - distance` before send (pure cosine, not the blended score). `404 not_found` when the base card is missing or has no embedding yet.

```bash
curl -fsS 'http://localhost:8000/cards/56ebc372-aabd-4174-a943-c7bf59e5028d/similar?limit=5'
```

### `GET /cards/:id/tags`

Oracle tags (Scryfall Tagger) for the card's `oracle_id`. Returns `{"object":"list","data":["removal","instant-speed-removal",…]}`. Empty `data` when the card exists but has no tags; `404 not_found` when the id is unknown. Hierarchy is flattened at ingest so the list includes every ancestor whose subtree covers this card — same set the `otag:` predicate matches against.

```bash
curl -fsS http://localhost:8000/cards/56ebc372-aabd-4174-a943-c7bf59e5028d/tags
```

### `GET /cards/:id/explain`

Per-label rules-classifier explainability. Returns `{"object":"explain","labels":{"<label>":{"score":<float>,"rules":[{"id":"<slug>","why":"..."}]}}}` for the five template labels. `rules` lists every rule pattern that fired against this card's `oracle_text` (unioned across faces). Empty `rules` for `plan` is normal — `plan` is a residue label, never a rule hit. Sourced from `label_scores` + `label_reasons` written during refresh; unknown rule ids fall back to the slug for `why`. Always fresh: `Cache-Control: no-cache`, no `ETag`. `404 not_found` when the card is unknown.

```bash
curl -fsS http://localhost:8000/cards/56ebc372-aabd-4174-a943-c7bf59e5028d/explain
```

In-place ALTER recipe for existing deployments (add `label_reasons jsonb NOT NULL DEFAULT '{}'::jsonb`):

```sql
ALTER TABLE cards ADD COLUMN IF NOT EXISTS label_reasons jsonb NOT NULL DEFAULT '{}'::jsonb;
```

The next refresh's classify pass repopulates it.

### `PUT` / `DELETE /cards/:id/labels` — manual label override

The rules classifier is deliberately strict and has known gaps, so a card can be miscategorised (e.g. a cost reducer that isn't tagged `ramp`, or card selection that isn't tagged `card_advantage`). `PUT` pins a correction: body `{"labels": ["ramp", ...]}` (any subset of the five slot labels; `[]` pins the card to no slot) replaces `predicted_labels` and every consumer — `/decks/analyze` slot counts, the DSL `pred:` clause, the deck-builder review — picks it up immediately. The correction is recorded under the reserved `_override` key of `label_reasons` so the next refresh's classify pass **carries it forward** instead of overwriting it. `DELETE` clears the override and reverts to the rules verdict. Both return the decorated card (with `labels_overridden: true|false`); `422` on an unknown label, `404` when the card is unknown. The web card-detail pane exposes this as an inline "edit" control on the Predicted labels section.

```bash
curl -fsS -X PUT http://localhost:8000/cards/<id>/labels \
  -H 'content-type: application/json' -d '{"labels":["card_advantage"]}'
curl -fsS -X DELETE http://localhost:8000/cards/<id>/labels
```

No schema change: the override rides in the existing `label_reasons` jsonb column.

### `GET /cards/:id/combos`

Commander Spellbook combos containing this card's `oracle_id`. Returns `{"object":"list","data":[{"id":"<spellbook-id>","cards":["Card A","Card B"],"card_ids":["<uuid>","<uuid>"],"prereqs":"...","steps":"...","results":["Infinite mana"]}]}`. `card_ids` mirrors `cards` order — lets clients (e.g. the web UI) link each combo participant back to `/cards/:id` without a name lookup. Empty `data` when the card exists but no ingested combo references it (or a deployment with `COMBOS_SOURCE_URL` set empty — the `combos` table is empty). `404 not_found` when the card is unknown. Always fresh: `Cache-Control: no-cache`, no `ETag`.

```bash
curl -fsS http://localhost:8000/cards/56ebc372-aabd-4174-a943-c7bf59e5028d/combos
```

### `GET /cards/:id/related`

Resolves every entry in `cards.all_parts` (Scryfall's per-printing related-card list — tokens, meld halves, adventures, combo pieces) against our name index and hands back canonical card ids for the collapsed printings we serve. Each entry: `{"component":"token|meld_part|meld_result|combo_piece","name":"...","type_line":"...","card":<full card object or null>}`. `card` is `null` when the referenced name isn't in our corpus (unimplemented token type, etc.) — still surface `component` + `name` so agents can render "combo piece: <name>" even for unhydrated related cards. Empty `data` when the card exists but has no related parts; `404 not_found` when the id is unknown.

```bash
curl -fsS http://localhost:8000/cards/56ebc372-aabd-4174-a943-c7bf59e5028d/related
```

### `GET /combos/search`

Paginated combo browse over the ingested `combos` table. Filters (all optional, ANDed):

* `card=<name>` (repeatable) — case-insensitive exact match; every listed card must belong to the combo.
* `ci=<WUBRG>` — subset match: only combos whose color identity fits within the selected letters (Spellbook default). Empty string = five-color allowed. Invalid letters → `400 bad_request`.
* `result=<substr>` — ILIKE against any produced result (`Infinite mana`, `Win the game`, …).
* `q=<substr>` — ILIKE against combined `prereqs` + `steps` prose.
* `page`, `page_size` — 1-indexed; `page_size` capped at 100 (default 25).
* `sort=id|cards` — stable id (default) or ascending card count.

Response `{"object":"list","data":[{...combo...}], "total": <int>, "page": <int>, "page_size": <int>, "has_more": <bool>}`. Each combo carries `id`, `cards`, `card_ids`, `color_identity`, `prereqs`, `steps`, `results`, `card_count`.

```bash
curl -fsS 'http://localhost:8000/combos/search?ci=UR&result=infinite+mana&page_size=10'
```

### `GET /combos/:id`

Fetch a single combo by its Spellbook id. `404 not_found` when unknown.

```bash
curl -fsS http://localhost:8000/combos/spellbook-1234
```

### `GET /cards/:id/substitutes?slot=<label>`

Slot-constrained variant of `/similar` (same hybrid rerank: HNSW top-K + cosine + lexical + cost + color + type + tag Jaccard). Adds a hard filter so every returned card carries the requested predicted-label slot — useful for "swap this card without changing its role in the deck". `slot` must be one of `ramp`, `card_advantage`, `targeted_disrupt`, `mass_disrupt`, `plan`; unknown slugs `422 unsupported_operator`. `limit ∈ [1, 100]` default `20`. Missing target / NULL embedding → `404 not_found`.

```bash
curl -fsS 'http://localhost:8000/cards/56ebc372-aabd-4174-a943-c7bf59e5028d/substitutes?slot=ramp&limit=5'
```

### `POST /inventory/sync/deckbox`

Sync the owned-card inventory from a [Deckbox](https://deckbox.org) set. Body `{"url"?, "set_id"?, "csv_text"?, "mode"?}` — supply a set URL, a bare set id, or raw CSV text. With **no** body fields, the previously saved Deckbox URL is re-fetched.

`mode` defaults to `replace` (the Deckbox export is the snapshot of truth; every `source = 'deckbox'` row is cleared first). `merge` clears only the oracle ids present in the payload and leaves the rest of the inventory alone. Rows written by `POST /inventory/cards` carry `source = 'manual'` and survive both modes.

> **Automated URL fetch does not currently work.** Deckbox serves `/sets/<id>/export?format=csv` to anonymous callers as an HTTP 200 **HTML login page**, so a set cannot be exported without a logged-in session — verified against a real public set. The endpoint detects that and returns `422 bad_request` rather than syncing zero cards (which, under `mode: replace`, would delete your inventory). Until this is resolved, export the CSV from the Deckbox UI and pass it as `csv_text`, or upload it — `POST /inventory/sync/upload` (below) or the Inventory tab's file picker.

Names are resolved to `cards.oracle_id` by case-insensitive exact match, with a front-face fallback for split cards (`Fire // Ice` → `Fire`). Unresolved names come back under `unmatched` rather than failing the sync; CSV-level problems (bad `Count` values) come back under `warnings`.

```bash
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"url": "https://deckbox.org/sets/3293466"}' \
  http://localhost:8000/inventory/sync/deckbox
# {"object":"inventory_sync","mode":"replace","total_cards":1284,
#  "distinct_cards":973,"unmatched":[],"synced_at":"2026-08-17T10:00:00+00:00","warnings":[]}
```

Errors: no URL / set id / CSV text and no saved URL → `422 bad_request`; a fetched body that is not CSV → `422 bad_request`; an unparseable CSV (no header, no `Name` column) → `422 bad_request` **before** any write; Deckbox unreachable → `502 bad_gateway`.

### `POST /inventory/sync/upload`

Multipart upload of a Deckbox CSV export. Same parser, same name resolution, same `replace` / `merge` semantics as `/inventory/sync/deckbox` — only the transport differs. **This is the automation path**, because Deckbox requires a logged-in session to export a set by URL (see the note above).

```bash
# export the CSV once from the Deckbox UI, then:
curl -fsS -X POST -F 'file=@collection.csv' -F 'mode=replace' \
  http://localhost:8000/inventory/sync/upload

# crontab: re-apply a CSV that some other job refreshes
0 4 * * * curl -fsS -X POST -F 'file=@/srv/deckbox/collection.csv' \
  http://localhost:8000/inventory/sync/upload
```

`mode` defaults to `replace`. Errors: an unparseable file (HTML, no header, no `Name` column) → `422 bad_request` **before** any write. Non-UTF-8 bytes are replaced rather than rejected, so one bad byte cannot fail a 3000-line collection.

### `GET /inventory?q=<name>&page=<n>&limit=<n>`

Paginated listing of owned cards, joined to `cards` for `price_usd` / `type_line` / the full Scryfall payload. `q` is a case-insensitive substring filter on the stored card name. `limit` is capped at 200.

### `GET /inventory/summary`

`{"object":"inventory_summary","total_cards":…,"distinct_cards":…,"total_value_usd":…,"last_synced_at":…,"deckbox_url":…}`.

`total_value_usd` is `SUM(quantity * COALESCE(source_price_usd, cards.price_usd))`. The Deckbox export reports a price for the printing you actually hold, and that is stored per row at sync time; `cards.price_usd` is only the fallback for rows with no export price. This matters more than it sounds: `cards.price_usd` is the **cheapest printing across the whole oracle**, so valuing a collection through it under-reports badly — a `Rhystic Study (wot)` worth $81 would be counted at the price of its cheapest reprint. Prices are a snapshot from the last sync rather than live market data; re-sync to refresh them.

### `GET /inventory/oracle-ids`

`{"object":"list","data":["<uuid>", …]}` — just the distinct oracle ids owned in a positive quantity, no card payloads and no pagination. The web UI fetches it once and badges a whole page of search results locally; without it, "do I own this?" would be one request per tile.

### `POST /inventory/cards`

Manual per-card override. Body `{"oracle_id": "<uuid>", "quantity": <int>, "foil_quantity"?: <int>}`. `quantity: 0` (with `foil_quantity: 0`) deletes the row. Unknown `oracle_id` → `404 not_found`.

### `DELETE /inventory`

Drops every inventory row and the saved sync settings. No body, no confirmation token.

### `POST /wishlist/sync/upload`

Multipart upload of a Deckbox **wishlist** CSV export. Same parser, name resolution and `replace` / `merge` semantics as the inventory sync — a wishlist is just another Deckbox set. `POST /wishlist/sync` takes the same content as JSON `{"csv_text": …, "mode": …}`.

Wishlist rows live in their own `wishlist` table, never in `inventory`. Wanting a card is never mistaken for owning it: `is:owned` and deck coverage read `inventory` alone.

```bash
curl -fsS -X POST -F 'file=@wishlist.csv' http://localhost:8000/wishlist/sync/upload
# {"object":"wishlist_sync","mode":"replace","total_cards":41,"distinct_cards":38,
#  "unmatched":[],"synced_at":"2026-08-17T10:00:00+00:00","warnings":[]}
```

Errors: an unparseable file → `422 bad_request` **before** any write.

### `GET /wishlist?q=<name>&page=<n>&limit=<n>`

Paginated wishlist listing. Each row carries `owned_quantity` — how many of that card the inventory already holds — so a client can flag entries you have since acquired.

### `GET /wishlist/summary`

`{"object":"wishlist_summary","total_cards":…,"distinct_cards":…,"total_value_usd":…,"owned_distinct_cards":…,"last_synced_at":…}`. `owned_distinct_cards` is the "you can cross these off" number. `last_synced_at` is `max(updated_at)` over the rows — the wishlist needs no companion settings table.

### `GET /wishlist/oracle-ids`

Flat list of wanted oracle ids, mirroring `/inventory/oracle-ids`. Badges a page of search results in one request.

### `POST /wishlist/cards`

Manual per-card override: `{"oracle_id": "<uuid>", "quantity": <int>}`. `quantity: 0` deletes the row. Unknown `oracle_id` → `404 not_found`.

### `DELETE /wishlist`

Drops every wishlist row.

### `GET /uploads`

History of every CSV applied through a sync endpoint, newest first, with the counts each import produced. `?kind=inventory|wishlist` filters; `?limit=` caps at 200. File content is **not** included — browsing history stays cheap.

### `GET /uploads/:id/download`

Streams the stored file back byte-for-byte as `text/csv` with a `Content-Disposition` attachment name, so a past import can be re-applied or diffed against a newer export. Unknown id → `404 not_found`.

### `DELETE /uploads/:id`

Removes one file from the history. Does not touch the inventory or wishlist rows it produced.

### `POST /decks`

Stores a deck. Body `{decklist, name?, format?, commander?, companion?, notes?, deckbox_url?, moxfield_url?, archidekt_url?, branch?, message?}`.

`branch` (default `main`) is a git-like label — `main` / `deckbox` / `moxfield` / `archidekt` — this version lands on; `message` is an optional free-text note. See "Deck version branches" below.

`decklist` accepts a plaintext decklist **or** a pasted Deckbox CSV / print-view HTML export — the format is sniffed from the content, never a filename. Section headings (`Commander`, `Companion`, `Deck` / `Mainboard`, `Sideboard`, Arena's `About` block) are understood, so the commander, the companion and the deck name are picked up from the export when it carries them. `Maybeboard` and `Tokens` sections are dropped — neither is part of the deck.

Deck content is **versioned**: this writes version 1, and every later change appends another (see `GET /decks/:id/versions`). Storage keeps the decklist as text, so the `decklist` field round-trips — POST it straight back to `/decks/analyze` or `/decks/legality` unchanged.

The three service URLs are validated by hostname; a Moxfield link in `archidekt_url` is a `422`, because that field is what an automated pull would later fetch.

```bash
curl -fsS -X POST localhost:8000/decks -H 'content-type: application/json' \
  -d '{"name":"Iroh gift","decklist":"Commander\n1 Iroh, Grand Lotus\n\nDeck\n1 Sol Ring\n"}'
```

Errors: `422 bad_request` for an empty or over-long (>64000 char) decklist, or a service URL on the wrong host.

### `POST /decks/:id/versions`

Import a new version **onto an existing deck** — the control the branch model in "Deck version branches" below was built for. Multipart form (`file=@deck.txt`), same as `POST /decks/upload`: `to_decklist` coerces the content, then `branch=` / `message=` / optional `source=` (defaults `upload`, mirrors `deck_versions.source`) go through the identical append machinery every write path shares — per-branch dedup, head auto-follow, `head_pinned`, and the `deck_cards` rebuild all apply unchanged. Original bytes are retained for a later exact-file download.

```bash
curl -fsS -X POST localhost:8000/decks/$ID/versions \
  -F 'file=@moxfield-export.txt' -F 'branch=moxfield' -F 'message=monthly resync'
```

Pasting a new version onto an existing deck is a separate, already-shipped path — `PATCH /decks/:id` with a `decklist` body — not duplicated here.

### `GET /decks/completion-plan`

What to buy next to finish whole decks. Optimises **decks completed** within a budget, not cards-per-dollar: a card five decks want that finishes none of them buys nothing you can sleeve up.

Returns **two** plans, neither of them the default, because they diverge sharply. On a real 62-deck library at `budget=10` both complete 3 decks — `by_cost` buys **20 cards for $9.27**, `by_cards` buys **5 for $8.14**.

```bash
curl -fsS 'localhost:8000/decks/completion-plan?budget=10'
curl -fsS 'localhost:8000/decks/completion-plan?max_cards=6'
```

Both `budget` and `max_cards` are optional and compose; with neither, the plan orders every completable deck.

Costs are **marginal**, not per-deck sums: each step is priced against what earlier steps already bought, and one bought copy satisfies every deck wanting one (the same shared-inventory model `coverage_pct` uses). A duplicated deck therefore completes for `$0.00`, where adding up per-deck `missing_usd` would overstate.

A deck whose gap includes an unpriced card cannot be ranked by cost. `by_cost` reports it under `skipped_unpriced` rather than dropping it silently; `by_cards` **can** rank it — card counts need no prices — and flags its total with `spend_usd_is_floor`, since a partial sum shown as exact is misleading.

Greedy, and the response says so in `note`: budgeted set cover with overlapping sets is NP-hard, and a different combination may complete more decks for the same money.

Auto-registered as MCP tool `get_deck_completion_plan`.

### `POST /decks/:id/fork`

Fork a deck into a NEW, independent deck seeded at version 1 from the source's **HEAD** — the content it currently shows, which differs from its newest version whenever head is pinned (`POST /decks/:id/head`) or `trunk_branch` has kept it off the branch most recently imported onto.

A fork is for a build you playtest independently and never reconverge — a `budget` or `pre-rotation` variant. That is what a branch is wrong for and a separate deck is right for, which is why `deck_versions.branch` stays a fixed four-value enum instead of growing free-form names.

```bash
curl -fsS -X POST localhost:8000/decks/$ID/fork \
  -H 'content-type: application/json' -d '{"name": "Budget Iroh"}'
```

`name` is optional and defaults to the source's name with ` (fork)` appended.

Inherits `format` / `commander` / `companion` / `folder`. Does **not** inherit `deckbox_url` / `moxfield_url` / `archidekt_url`: the fork is not that remote deck, and copying the link would point a second local deck at the same remote, so a later sync could overwrite the wrong list. Does **not** copy version history — the fork starts its own timeline at version 1 with `source = 'fork'`, because carrying the source's past would make the two decks' version numbers describe different timelines while looking like one. `trunk_branch` is not inherited either: a fresh fork has only ever written branch `main`, so a multi-branch head policy has nothing to apply to yet.

The source deck is completely untouched — same version count, same head, same coverage.

Errors: `404 not_found` for an unknown or malformed source id.

Errors: `404 not_found` for an unknown deck; `422 bad_request` for an empty/over-long decklist, an unknown `branch`, or an unknown `source`.

### `POST /decks/upload`

Multipart form of the same thing (`file=@deck.txt`). Accepts every shape in `data/example_deck_exports/` — Moxfield plain / Arena / MTGO / set-code variants, Archidekt, and Deckbox txt, CSV and print-view HTML. The original bytes are kept on the version so a later download returns the exact file that was imported. `branch=` and `message=` form fields work the same as `POST /decks`.

```bash
curl -fsS -X POST localhost:8000/decks/upload -F 'file=@my-deck.txt' -F 'name=Big Boys'
```

### `GET /decks`

Deck library, newest-touched first. `?q=` filters on name, `?page=` / `?limit=` paginate (limit caps at 200). Rows carry the current version number, its card counts and the effective commander and companion, but **not** the decklist body — browsing a library should not ship every deck in full.

`?folder=` filters to one folder path, and `?include_subfolders=true` widens that to everything beneath it. Passing the parameter **empty** (`?folder=`) lists only unfiled decks — that is a different request from omitting it, which applies no folder filter at all.

`?sort=` accepts `updated` (newest-touched first, the default), `name`, `coverage` — how much of each deck you already own — or `missing_cost`, the rough price of the cards it still needs. `?direction=asc|desc` overrides the per-key default (coverage and updated descending, name ascending); ascending coverage answers "what do I still need to buy?". Unknown key → `422 bad_request`.

Coverage sorts on the **ratio**, not the raw owned count, so a fully-built 40-card deck outranks a 60-of-100 one. Ownership is clamped per card, so owning five copies of a singleton contributes one and coverage cannot exceed 100%. Decks with no indexed cards sort last under either direction — "not indexed yet" is never the best answer to "which deck is closest to built?".

Every row carries `owned_cards` and `coverage_pct` regardless of sort (`coverage_pct` is `null`, not `0`, for a deck the derived index has not covered yet). Backed by `deck_cards` — inventory is folded once per request rather than per deck.

### Missing-card cost

Every deck row carries `missing_usd` (rough cost of the cards it still needs) and `unpriced_missing`. `GET /decks/:id/cards`'s summary carries the same pair.

Price is `cards.price_usd` — the **cheapest printing across the whole oracle** — so the figure answers "roughly what would it cost to finish this deck", not "what is my exact shopping basket". Ownership is clamped per card, so owning extras never produces a negative.

**Unpriced cards are counted, not charged at zero.** About 5% of the corpus has no `price_usd`, so the total sums only priced cards and `unpriced_missing` travels beside it. A deck missing three unpriced staples would otherwise read as cheap with nothing on screen saying the figure is partial. Render both.

`?sort=missing_cost` ascends by default — cheapest-to-finish first, deliberately opposite to `coverage`'s descending default. `missing_usd` is `null` (not `0`) for a deck the derived index has not covered yet.

Prices are as fresh as the last `make refresh`; they come from the Scryfall bulk ingest, not live lookups.

### `GET /decks/folders`

Every folder path in the library with `deck_count` (decks filed directly in it), `total_deck_count` (including descendants), `name`, `depth`, plus a top-level `unfiled_count`.

Ancestors are **derived, not stored**: a deck filed at `Commander/Budget` yields a `Commander` node even when no deck sits directly in it, so a client can build a tree without inferring missing parents.

```bash
curl -fsS http://localhost:8000/decks/folders
# {"object":"list","unfiled_count":2,"data":[
#   {"path":"Commander","name":"Commander","depth":0,"deck_count":0,"total_deck_count":3},
#   {"path":"Commander/Budget","name":"Budget","depth":1,"deck_count":3,"total_deck_count":3}]}
```

### `POST /decks/folders/rename`

Moves a folder and every deck beneath it in one statement. Body `{"from": "Commander", "to": "Legacy"}`; descendants keep their relative path, so `Commander/Budget` becomes `Legacy/Budget`. Omitting `to` **unfiles** the subtree — which is also how a folder is deleted, since there is no folder row to remove.

Renaming an unknown folder is not an error; it reports `decks_moved: 0`. Invalid or missing `from` → `422 bad_request`.

Matching is exact-or-slash-anchored: renaming `Commander` never touches a sibling named `CommanderX`. `%` and `_` are escaped, so a folder legitimately called `50%_budget` cannot match half the library.

`updated_at` is deliberately **not** bumped — a bulk reorganisation is not per-deck activity, and moving it would reshuffle the whole newest-touched-first library listing for a purely organisational change.

```bash
curl -fsS -X POST http://localhost:8000/decks/folders/rename \
  -H 'content-type: application/json' -d '{"from":"Commander","to":"Legacy"}'
# {"object":"folder_renamed","from":"Commander","to":"Legacy","decks_moved":3}
```

### Deck folders

A folder is a `/`-separated path string on the deck (`decks.folder`), not a table. A folder has no independent existence — it *is* the set of decks sharing a path prefix — so a folder cannot be orphaned and a cycle cannot be constructed. `NULL` means unfiled.

Paths are normalised on write: segments trimmed, repeated and edge separators collapsed, anything normalising to nothing stored as `NULL`. Control characters and paths over 256 characters are rejected with `422 bad_request`.

Case is **preserved**, so `Commander` and `commander` are two folders. That is the priced cost of the column approach — folding case would surprise anyone who capitalises deliberately.

### `GET /decks/:id`

One deck plus the decklist text of its **HEAD** version — the pinned version when `head_pinned: true`, otherwise the newest one. `commander` and `companion` are the effective values: the explicit override when set, otherwise whatever the decklist's `Commander` / `Companion` section names (partners joined by ` // `). The raw overrides are also returned as `commander_override` / `companion_override`, so a client can tell "set by hand" from "derived from the file". Unknown or malformed id → `404 not_found`.

Commander and companion cards are counted in the deck: `total_cards` for a 100-card Commander deck is 100, not 99.

### `GET /decks/:id/evaluate`

Grades a saved deck's HEAD decklist against the [`docs/edh-guide.md`](edh-guide.md) template and returns a `summary` plus an ordered list of improvement `tips`. Composes `deck_service.get_deck` + the `/decks/analyze` pipeline on one connection; the tips engine (`src/biblioplex/decklist/evaluate.py`) is a pure function reused by the Decklist wizard.

`summary` carries `total_cards`, `lands`, `nonland`, `curve_peak`, `coverage_pct` (when an inventory is synced) and per-slot `{count, target, delta}`. Each tip is `{code, severity, message}`: `severity` is `warn` for a hard template gap (lands below 38, a functional slot under target) or `info` for curve shaping (off-peak, 5-drop allergy), light plan, and partial ownership. The full `/decks/analyze` payload rides along under `analysis` so a client renders the curve and slot bars without a second call. `404 not_found` for an unknown/malformed id. Auto-registered as MCP tool `evaluate_deck`.

```bash
curl -fsS localhost:8000/decks/$ID/evaluate | jq '{summary, tips}'
```

### `PATCH /decks/:id`

Patches any subset of the fields. Supplying `decklist` appends a version with `source: "edit"` onto `branch` (default `main`, optional `message`) — unless the text is identical to that branch's own latest, which is a no-op returning the existing version so re-saving does not junk up the history. `version_created` says which happened; `imported_version` is the version this write actually produced (or deduped onto), which can differ from the top-level `version` (HEAD) when the deck is pinned somewhere else — see "Deck version branches" below. `diff_bases` is present whenever `decklist` was supplied.

### `DELETE /decks/:id`

Deletes the deck and every version of it (`ON DELETE CASCADE`). Not recoverable.

### `GET /decks/:id/cards`

The deck as a table rather than a text blob. One row per distinct card with `section` (`commander` / `main` / `sideboard`), `qty`, the card `id` for linking to `/cards/:id`, `type_line`, `mana_cost`, `cmc`, `price_usd`, and quantity-aware ownership — `qty_owned`, `qty_missing`, `on_wishlist`.

Rows are **flat with a `section` field**, not grouped, so a client can sort the whole deck by ownership. Unlike `POST /decks/analyze` — which reads the mainboard only — the **sideboard is included**, so a deck's own card table never silently omits part of it.

Ownership sums across printings: `inventory` holds one row per printing, so a card owned in two editions counts once at its combined quantity. Cards whose names resolve to nothing still appear with `id: null` and `resolved: false`, so the table always agrees with the deck's card count instead of inflating coverage.

Reads the derived `deck_cards` index, so nothing re-parses the decklist per request — but `deck_cards` only ever reflects **HEAD**. `?version=N` reads a specific version's table instead, computed **live** from that version's decklist rather than from the index; it never touches `deck_cards` or `head_version`, so it is a pure read even when `N` is not the current head. Powers the web branch-head navigator, which views another branch's own latest version without promoting it.

When `?version=N` is given, the response also carries top-level `commander` / `companion` — the **effective** values for that specific version, computed the same way `GET /decks/:id` computes HEAD's: the deck's `commander`/`companion` override when set, else parsed from that version's own decklist. An override is a property of the deck, not of a branch, so it reads identically no matter which version is being viewed; only a derived value tracks the branch. Omitted when `version` is not given — the caller already has HEAD's values from `GET /decks/:id`.

The summary carries both money figures. `total_usd` is what the **whole deck** is worth — every section, including cards you already own — while `missing_usd` is only what the gap would cost. Both are quantity-weighted, so a playset counts four times.

There is deliberately **no `owned_usd`**: it is exactly `total_usd - missing_usd`, and a third field could drift out of agreement with the two it derives from.

`unpriced_total` and `unpriced_missing` count cards with no price at all, so a `$0.00` figure never silently reads as "free" when the truth is "unknown" — about 5% of the corpus has no `price_usd`.

```bash
curl -fsS http://localhost:8000/decks/$ID/cards | jq .summary
# {"total_cards":100,"owned_cards":56,"missing_cards":44,
#  "distinct_cards":100,"unresolved_cards":0,"coverage_pct":56.0,
#  "missing_usd":396.94,"unpriced_missing":0,
#  "total_usd":702.11,"unpriced_total":0}
```

### `GET /decks/:id/versions`

Newest-first history of every state the deck's decklist has been in — imports **and** manual edits, so a diff between any two points is complete. Each row carries its `branch`, its `message`, its `source` (`upload` / `paste` / `url` / `edit` / `restore`), the originating filename when there was one, and the card counts that version produced. Bodies are excluded. `?limit=` caps at 500; `?branch=` filters to one branch's own history (200 with `data: []` for a real deck untouched on that branch — only a fully unknown deck 404s).

### `GET /decks/:id/versions/:n`

The canonical decklist text of one past version.

### `GET /decks/:id/versions/:n/download`

Streams a version back for saving. When it came from an uploaded file you get the **original bytes** under their original filename — the exact CSV or HTML that was imported, not a re-serialisation. Paste / edit / restore versions have no source file, so the canonical text comes back as `<name>-v<N>.txt` (`?name=` sets the base).

### `POST /decks/:id/versions/:n/restore`

Re-applies an old version by **appending** its text as a new version with `source: "restore"`, onto the **same branch** the restored version came from. History is never rewritten or truncated — the restore shows up as its own entry and the version restored from stays put. Restoring the current version is a no-op (`version_created: false`).

### `POST /decks/:id/head`

Explicitly promotes a version to **head** and pins it: `{"version": 3}` → `{"object":"deck_head","deck_id":…,"version":3,"branch":"deckbox","head_pinned":true}`. From then on, imports onto *other* branches no longer move head automatically, and `deck_cards` is rebuilt in the same transaction since head's content just changed.

`{"version": null}` (or an omitted `version`) **clears the pin** and resumes auto-follow onto the newest version across every branch — `head_version` goes back to `NULL`, the legacy meaning of "follow the newest", so it stays coherent rather than frozen at the just-cleared pin. `deck_cards` is rebuilt onto that resolved version in the same transaction, and the next import moves head again. Without this, promoting was a one-way door: every later import kept reporting success while the deck silently stopped changing.

Errors: `404 not_found` for an unknown deck, an unknown `version`, or an un-pin request against a deck with no versions.

### `GET /decks/:id/branches`

"Are my synced copies in sync?" — with **no fetch**. Every branch's own latest version is already stored locally, so this is a pure local comparison, not a pull from Deckbox/Moxfield/Archidekt (a three-way pull from those sites is not implemented). One row per branch with at least one version:

```json
{"object": "list", "deck_id": "…", "data": [
  {"branch": "main", "version": 4, "updated_at": "…", "is_head": true, "differs": {"added": 0, "removed": 0, "changed": 0}},
  {"branch": "moxfield", "version": 2, "updated_at": "…", "is_head": false, "differs": {"added": 0, "removed": 1, "changed": 3}}
]}
```

`differs` is measured against HEAD's content: `added` = cards that branch has and HEAD does not, `removed` = cards HEAD has and that branch does not, `changed` = cards present on both sides at a different quantity. The head's own row is flagged (`is_head`) rather than diffed against itself.

**Text is never compared.** Biblioplex stores each version's decklist byte-for-byte as imported, so a Deckbox export and a Moxfield export of the identical deck always differ textually (line order, comments, `4x` vs `4 `, tail annotations) — a string comparison would report permanent divergence. Both sides fold through `deck_card_rows()` before comparing, and the classification reuses the exact differ `POST /decks/diff` runs, so the counts here always agree with a manual diff of the same two versions.

```bash
curl -fsS http://localhost:8000/decks/$ID/branches
```

### Deck version branches

`deck_versions.branch` (`main` / `deckbox` / `moxfield` / `archidekt`) plus `decks.head_version` / `head_pinned` turn version history git-like: a branch is a label plus a filter over the same global version-number sequence, not a namespace — every `/decks/:id/versions/:n` URL keeps working regardless of which branch a version is on.

**Dedup is per-branch.** An unchanged Deckbox re-import right after a Moxfield import is not seen as a change, because each is compared against its OWN branch's latest — not the deck's newest version overall.

**Head auto-follows imports unless pinned.** A write on an unpinned deck moves `head_version` to the new version and rebuilds `deck_cards` in the same transaction. `POST /decks/:id/head` pins head explicitly; once pinned, a write to any OTHER branch neither moves head nor touches `deck_cards` — every "current content" read (`GET /decks/:id`, `GET /decks`, `GET /decks/:id/cards`, the effective commander/companion) keeps showing whatever head is pinned to. A deduped write (`version_created: false`) never moves head — there is nothing new to move to.

**`trunk_branch` narrows auto-follow to one branch.** By default (`trunk_branch: null`) head follows every branch's imports, which is git-like but means a deck synced from two services has its current content flip with sync order — import Deckbox, head lands there; import Moxfield an hour later, head moves again, dragging `coverage_pct` / `missing_usd` / `total_usd` / the card table with it. `PATCH /decks/:id {"trunk_branch": "main"}` names the ONE branch allowed to move head; imports onto any other branch still append a version (so nothing is lost, and `POST /decks/:id/versions` / `PATCH` both still succeed) but leave head, `deck_cards`, and every value derived from it untouched — the exact blocked path `head_pinned` already takes, sharing one code path rather than two. `head_pinned` still wins over `trunk_branch` when both apply. Set it back to `null` to resume following every import. `422 bad_request` for anything outside `main` / `deckbox` / `moxfield` / `archidekt`.

`diff_bases: {main, branch}` rides every write that supplied a `decklist`: `main` is branch `main`'s own latest version (captured *before* the write, since head has just auto-moved and diffing against it would always be empty), `branch` is this branch's own previous latest. Both are `null` on a decklist's first-ever version. Feeds the web UI's post-import diff; there is still no dedicated diff endpoint (see below).

**Importing a new version onto an existing deck** is what makes any of the above reachable by clicking through the app: `POST /decks/:id/versions` (file) and `PATCH /decks/:id` (paste) both append onto a deck you already picked, so `diff_bases` land non-null and the web import-diff subtabs render a real diff instead of their empty state. The web deck-detail pane's "Import new version" control offers branch + file/paste + message and calls both.

### Diffing two versions

There is deliberately **no** version-diff endpoint. `POST /decks/diff` already takes two decklist strings and is pure Python, so fetch both versions and post them:

```bash
A=$(curl -fsS localhost:8000/decks/$ID/versions/1 | jq -r .decklist)
B=$(curl -fsS localhost:8000/decks/$ID/versions/2 | jq -r .decklist)
jq -n --arg a "$A" --arg b "$B" '{a:$a,b:$b}' \
  | curl -fsS -X POST localhost:8000/decks/diff -H 'content-type: application/json' -d @-
```

### Section headings on the stateless `/decks/*` endpoints

Every `POST /decks/*` endpoint below reads decklist **sections**, so headings from a site export (`Commander`, `Deck`, `Mainboard`, `Sideboard`, Arena's `About` / `Name <deck>`) are understood rather than resolved as cards. Older versions sent them to the resolver and got back phantom `unresolved` entries: a stored Archidekt deck reported `unresolved: ["Mainboard"]`.

A decklist with **no** headings is unaffected: everything is the mainboard, exactly as before.

Which sections each endpoint reads:

| Sections read | Endpoints | Why |
| --- | --- | --- |
| mainboard only | `analyze`, `compare`, `suggest`, `sideboard-suggest`, `combos`, `near-combos`, `synergy`, `mulligan`, `goldfish`, `bracket`, `upgrade` | Slot counts, curve and shuffle simulations mean the maindeck — a sideboard card must not inflate a ramp count or land in an opening hand. |
| mainboard + sideboard | `price`, `export`, `diff` | You buy the sideboard, a format converter must not drop it, and a change report wants every change. |
| deck body + sideboard, kept apart | `legality` | Build rules constrain each separately. |

The mainboard **includes** the commander and companion, so an analysed Commander deck is 100 cards. `legality` is the exception: it subtracts the commander, because its rules expect a 99-card body plus a commander.

### `POST /decks/analyze`

Plaintext-decklist analysis against the template slot targets (`docs/design/template-classifier-plan.md` Step 6 consumer rung). Body `{"decklist": "<plaintext>"}` (≤64000 chars). One entry per line: `4 Lightning Bolt`, `1x Sol Ring`, or just `Sol Ring`. Blank lines and `#` / `//` comments are stripped; split-card names (`Fire // Ice`) survive because the comment marker is anchored to line start.

Resolution: case-insensitive exact match first, per-miss trigram fallback at 0.4 (same threshold as `/cards/named?fuzzy=`). Misses are returned under `unresolved`. Response sums `predicted_labels` across resolved cards (multilabel — a card with two slots adds `qty` to both, so `sum(counts.values())` can exceed deck size). Land is counted in `counts.land` but absent from `targets`/`deltas` (structural slot, not model-graded).

`curve` is a CMC histogram over non-land resolved cards, bucketed `0..6` plus a single `7+` bin. `curve_targets` is the mana-curve bell from the [EDH guide](edh-guide.md) over the 62 nonland slots — `{"1":9,"2":18,"3":15,"4":10,"5":5,"6+":5}` — and `curve_deltas` is `actual − target` per key (curve buckets `6` and `7+` fold into `6+`; MV0 has no target). `pips` counts colored-letter occurrences across every mana symbol in the deck's non-land mana costs: hybrids (`{U/B}`) add to both letters, phyrexian (`{W/P}`) adds W, generic (`{2}`, `{X}`) adds nothing, `{C}` counts as colorless. Both are qty-weighted.

Land detection is layout-aware: for `transform` / `modal_dfc` cards, only the FRONT face's `type_line` decides — a MDFC with a land back (Bala Ged Recovery, Emeria's Call, Search for Azcanta) is counted as a spell, matching mainline deckbuilder tooling. Its CMC/pips contribute to `curve` and `pips` as if it were a normal spell. For every other layout the word-boundary check runs against the top-level `type_line`.

```bash
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"decklist": "1 Sol Ring\n1 Cultivate\n1 Counterspell\n8 Forest"}' \
  http://localhost:8000/decks/analyze
# {"object":"deck_analysis",
#  "counts":{"ramp":2,"card_advantage":0,"targeted_disrupt":1,"mass_disrupt":0,"plan":0,"land":8},
#  "targets":{"ramp":12,"card_advantage":12,"targeted_disrupt":12,"mass_disrupt":6,"plan":30},
#  "deltas":{"ramp":-10,"card_advantage":-12,"targeted_disrupt":-11,"mass_disrupt":-6,"plan":-30},
#  "curve":{"0":0,"1":1,"2":2,"3":0,"4":0,"5":0,"6":0,"7+":0},
#  "pips":{"W":0,"U":1,"B":0,"R":0,"G":1,"C":1},
#  "cards":[...], "unresolved":[]}
```

`inventory_summary` + `missing_cards` grade the deck against your synced collection. Ownership is matched at the **oracle_id** level — owning any printing counts. `inventory` holds one row per printing, so quantities are summed per oracle before comparison. `missing_on_wishlist` counts how many missing cards you have already flagged to buy, and each missing card carries `on_wishlist`; entries NOT on the wishlist sort first, since those are the ones needing action. An empty inventory yields `coverage_pct: 0.0` and every card in `missing_cards`; a deck whose names all fail to resolve yields `coverage_pct: 100.0` and an empty list (nothing to own).

```json
"inventory_summary": {
  "total_cards_in_deck": 100, "owned_cards_in_deck": 85,
  "total_distinct_cards": 99, "owned_distinct_cards": 84,
  "total_missing_count": 15, "coverage_pct": 85.0
},
"missing_cards": [
  {"id": "...", "oracle_id": "...", "name": "Mana Crypt",
   "qty_required": 1, "qty_owned": 0, "qty_missing": 1,
   "on_wishlist": true}
]
```

Errors: empty / absent `decklist` → `422 bad_request`; >64000 chars → `422 bad_request "decklist too long"`. Card listing dedupes by resolved id (summed qtys) and preserves input order.

### `POST /decks/compare`

Diff two decklists. Body `{"a": "<plaintext>", "b": "<plaintext>"}` (≤64000 chars each). Runs the `/decks/analyze` pipeline against both sides on the same connection, then diffs by lowercased card name. Response:

```json
{
  "object": "deck_compare",
  "a": <deck_analysis payload>,
  "b": <deck_analysis payload>,
  "diff": {
    "added": [{"id","name","qty","predicted_labels"}, ...],
    "removed": [{"id","name","qty","predicted_labels"}, ...],
    "changed_qty": [{"id","name","qty_a","qty_b","delta"}, ...],
    "counts_delta": {"ramp": 1, ...},
    "curve_delta": {"1": -1, "3": 1, ...},
    "pips_delta": {"G": 2, ...}
  }
}
```

`added` = present in B, absent in A. `removed` = present in A, absent in B. `changed_qty` = present in both with different quantities (`delta = qty_b - qty_a`). All three lists sort by input order via `_tally`'s dedup. Slot / curve / pip deltas are `b - a` per key. Errors: empty `a` or `b` → `422 bad_request`; either side >64000 chars → `422 bad_request "decklist too long"`.

```bash
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"a":"1 Sol Ring\n1 Lightning Bolt","b":"1 Sol Ring\n2 Lightning Bolt\n1 Cultivate"}' \
  http://localhost:8000/decks/compare
```

### `POST /decks/suggest`

Delta-driven fill suggestions. Runs the same resolve+tally pipeline as `/decks/analyze`, then for each slot with `delta < 0` returns the top-K cards carrying that predicted label — ranked by `edhrec_rank ASC NULLS LAST`, filtered to exclude cards already in the resolved decklist, and (when `commander` is set) constrained to the commander's color identity (`color_identity <@ commander.color_identity`).

Body: `{"decklist": "<plaintext>", "commander": "<optional card name>"}`. Query: `limit=<1..20>` per-slot count (default 5). Response:

```json
{
  "object": "deck_suggestions",
  "deltas": {"ramp": -11, "card_advantage": -12, ...},
  "targets": {"ramp": 12, ...},
  "suggestions": {"ramp": [<raw card>, ...], "targeted_disrupt": [...]},
  "commander_color_identity": ["R"] | null,
  "unresolved": [...]
}
```

Only underfilled slots appear as keys in `suggestions`. Each entry decorates the raw Scryfall payload with `predicted_labels` + `label_scores` like the other card endpoints. Empty commander (`null`) leaves color identity unconstrained.

Errors: empty / absent `decklist` → `422 bad_request`; >64000 chars → `422 bad_request "decklist too long"`; unresolved `commander` → `422 commander_not_found`; `limit` outside `[1, 20]` → `422` (FastAPI validator).

```bash
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"decklist": "1 Sol Ring\n1 Cultivate\n8 Forest", "commander": "Krenko, Mob Boss"}' \
  'http://localhost:8000/decks/suggest?limit=3'
```

### `POST /decks/legality`

Format-legality + build-rule check for a decklist. Body `{"decklist": "<plaintext>", "format": "commander|modern|legacy|pioneer|standard|vintage|pauper|brawl|...", "commander": "<optional name>"}`. Default `format` is `commander`. When `commander` is supplied, the deck body should be 99 cards (commander not counted in the body) and every card's `color_identity` must be a subset of the commander's. A decklist carrying a `Commander` heading supplies that name automatically, and the commander is subtracted from the body — so a 100-card export validates as 99-plus-commander with no `commander` argument at all. An explicitly-passed `commander` always wins over the parsed one. Caveat: `commander_color_identity` is `null` for a *colorless* commander, which is indistinguishable from "no commander was used" — the `deck_size` of 99 is what reveals one was applied. Constructed formats (non-commander): max 4 copies non-basic, ≥60 cards, `restricted` cards capped at 1 copy. Basic Lands + a curated "any number of copies" catalog (Rat Colony, Persistent Petitioners, Dragon's Approach, …) are exempt from copy caps.

Response `{object: "deck_legality", legal: bool, format, deck_size, expected_deck_size, commander_color_identity: [str] | null, violations: [{card, reason, detail}], unresolved: [str]}`. `violations` groups every rule miss; `reason` slugs are stable — clients pattern-match on them: `not_legal`, `over_max_copies`, `color_identity`, `deck_size`, `unresolved`.

```bash
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"decklist": "1 Sol Ring\n2 Lightning Bolt\n1 Black Lotus", "format": "commander"}' \
  'http://localhost:8000/decks/legality'
```

Errors: empty / absent `decklist` → `422 bad_request`; `>64000` chars → `422 bad_request "decklist too long"`; unresolved `commander` name → `422 commander_not_found`. Unresolved decklist entries surface as `unresolved` violations (non-blocking-in-spirit but they block the `legal` flag — fix or delete before flipping to legal).

### `POST /decks/manabase`

Recommend a mana base for a commander. Body `{"commander": "<name>", "pip_weights": {"W":1,"U":0,"B":3,...}?, "budget_usd": <optional cap>}`. Query `land_count=<20..50>` (default 37). Nonbasic picks: filters a curated ~35-land priority list (rainbow fixing → fetches → shocks → utility) down to lands whose `color_identity <@ commander.color_identity`, optionally caps by budget, preserves priority order via `array_position`. Basics: proportional Hare-quota distribution over remaining slots — defaults to even weight across the commander's colors when `pip_weights` is absent. Response `{object: "deck_manabase", commander_color_identity, basics: {"Plains": n, ...}, nonbasics: [<raw>], notes: [str]}`.

```bash
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"commander": "Krenko, Mob Boss"}' \
  'http://localhost:8000/decks/manabase?land_count=37'
```

Not a full land database — the priority list is small on purpose. `NONBASIC_PRIORITY` in `src/biblioplex/decklist/manabase.py` is the source; extend it or migrate to a data-driven "top-K CI-compatible lands by EDHrec-rank" query when the curated shape stops fitting. Errors: unresolved `commander` → `422 commander_not_found`; `land_count` outside `[20, 50]` → `422` (FastAPI validator); `budget_usd <= 0` → `422` (FastAPI validator).

### `POST /decks/upgrade`

Per-card swap suggestions ranked by EDHrec-rank improvement. Body `{"decklist": "<plaintext>", "commander": "<optional name>", "budget_usd": <optional cap>}`. Query `limit=<1..25>` (default 10). For each deck card that has an EDHrec rank + at least one predicted label, the endpoint finds the single most-played (`edhrec_rank ASC`) alternative that shares the current card's dominant label, isn't already in the deck, has a lower (better) rank, fits the commander's color identity (when `commander` is set), and is under `budget_usd` (when set — NULL prices pass through). Response `{object: "deck_upgrades", swaps: [{out: {id, name, edhrec_rank}, in: <decorated raw>, matched_label, score}], commander_color_identity, unresolved}` with `score = out_rank - in_rank` (larger = bigger upgrade). Empty `swaps` when the deck is already high-power or nothing in the corpus outperforms it in the given slot.

```bash
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"decklist": "1 Cultivate\n1 Tempt with Discovery", "commander": "Ghalta, Primal Hunger", "budget_usd": 5.0}' \
  'http://localhost:8000/decks/upgrade?limit=5'
```

Errors: empty / absent `decklist` → `422 bad_request`; `>64000` chars → `422 bad_request "decklist too long"`; unresolved `commander` → `422 commander_not_found`; `limit` outside `[1, 25]` → `422` (FastAPI validator); `budget_usd <= 0` → `422` (FastAPI validator).

### `POST /decks/bracket`

Rough 1–5 bracket estimate per WOTC's Sept-2024 Commander bracket system. Body `{"decklist": "<plaintext>"}` (≤64000 chars). Runs the resolve pipeline, counts curated bracket-shifting signals (game-changers, fast mana, MLD, tutors, extra turns) Python-side, and counts subset-matching ingested combos via one SQL. Response `{object: "deck_bracket", bracket: 1..5, signals: {...}, game_changers: [names], notes: [str], unresolved: []}`.

```bash
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"decklist": "1 Sol Ring\n1 Rhystic Study\n1 Cyclonic Rift"}' \
  http://localhost:8000/decks/bracket
```

Rough estimate — WOTC's rubric is qualitative. See [`docs/deck-bracket.md`](deck-bracket.md) for the signal definitions + ladder + curated lists' maintenance policy. Errors: empty / absent `decklist` → `422 bad_request`; `>64000` chars → `422 bad_request "decklist too long"`.

### `POST /decks/combos`

Decklist-scoped Commander Spellbook combo lookup. Body `{"decklist": "<plaintext>"}` (≤64000 chars, same cap as `/decks/analyze`). Resolves names via the same exact + trigram-fallback path, then returns every ingested combo whose full card set is a subset of the deck's `oracle_id`s (`combos.oracle_ids <@ deck_oracle_ids` — Postgres array subset, backed by the GIN index).

```bash
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"decklist": "1 Isochron Scepter\n1 Dramatic Reversal\n1 Sol Ring"}' \
  http://localhost:8000/decks/combos
# {"object":"deck_combos",
#  "combos":[{"id":"spellbook-42","cards":["Isochron Scepter","Dramatic Reversal"],
#              "prereqs":"...","steps":"...","results":["Infinite mana"]}],
#  "unresolved":[]}
```

Empty `combos` when the corpus has no matches (or no combos ingested yet — check `combos_count` on `/health`). Unresolved names surface in a separate field like `/decks/analyze`. Errors: empty / absent `decklist` → `422 bad_request`; `>64000` chars → `422 bad_request "decklist too long"`.

### `POST /decks/mulligan`

Monte-Carlo opening-hand sampler. `keep_rate` = fraction of hands with 2–5 lands (London-mulligan heuristic). Deterministic with `seed`. `stats.hands = 0` when the sim couldn't run.

```bash
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"decklist": "38 Forest\n61 Bolt", "hands": 500, "seed": 42}' \
  http://localhost:8000/decks/mulligan
# {"object":"deck_mulligan","hands":500,"hand_size":7,"seed":42,"pool_size":99,
#  "stats":{"land_min":0,"land_max":7,"land_median":3.0,"land_mean":2.88,
#           "cmc_le_3_mean":4.51,"keep_rate":0.72},"unresolved":[]}
```

Errors: empty/absent `decklist` → `422 bad_request`; `>64000` chars → `422`; `hands` or `hand_size` out of range → `422`.

### `POST /decks/near-combos?missing=1&limit=25`

Combos the deck is `1..missing` cards away from completing. cEDH-style signal for "you're 1 card from Thassa's Oracle." Same body as `/decks/combos`; adds per-combo `in_deck` and `gap` lists so consumers can render "have this, get that."

```bash
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"decklist": "1 Isochron Scepter"}' \
  'http://localhost:8000/decks/near-combos?missing=1&limit=10'
# {"object":"deck_near_combos",
#  "combos":[{"id":"spellbook-42","missing":1,
#              "in_deck":["Isochron Scepter"],
#              "gap":[{"name":"Dramatic Reversal","id":"<uuid>"}],
#              "prereqs":"...","steps":"...","results":["Infinite mana"]}],
#  "unresolved":[]}
```

Sorted by gap ASC (smallest gap first). Empty-deck short-circuits — no runaway all-combos scan. Errors: same envelope as `/decks/combos` plus `missing` out of `[1, 3]` → 422 and `limit` out of `[1, 100]` → 422.

### `?projection=<comma-list>`

Supported on `/cards/:id`, `/cards/named`, `/cards/search`, `/cards/:id/similar`. Comma-separated dotted paths (`name`, `prices.usd`, `card_faces.0.name`). Top-level key must be a real Scryfall card field (or the synthetic `similarity` on `/similar`). Depth cap 6. Missing paths emit `null` so response shape stays stable. Bad input → `422 invalid_projection`.

```bash
curl -fsS 'http://localhost:8000/cards/search?q=t:dragon&projection=name,cmc,colors'
```
