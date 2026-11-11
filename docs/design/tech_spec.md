# Biblioplex — Tech Spec

A backend that serves a Scryfall-compatible subset of `GET /cards/search` and `GET /cards/:id` over the **default_cards** bulk file, plus `GET /cards/:id/similar` for vector-similarity lookup. Primary consumer: AI agents.

---

## §1 Scope

### §1.1 In scope

- `GET /cards/search` — fulltext-query endpoint, Scryfall-compatible response shape, paginated 175/page.
- `GET /cards/:id` — single card by Scryfall UUID.
- `GET /cards/named?fuzzy=` / `?exact=` — name lookup returning a single card. See §6.5.
- `GET /cards/:id/similar` — top-K similar cards by embedding cosine distance.
- `POST /admin/refresh` — trigger bulk re-ingest.
- All response endpoints accept `?projection=<comma-list>`.
- One row per `oracle_id`. Reprints / multiple printings collapsed to the first/oldest (`released_at` ASC).
- `reversible_card` layout is the one exception: each face is a legally distinct card with its own `oracle_id`. Split into one row per face at ingest.
- Multi-face card text is searchable via all faces (transform, modal_dfc, split, flip, adventure, meld, case, prototype, double_faced_token). See §3.3.
- Extras (tokens, emblems, schemes, planes, vanguards, art_series) are filtered out at ingest.

### §1.2 Non-goals

- No printing-level granularity: prices, sets, collector numbers, frames, art, foil, language, rulings, watermarks, illustrations.
- No other Scryfall sibling endpoints (`/cards/random`, `/cards/collection`, code/number lookups, multiverse/mtgo/arena/tcgplayer/cardmarket id lookups). `/cards/autocomplete` shipped 2026-07-01 as a quickwin — see README.
- No CSV format, no `text`/`image` formats. JSON only.
- No auth, no rate limiting, no multi-tenancy.

### §1.3 Note on default_cards vs oracle_cards

`default_cards` contains every printing (en + printed-lang where en absent). `oracle_cards` already deduplicates to one row per `oracle_id`. The spec follows user direction (default_cards) and dedupes in-DB. If file size becomes an operational pain point, swap source to `oracle_cards` — no schema change required.

---

## §2 Stack

- Python 3.14
- FastAPI + Uvicorn
- SQLAlchemy 2.x (async) + psycopg 3
- PostgreSQL 17 + `pgvector` extension (image: `pgvector/pgvector:pg17`)
- `sentence-transformers` for HuggingFace embeddings (CPU default; GPU optional via env)
- `httpx` for bulk download, `ijson` for stream-parsing
- `pydantic` v2 for response models
- `pytest` + `httpx.AsyncClient` for tests

No Alembic in first pass — bootstrap schema from a single `schema.sql` run on container start.

---

## §3 Data model

### §3.1 `cards` table

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `uuid` PK | Scryfall card id |
| `oracle_id` | `uuid` UNIQUE NOT NULL | dedup key |
| `name` | `text` NOT NULL | faces joined `A // B` |
| `type_line` | `text` | |
| `oracle_text` | `text` | |
| `mana_cost` | `text` | |
| `cmc` | `numeric` | |
| `colors` | `text[]` | `{W,U,B,R,G}` |
| `color_identity` | `text[]` | |
| `keywords` | `text[]` | |
| `produced_mana` | `text[]` | |
| `power` | `text` | strings to keep `*`/`X` |
| `toughness` | `text` | |
| `loyalty` | `text` | |
| `defense` | `text` | |
| `layout` | `text` | |
| `rarity` | `text` | from first/oldest printing |
| `legalities` | `jsonb` | `{format: legal\|not_legal\|restricted\|banned}` |
| `card_faces` | `jsonb` | passthrough |
| `all_parts` | `jsonb` | passthrough |
| `released_at` | `date` | for dedup ordering |
| `name_search` | `text` NOT NULL | name + each `card_faces[*].name`, space-joined |
| `name_sort_key` | `text` GENERATED | `lower(regexp_replace(name, '^(a\|an\|the)\s+', '', 'i'))`. Indexed for `order=name`. |
| `type_search` | `text` NOT NULL | type_line + each `card_faces[*].type_line` |
| `oracle_search` | `text` NOT NULL | oracle_text + each `card_faces[*].oracle_text` |
| `mana_cost_search` | `text` NOT NULL | mana_cost + each `card_faces[*].mana_cost` |
| `colors_eff` | `text[]` NOT NULL | `colors` if present, else union of `card_faces[*].colors` |
| `is_commander` | `boolean` GENERATED | `legendary` ∧ (`creature` ∨ `oracle_search ~ 'can be your commander'`) |
| `embed_source` | `text` GENERATED | `type_search || ' ' || oracle_search || ' ' || array_to_string(keywords, ' ')` |
| `embedding` | `vector(D)` | D from `HF_EMBED_DIM` env |
| `raw` | `jsonb` NOT NULL | full original card JSON for projection fallback |

### §3.2 Indexes

- `cards_oracle_id_key` (unique, auto)
- `cards_name_trgm` — `gin (name_search gin_trgm_ops)`
- `cards_oracle_trgm` — `gin (oracle_search gin_trgm_ops)`
- `cards_type_trgm` — `gin (type_search gin_trgm_ops)`
- `cards_colors_gin` — `gin (colors_eff)`
- `cards_color_identity_gin` — `gin (color_identity)`
- `cards_keywords_gin` — `gin (keywords)`
- `cards_produced_gin` — `gin (produced_mana)`
- `cards_legalities_gin` — `gin (legalities jsonb_path_ops)`
- `cards_is_commander_idx` partial `where is_commander`
- `cards_name_sort_key_idx` — `btree (name_sort_key)` for `order=name`
- `cards_embedding_hnsw` — `using hnsw (embedding vector_cosine_ops)`

### §3.3 Multi-face flatten

Scryfall populates top-level `oracle_text`, `type_line`, `mana_cost`, `colors` from the front face only for `transform`, `modal_dfc`, `adventure`, `meld`, `case`, `prototype`, `double_faced_token`. Top-level `name` joins faces with ` // `; same for `type_line` on `split`/`flip`. To make `o:`, `t:`, `m:`, `c:` work against the *whole* card, the ingester populates four plain `text` columns (`name_search`, `type_search`, `oracle_search`, `mana_cost_search`) by space-joining the top-level value with every `card_faces[*].<field>`. NULLs coalesced to `''`.

`colors_eff` is `colors` when non-null, else the deduped union of `card_faces[*].colors`.

These columns are not Postgres `GENERATED` because pgsql forbids subqueries / `jsonb_array_elements` in generated expressions — they are computed in the ingest path and re-derived on `POST /admin/refresh`.

### §3.4 Reversible cards

`layout = 'reversible_card'` (e.g. Heads I Win, Tails You Lose). Each face carries its own `oracle_id` and is legally a separate card. The ingester emits one row per face:
- `id` = face-specific Scryfall sub-id if present, else `<parent_id>:<face_index>`.
- `oracle_id` = face's `oracle_id`.
- All gameplay fields lifted from the face, with `card_faces` set to a single-element array containing that face.
- `layout` rewritten to the face's own layout (usually `normal`).

---

## §4 Bulk ingest

### §4.1 Source

`GET https://api.scryfall.com/bulk-data/default-cards` → `download_uri` → `httpx.stream` the gzipped JSON.

### §4.2 Flow

1. `POST /admin/refresh` enqueues an in-process background task (FastAPI `BackgroundTasks`). One run at a time guarded by `pg_advisory_lock(42)`.
2. Stream-parse with `ijson`. For each card:
   - **Drop** if `layout IN ('token','emblem','scheme','planar','vanguard','art_series','double_faced_token')`. (`double_faced_token` is a token sub-layout; drop too. Real DFC layout is `transform`/`modal_dfc`.)
   - **Reversible**: if `layout = 'reversible_card'`, emit one staged row per element of `card_faces` (see §3.4).
   - Otherwise emit one staged row, computing `name_search`, `type_search`, `oracle_search`, `mana_cost_search`, `colors_eff` per §3.3.
3. Insert into `cards_staging` (same shape as `cards` minus generated cols and `embedding`).
4. Collapse into `cards`:
   ```sql
   INSERT INTO cards (...)
   SELECT DISTINCT ON (oracle_id) ...
   FROM cards_staging
   ORDER BY oracle_id, released_at ASC NULLS LAST
   ON CONFLICT (id) DO UPDATE SET ...;
   ```
5. Embed all rows with NULL `embedding` in batches of 256 (configurable via `EMBED_BATCH`).
6. `DROP INDEX IF EXISTS cards_embedding_hnsw; CREATE INDEX cards_embedding_hnsw ...` — build HNSW after embeddings populated (bulk-then-index is ~10× faster than incremental).
7. Truncate staging.

### §4.3 First-boot behavior

On startup, if `SELECT count(*) FROM cards = 0`, log a hint and a curl example. Do not auto-fetch — user chose manual-only.

---

## §5 Search DSL

Full Scryfall syntax with non-applicable parts dropped (no prices, sets, prints, art tags, year/date, cube, border/frame/foil/finish/stamp, language, promo, display keywords other than `unique`/`order`/`dir` which are query-string params).

### §5.1 Supported operators

| Operator | Aliases | Semantics |
| --- | --- | --- |
| `name:X` / bare word | — | trgm `ILIKE %X%` on `name_search` OR `name_search %> X` (word_similarity ≥ `pg_trgm.word_similarity_threshold`, set to 0.4 per connection in `db.py`). Typo-tolerant — `invason` matches `Invasion of Ikoria`. |
| `!"Exact"` / `!word` | — | exact case-insensitive equality on `name_search`'s primary token (top-level `name`) |
| `o:X` / `oracle:X` | `fo:`/`fulloracle:` treated same (no reminder text distinction here) | `oracle_search ILIKE %X%`. `~` placeholder substitutes the card's top-level `name`. Both the substituted name and the user-supplied `X` are matched as **literal text** — ILIKE metacharacters (`\`, `_`, `%`) are escaped before pattern build (D13.1), so `o:50%` matches the literal string `50%` and a card named `Kongming, "Sleeping Dragon"_alt` won't wildcard-match unrelated rows via `~`. |
| `t:X` / `type:X` | — | `type_search ILIKE %X%` |
| `c:` / `color:` | — | colors set ops vs `colors_eff` — see §5.4 |
| `id:` / `identity:` | — | same ops vs `color_identity` |
| `m:` / `mana:` | — | mana cost match against per-face symbol multisets (any face matches) — see §5.5 |
| `mv:` / `manavalue:` / `cmc:` | — | numeric vs top-level `cmc` (Scryfall already normalizes per layout), `even`/`odd` supported |
| `pow:` / `power:` | — | numeric vs `power::numeric` (NULL on non-numeric) |
| `tou:` / `toughness:` | — | numeric vs `toughness::numeric` |
| `pt:` / `powtou:` | — | numeric vs `pow+tou` |
| `loy:` / `loyalty:` | — | numeric vs `loyalty::numeric` |
| `kw:` / `keyword:` | — | `keywords @> ARRAY[X]` (case-insensitive) |
| `produces:` | — | `produced_mana` set ops |
| `f:` / `format:` | — | `legalities->>X = 'legal'` |
| `banned:` | — | `legalities->>X = 'banned'` |
| `restricted:` | — | `legalities->>X = 'restricted'` |
| `r:` / `rarity:` | — | comparison vs `rarity` ordered `common<uncommon<rare<special<mythic<bonus` |
| `layout:` | — | equality on `layout` |
| `is:commander` | — | `is_commander = true` |
| `commander:X` | only `:` accepted; other ops 422 | identity-subset filter (`color_identity <@ X`) — Scryfall sugar for `id<=X`. Pair with `f:commander` explicitly when you want format legality too. |
| `is:hybrid` | — | `mana_cost ~ '\{[WUBRG]/[WUBRG]\}'` |
| `is:phyrexian` | — | `mana_cost ~ '\{[WUBRGC]/P\}'` |
| `is:split`/`flip`/`transform`/`tdfc`/`meld`/`leveler`/`dfc`/`mdfc` | — | `layout = X` (alias `tdfc`→`transform`) |
| `is:permanent` / `spell` / `vanilla` / `frenchvanilla` | — | derived from top-level `type_line` + `oracle_text` only (NOT face-flattened) — matches Scryfall's front-face-only semantics for type categories. `t:` and `o:` keep using `_search` columns (face-aware). |
| `is:reserved` (D15.20) | — | `raw->>'reserved' = 'true'` — Wizards' Reserved List membership. Oracle-level (RL is by card name, not printing), so the oldest-printing collapse preserves it. `is:reprint` intentionally not wired — our collapse keeps the oldest printing whose `reprint` is (by definition) always `false`, so the predicate would return empty. |
| `edhrec:` / `edhrecrank:` (D15.22) | `> < >= <= = != :` | numeric vs `cards.edhrec_rank` (per-oracle popularity rank, lower = more played). Backed by `cards_edhrec_rank_idx`. Non-numeric RHS → constant-false; NULL-ranked rows never match positive comparisons. Pairs with the pre-existing `order=edhrec` sort. |
| `usd:` (D15.23) | `> < >= <= = != :` | numeric vs `cards.price_usd` (MIN over all printings of the same oracle_id, per D1.9 collapse). Backed by `cards_price_usd_idx`. NULL-priced rows never match positive comparisons. Pairs with the pre-existing `order=usd` sort and the `budget_usd` gate on `/decks/upgrade` + `/decks/manabase`. |
| `has:indicator` / `has:watermark` | — | bool field presence (watermark from raw) |
| `game:` | — | `raw->'games' ? X` against the kept printing's `games` array (`paper`/`mtgo`/`arena`/`astral`/`sega`). Latest-printing only — see drift §5.1. |
| `otag:` / `function:` | — | Scryfall oracle tag (Tagger). `EXISTS` on `oracle_tags(oracle_id, tag)` side table. Hierarchy flattened at ingest so the lookup is a flat indexed equality. |
| Numeric comparators | `> < >= <= = !=` | for any numeric field |
| Boolean composition | `AND` (implicit), `OR`, `-X` / `not:`, `(...)` | |
| Regex | `/.../` for `name`, `o`, `t` | Postgres `~*` |
| Quoted strings | `" "` | preserve spaces/punctuation |

### §5.2 Out of DSL (return 422 with hint)

`s:`/`e:`/`set:`/`cn:`/`b:`/`block:`/`g:`/`in:`/`st:`/`new:`/`prints:`/`sets:`/`paperprints:`/`papersets:`/`cube:`/`year:`/`date:`/`eur:`/`tix:`/`cheapest:`/`a:`/`artist:`/`artists:`/`ft:`/`flavor:`/`wm:`/`watermark:`/`border:`/`frame:`/`is:foil|nonfoil|etched|glossy|hires|digital|alchemy|rebalanced|promo|spotlight|booster|...`/`stamp:`/`art:`/`atag:`/`lang:`/`language:`. `otag:` / `function:` are **wired** per §5.1 (oracle tags); `edhrec:`/`edhrecrank:` promoted to §5.1 (D15.22); `usd:` promoted to §5.1 (D15.23); `atag:` (illustration tags) remains out-of-scope.

### §5.3 Parser

Hand-rolled recursive-descent. One file, one entrypoint `parse(q: str) -> Expr`. AST → SQLAlchemy expression. No PEG/Lark dependency.

### §5.4 Color comparison semantics

All operate on `colors_eff` (union across faces, see §3.3). `c:rg` → `colors_eff @> {R,G}`. `c=rg` → `colors_eff = {R,G}`. `c>=uw` → superset. `c<=esper` → subset. `c:m` / `c:multicolor` → `cardinality(colors_eff)>=2`. `c:c` / `c:colorless` → `colors_eff = '{}'`. Numeric: `c=2` → `cardinality(colors_eff)=2`. Nicknames (guilds, shards, wedges, four-color) → static map → letter set.

### §5.5 Mana cost comparison

A card has a list of mana-cost strings: top-level `mana_cost` plus each `card_faces[*].mana_cost`. Each is tokenized to a symbol multiset (generic numerals expanded: `2WW` → `{1,1,W,W}`). `m:X` matches when **any** face's multiset is a superset of `X`. `m=X` matches when any face equals `X`. `m>X` strict superset. Empty top-level cost (MDFC, transform) is skipped; faces are what carry costs. Phyrexian/hybrid require `{}` wrapping for split symbols; bare `WUBRGC` shorthand OK.

---

## §6 Endpoints

### §6.1 `GET /cards/search`

Query params:

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `q` | string ≤1000 chars | required | DSL per §5. Empty/missing → 422 `q is required`. |
| `unique` | `cards`\|`prints`\|`art` | `cards` | only `cards` is meaningful (we have one row per oracle); others alias to `cards` |
| `order` | `name`\|`cmc`\|`power`\|`toughness`\|`released`\|`color`\|`edhrec`\|`rarity`\|`usd`\|`relevance` | `name` | `edhrec` sorts by `cards.edhrec_rank`, NULLS LAST; `usd` sorts by MIN(prices.usd) across printings, NULLS LAST. `relevance` sorts by `word_similarity(<bare-terms>, name_search) DESC`; `dir=auto` → `desc`. Filter-only queries (no bare-word terms) fall back to `name_sort_key asc`. |
| `dir` | `auto`\|`asc`\|`desc` | `auto` | |
| `page` | int ≥1 | 1 | 175/page |
| `projection` | string | — | §7 |

Secondary sort always `id ASC` after the primary `order=` for stable pagination.
`order=name` sorts on `name_sort_key` (leading "A "/"An "/"The " stripped, lowercased).
`format=csv` / `format=text` → 400 `unsupported format` (JSON only).

Response: Scryfall `List` shape.
```json
{ "object":"list", "total_cards":N, "has_more":bool, "next_page":"...?page=2", "data":[...] }
```

422 on parse failure or unsupported operator (per §5.2). 404 if `total_cards=0`.

### §6.2 `GET /cards/:id`

Param: `:id` UUID. `?projection=` optional. 404 if missing. JSON only.

### §6.3 `GET /cards/:id/similar`

Param: `:id`. Query: `limit` int 1–100 default 20, `projection`.

```sql
SELECT *, embedding <=> $1 AS distance
FROM cards
WHERE id <> $2 AND embedding IS NOT NULL
ORDER BY embedding <=> $1
LIMIT $3;
```

Returns `List` with each entry annotated `"similarity": 1 - distance`. 404 if base card missing or its embedding NULL.

### §6.4 `GET /cards/named`

Query params (exactly one of `exact`/`fuzzy` required):

| Param | Type | Notes |
| --- | --- | --- |
| `exact` | string | Case-insensitive exact match on top-level `name`. |
| `fuzzy` | string | Trigram similarity against `name_sort_key`. Returns the single best match if `similarity >= 0.4`, else 404. |
| `projection` | string | §7 |

Returns a card object (not a List). 400 if neither param supplied or both supplied. 404 if no card matches.

### §6.5 `POST /admin/refresh`

No body. Returns `{ "status":"started"|"already_running", "job_id":"uuid" }`. Job runs in background. Optional `GET /admin/refresh/:job_id` for status (skip if YAGNI — log to stdout suffices).

### §6.6 `GET /health`

Always `200` when the DB is reachable. Body:

```json
{
  "status": "ok",
  "db": "up",
  "cards_count": 31245,
  "refresh": { "running": false, "started_at": null, "job_id": null }
}
```

Returns `503` only if `SELECT 1` against the DB fails. Refresh state does **not** flip the status — orchestrators get `running:true` in the body and decide policy themselves.

**`GET /health/live` (D16.20)** — sub-second liveness probe complement. Fires `SELECT 1`; returns `200 {"status":"ok"}` on success, `503 {"status":"error"}` on any DB error. Body carries only `status`. K8s liveness probes must not block on reltuples-side stats or refresh-state locks — use this endpoint for the probe, `/health` for readiness.

### §6.7 `POST /decks/analyze`

Consumer-facing companion to the rules-layer classifier (`src/biblioplex/classify/rules.py`, see `docs/design/template-classifier-plan.md`). Takes a plaintext decklist, resolves names against `cards`, sums predicted-label counts across the resolved cards, and reports per-label deltas against the template targets:

```python
TARGETS = {
    "ramp": 12,
    "card_advantage": 12,
    "targeted_disrupt": 12,
    "mass_disrupt": 6,
    "plan": 30,
}
```

(Land is structural and excluded from the model — counted by the resolver but ignored in deltas.)

Body (JSON): `{"decklist": "<plaintext>"}`. The plaintext is one entry per line, optional leading integer quantity (`4 Lightning Bolt`, `1x Sol Ring`, or just `Sol Ring`). Blank lines and lines starting with `#` or `//` are ignored. `decklist` empty or absent → `422 q is required`-style envelope (`code:"bad_request"`, details name the field). Length cap 64000 characters → `422 bad_request "decklist too long"`. Per-line quantity capped at 99 (clamped silently).

Resolution: case-insensitive exact match on `name` first (batched `LOWER(name) = ANY(...)`), then per-miss trigram fallback against `name_sort_key` at the same `0.4` threshold used by `/cards/named?fuzzy=` (§6.4). Inputs that miss both are returned under `unresolved`.

Response:

```json
{
  "object": "deck_analysis",
  "counts":   { "ramp": 11, "card_advantage": 10, "targeted_disrupt": 12, "mass_disrupt": 6, "plan": 28, "land": 38 },
  "targets":  { "ramp": 12, "card_advantage": 12, "targeted_disrupt": 12, "mass_disrupt":  6, "plan": 30 },
  "deltas":   { "ramp": -1, "card_advantage": -2, "targeted_disrupt":  0, "mass_disrupt":  0, "plan": -2 },
  "cards":   [ { "id": "<uuid>", "name": "Sol Ring", "qty": 1, "predicted_labels": ["ramp"] }, ... ],
  "unresolved": ["lighning bolt"]
}
```

Multilabel semantics: a card with `predicted_labels = ["ramp","card_advantage"]` contributes `qty` to BOTH slots, so `sum(counts.values())` can exceed deck size. This is the same slot-counts semantic the web UI surfaces above the result list (see README "Web UI").

`land` is reported in `counts` but absent from `targets`/`deltas` (structural slot, not model-graded). Type-line containing `Land` qualifies; basics included in the count.

The response also carries the mana-shape fields `curve` (CMC histogram over non-land resolved cards, buckets `0..6` plus `7+`, qty-weighted), `pips` (colored-letter counts across non-land mana costs), and — for grading the curve — `curve_targets` and `curve_deltas`. `curve_targets` is the guide §6 bell over the 62 nonland slots, `{"1":9,"2":18,"3":15,"4":10,"5":5,"6+":5}`; `curve_deltas` is `actual − target` per key, folding curve buckets `6` and `7+` into a single `6+` and leaving MV0 untargeted (like land). `inventory_summary` / `missing_cards` grade the deck against a synced inventory when present. `/decks/compare` reuses this pipeline, so both sides inherit these fields.

Cards with `predicted_labels = []` (no functional label and no `plan` fallback at classify time — should be rare given the rules-layer `plan = 1.0 iff functional_max < 0.5` semantic) contribute to no slot. They appear in `cards` for transparency.

### §6.8 `GET /cards/:id/substitutes`

Slot-constrained variant of `/cards/:id/similar` (§6.3). Hybrid nearest-neighbour pipeline identical to `/similar`, with an additional hard filter `predicted_labels @> ARRAY[:slot]` on candidate over-fetch so every returned card carries the requested slot.

Query params:

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `slot` | one of `ramp`, `card_advantage`, `targeted_disrupt`, `mass_disrupt`, `plan` | required | Slug must match a `LABELS` entry; unknown → `422 unsupported_operator`. |
| `limit` | int 1–100 | 20 | |
| `projection` | string | — | §7 |

Errors: `slot` missing → `422 bad_request "slot is required"`; `slot` not in `LABELS` → `422 unsupported_operator`; base card missing or embedding `NULL` → `404 not_found` (same shape as `/similar`).

Returns the same `List` envelope as `/similar`, with each entry's `raw` decorated with `similarity = 1 - distance`.

### §6.11 `POST /decks/legality` (D14.7)

Format-legality + build-rule check for a decklist. Body `{"decklist": "<plaintext>", "format": "<slug>", "commander": "<optional name>"}`. `format` defaults to `commander` and matches Scryfall's `legalities` map keys. `commander` name is resolved separately (same `_resolve_commander_ci` helper as §6.9 / §6.10) so its color identity can enforce CI-subsumption on every card.

**Rules by format:**

Commander (also `brawl`):
- Deck size = 100 (99 body + 1 commander when `commander` is supplied; 100 body when it isn't).
- Every card's `legalities[format] == 'legal'`. `banned` / `not_legal` / `restricted` fail.
- Singleton non-basic. Basic Lands (`Basic` in `type_line`) exempt. Curated "any number" catalog exempt — see `UNLIMITED_COPIES_NAMES` in `src/biblioplex/decklist/legality.py`.
- Every card's `color_identity` ⊆ commander's when supplied.

Constructed (`modern`, `legacy`, `pioneer`, `standard`, `vintage`, `pauper`, `historic`, `alchemy`, `timeless`, `oathbreaker`, …):
- Deck size ≥ 60.
- Every card `legalities[format] == 'legal'` or `'restricted'` (vintage). `restricted` implies max 1 copy.
- Max 4 copies non-basic. Basics + curated "any number" catalog exempt.
- No CI check (color identity is a commander-format concept).

Response: `{object: "deck_legality", legal: bool, format, deck_size, expected_deck_size, commander_color_identity: [str] | null, violations: [{card, reason, detail}], unresolved: [str]}`.

**Violation `reason` slugs** (stable — clients pattern-match): `not_legal`, `over_max_copies`, `color_identity`, `deck_size`, `unresolved`.

Errors: empty / absent `decklist` → `422 bad_request`; `>64000` chars → `422 bad_request "decklist too long"`; unresolved commander name → `422 commander_not_found`.

### §6.10 `POST /decks/bracket`

Rough 1–5 estimate for a decklist per WOTC's Commander bracket system (announced Sept 2024). Body `{"decklist": "<plaintext>"}` (same parser + 64000-char cap as §6.7). Response `{object: "deck_bracket", bracket, signals, game_changers, notes, unresolved}`.

**Signals** (all six counted independently — a card can trigger more than one):

| Signal | Source |
| --- | --- |
| `game_changers` | Hand-curated `frozenset[str]` (~40 cards from WOTC's Sept-2024 announcement). |
| `fast_mana` | Hand-curated `frozenset[str]` (~15 non-basic ramp pieces faster than turn number). |
| `mld` | Hand-curated `frozenset[str]` (~12 mass-land-destruction spells). |
| `tutors` | Hand-curated `frozenset[str]` (~18 unconditional "any card" tutors — narrow tutors excluded). |
| `extra_turns` | Hand-curated `frozenset[str]` (~12 extra-turn spells). |
| `combos` | `count(*) FROM combos WHERE oracle_ids <@ :deck_oids::uuid[]` — subset match against the D14.1 combos side table. Zero when `COMBOS_SOURCE_URL` is unset. |

Curated lists live in `src/biblioplex/decklist/bracket.py`; full contents + migration notes in `docs/deck-bracket.md`.

**Ladder** — first-match wins, evaluated top-to-bottom:

```
combos >= 2                             → 5 (cEDH)
combos >= 1 AND fast_mana >= 3          → 5 (cEDH)
combos >= 1                             → 4 (Optimized)
game_changers >= 4                      → 4 (Optimized)
mld >= 1                                → 4 (Optimized)
game_changers >= 1                      → 3 (Upgraded)
fast_mana >= 2                          → 3 (Upgraded)
tutors >= 3                             → 3 (Upgraded)
extra_turns >= 1                        → 3 (Upgraded)
tutors >= 1                             → 2 (Core)
fast_mana >= 1                          → 2 (Core)
(no signals)                            → 1 (Exhibition)
```

**Not authoritative.** WOTC published a spirit-of-the-format guideline without numeric thresholds, so this ladder is an opinionated first-cut. Migration path when curated-list maintenance starts hurting: swap Python-side name checks for `otag:` lookups against Scryfall Tagger's `tutor` / `mass-land-destruction` / `extra-turn` / `ramp` slugs (deferred — see `docs/deck-bracket.md`).

`game_changers` (top-level, list of matched card names, lowercased) is surfaced separately from `signals.game_changers` (the count) so consumers can render "your game-changers are …" without a second query. `notes` is a human-readable list of the signals that triggered the chosen bracket floor. `unresolved` mirrors §6.7.

### §6.15 `POST /decks/mulligan` (D14.10)

Monte-Carlo opening-hand sampler. Draws `hands` × `hand_size` hands from the resolved deck and reports land count + cheap-card count + keep-rate stats.

Body: `{"decklist": "<plaintext>", "hands": 500, "hand_size": 7, "seed": null}`. Same 64000-char cap as §6.7. `hands` clamped `[1, 10000]`, `hand_size` clamped `[1, 20]`. `seed` (nullable int) threads `random.Random(seed)` for reproducible runs.

Pool construction: for each resolved row, add `qty` copies of `(name, cmc, is_land)` to the pool. `is_land` uses the same layout-aware detector as `/decks/analyze` (MDFC front-face aware). Cards with non-numeric CMC (X spells, MDFC backs) get `cmc = None` and never count toward `cmc_le_3_mean`.

`keep_rate` is the London-mulligan heuristic: fraction of hands with `2 ≤ lands ≤ 5`. No formal EV, no bottom-decking simulation (would require color balance + play/draw priors that don't fit a pure fn).

Response:

```json
{
  "object": "deck_mulligan",
  "hands": 500,
  "hand_size": 7,
  "seed": 42,
  "pool_size": 99,
  "stats": {
    "land_min": 0,
    "land_max": 7,
    "land_median": 3.0,
    "land_mean": 2.8846,
    "cmc_le_3_mean": 4.512,
    "keep_rate": 0.7231
  },
  "unresolved": []
}
```

`hands = 0` sentinel when the sim couldn't run (empty pool or `hand_size` exceeds pool size); the `stats` block still surfaces but every field is zero.

Errors: empty/absent `decklist` → 422 `bad_request`; `>64000` chars → 422; `hands` or `hand_size` out of range → 422.

### §6.14 `POST /decks/near-combos` (D14.9)

Extension of §6.9 `/decks/combos`. Returns combos the deck is `1..missing` cards away from completing — the cEDH signal for "you're 1 card from Thassa's Oracle."

Body: `{"decklist": "<plaintext>"}` (same parser + 64000-char cap as §6.7).

Query params:

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `missing` | int 1–3 | 1 | Gap size to surface. `1` is the primary cEDH signal; deeper gaps approach noise. |
| `limit` | int 1–100 | 25 | Sort key is gap ASC — smallest-gap combos first. |

SQL: pre-filter `co.oracle_ids && :deck_oids::uuid[]` (any-overlap; GIN-backed). Two LATERAL subqueries split the combo's oracle_ids into `gap` (in combo, NOT in deck) and `in_deck`. Outer `gap.gap_count BETWEEN 1 AND :missing` drops both fully-present combos (that's `/decks/combos`'s job) and combos whose gap exceeds the cap.

Response:

```json
{
  "object": "deck_near_combos",
  "combos": [
    {
      "id": "spellbook-42",
      "prereqs": "...",
      "steps": "...",
      "results": ["Infinite mana"],
      "in_deck": ["Isochron Scepter"],
      "gap": [{"name": "Dramatic Reversal", "id": "<uuid>"}],
      "missing": 1
    }
  ],
  "unresolved": []
}
```

Empty-deck short-circuit: when the resolve step returns zero rows, the near-combos SQL is skipped and the response carries `combos: []` (never a runaway all-combos scan on an empty subset predicate).

Errors: empty/absent `decklist` → 422 `bad_request`; `>64000` chars → 422; `missing` out of `[1, 3]` → 422; `limit` out of `[1, 100]` → 422.

### §6.13 `GET /commanders/suggest` (D17.2)

Commander discovery. Closes the "I want an Aristocrats commander in Mardu" gap for MCP agents. Rough DSL near-equivalent: `/cards/search?q=is:commander id<=WUB otag:aristocrats&order=edhrec` — the endpoint bakes the same recipe behind an MCP-first URL so agents don't need to synthesize the DSL sugar.

Query params:

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `tags` | comma-separated string | — | Scryfall Tagger slugs (case-insensitive; AND-joined). Every slug must match. |
| `colors` | WUBRG string | — | Case-insensitive. Enforces `color_identity <@ :ci` (subset). Non-WUBRG char → 422. |
| `limit` | int 1–175 | 25 | |
| `projection` | string | — | §7 |

SQL:

```sql
SELECT c.raw, c.predicted_labels, c.label_scores
FROM cards c
WHERE c.is_commander = true
  AND c.color_identity <@ CAST(:ci AS text[])       -- optional
  AND EXISTS (SELECT 1 FROM oracle_tags ot
              WHERE ot.oracle_id = c.oracle_id AND ot.tag = :tag_0)  -- one per tag slug
  ...
ORDER BY c.edhrec_rank ASC NULLS LAST, c.id ASC
LIMIT :limit
```

Response: Scryfall `List` envelope `{object: "list", data: [<card>...], has_more: bool}`. `has_more` is `len(data) == limit` (no `total_cards` — the endpoint is top-N by design; agents raise `limit` if they need more).

Unknown tag slugs silently return zero rows (no 422 for unrecognized slugs) — agents commonly guess slugs and 422 would be user-hostile. Malformed `colors` (non-WUBRG char) still 422s so operators catch typos.

### §6.12 `GET /cards/random` (D17.3)

Returns one random card, optionally satisfying a DSL filter. Spec §1.2 originally listed `/cards/random` as a non-goal; D17.3 promoted it for MCP agents that ask for "surprise me" / flavor-exploration prompts.

Query params:

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `q` | string ≤1000 chars | — | Optional DSL filter per §5. Empty / whitespace is treated as unfiltered (unlike `/cards/search` where empty `q` is a 422). |
| `projection` | string | — | §7 |

Response: single Scryfall card object (not a `List` envelope — matches `/cards/{id}`).

SQL: `SELECT raw, predicted_labels, label_scores FROM cards WHERE <where> ORDER BY random() LIMIT 1`. On ~30k rows the sort is <100 ms; if scale grows 10× consider `TABLESAMPLE SYSTEM (1)` for the unfiltered branch.

**Not cached** — random by design; `Cache-Control` header absent.

Errors: `q` > 1000 chars → 422 `bad_request`; DSL parse fails → 422 `bad_request`; §5.2 rejected key → 422 `unsupported_operator`; `q` narrows to zero rows or the table is empty → 404 `not_found`.

### §6.23 `GET /tags` (D17.4)

Oracle-tag slug catalog. Biggest MCP blocker before D17.4 was slug guessing (`otag:aristocrats` vs `otag:aristocrat`). This endpoint enumerates every slug present in the `oracle_tags` side table with a hit count so agents can autocomplete + rank before firing a DSL query.

Query params:

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `prefix` | string | — | Optional case-insensitive prefix. Compiled to `tag ILIKE :prefix || '%'`. Empty / unset = unfiltered. |
| `limit` | int 1–500 | 50 | |

SQL:

```sql
SELECT tag AS slug, count(*) AS count
FROM oracle_tags
[WHERE tag ILIKE :prefix || '%']
GROUP BY tag
ORDER BY count(*) DESC, tag ASC
LIMIT :limit
```

Response: Scryfall `List` envelope `{object: "list", data: [{slug, count}], has_more}`. `has_more = len(data) == limit`.

Cache: weak ETag over `(endpoint, prefix, limit, refresh_epoch)`; `If-None-Match` short-circuits to 304 before the DB round-trip. `Cache-Control: public, max-age=300, stale-while-revalidate=600`.

### §6.24 `GET /keywords` (D17.4)

Scryfall keyword catalog. Extracts strings from `raw->'keywords'` (`Flying`, `Trample`, `Ward`, …) with a hit count. Backs the pending `keyword:` DSL predicate (D15.30). Casing matches Scryfall's canonical form.

Query params:

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `prefix` | string | — | Optional case-insensitive prefix. Compiled to `k ILIKE :prefix || '%'`. |
| `limit` | int 1–500 | 50 | |

SQL:

```sql
SELECT k AS keyword, count(*) AS count
FROM cards, jsonb_array_elements_text(raw->'keywords') AS k
[WHERE k ILIKE :prefix || '%']
GROUP BY k
ORDER BY count(*) DESC, k ASC
LIMIT :limit
```

Response: Scryfall `List` envelope `{object: "list", data: [{keyword, count}], has_more}`. Cached same as §6.23.

### §6.25 `GET /sets` (D17.4)

Set catalog. Enumerates every Scryfall set present in the corpus with `{code, name, released_at, count}`. Ordered by release date DESC so the most recent set surfaces first. No prefix / limit knobs — the universe is bounded (<1k sets total on Scryfall) and agents get the full list in one payload.

SQL:

```sql
SELECT raw->>'set' AS code,
       raw->>'set_name' AS name,
       MIN(CAST(raw->>'released_at' AS date)) AS released_at,
       count(*) AS count
FROM cards
GROUP BY raw->>'set', raw->>'set_name'
ORDER BY MIN(CAST(raw->>'released_at' AS date)) DESC NULLS LAST
```

Response: Scryfall `List` envelope `{object: "list", data: [{code, name, released_at, count}]}` (no `has_more` — full-catalog dump).

Cache: weak ETag over `(endpoint, refresh_epoch)`; same 300/600 SWR posture as §6.23 / §6.24.

Bug.27 discipline: `CAST(raw->>'released_at' AS date)` — SQLAlchemy `text()` mangles `::`.

---

## §7 Projection

`?projection=name,type_line,prices.usd,card_faces.0.image_uris.normal`

- Comma-separated. Dotted paths into JSON. Array indices supported (`card_faces.0.name`).
- Whitelist top-level keys to the union of Scryfall card fields + our `similarity` annotation. Unknown top-level → 422.
- Resolved server-side: SELECT all rows, project after row fetch (pure Python `dict` walk). Premature SQL JSON-path optimization deferred.
- **Missing path** (path syntactically valid, value absent on a given card) → emit `null` for that field on that card. Keeps response shape stable for agents pattern-matching field presence.
- Path depth cap: 6 segments. Deeper paths → 422.
- Empty/unset `projection` → return the original Scryfall card object (reconstructed from `raw` JSONB), preserving wire compatibility.
- `?fields=` is a shorter alias for `?projection=` (D15.29); both names accept identical CSV syntax. `?fields=` wins when both are supplied. Only `fields` surfaces in the OpenAPI/MCP schema; `?projection=` remains supported for wire-compat.

---

## §8 Embeddings

### §8.1 Model

- Loaded via `sentence-transformers.SentenceTransformer(model_name)`.
- `HF_EMBED_MODEL` env, default `sentence-transformers/all-MiniLM-L6-v2`.
- `HF_EMBED_DIM` env required; checked against `model.get_sentence_embedding_dimension()` at startup; mismatch → fail-fast.
- `HF_EMBED_DEVICE` env, `cpu`\|`cuda`, default `cpu`.
- Model downloaded lazily from HuggingFace on first instantiation (no Docker build-time pre-cache). First call after deploy pauses while the model downloads to `~/.cache/huggingface`. Subsequent process starts hit the cache. Requires HF reachable from the API container's network — document in README.

### §8.2 Re-embedding

Changing `HF_EMBED_MODEL` requires:
1. Drop `cards.embedding` column, recreate with new `vector(D')`.
2. Drop & rebuild HNSW index.
3. Re-run embedding step (`POST /admin/refresh?embed_only=true` flag — TBD if needed).

First pass: document the manual `psql` snippet. Skip the admin flag.

### §8.3 Source text

`type_search || ' ' || oracle_search || ' ' || array_to_string(keywords, ' ')`. Uses the face-flattened columns from §3.3 so MDFC/transform/adventure backs are represented in the vector. NULLs coalesced to empty. Exposed as the `embed_source` generated column.

---

## §9 Docker Compose

```yaml
services:
  db:
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_DB: biblioplex
      POSTGRES_USER: biblioplex
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./schema.sql:/docker-entrypoint-initdb.d/schema.sql:ro
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "biblioplex"]
      interval: 5s
  api:
    build: .
    environment:
      DATABASE_URL: postgresql+psycopg://biblioplex:${DB_PASSWORD}@db:5432/biblioplex
      HF_EMBED_MODEL: ${HF_EMBED_MODEL:-sentence-transformers/all-MiniLM-L6-v2}
      HF_EMBED_DIM: ${HF_EMBED_DIM:-384}
      HF_EMBED_DEVICE: ${HF_EMBED_DEVICE:-cpu}
      EMBED_BATCH: ${EMBED_BATCH:-256}
    ports: ["8000:8000"]
    depends_on:
      db: { condition: service_healthy }
volumes:
  pgdata:
```

`schema.sql` includes `CREATE EXTENSION pgvector; CREATE EXTENSION pg_trgm;` and all DDL from §3.

Operational notes:
- Disk: peak ingest ~1.5 GB (522 MB JSON download + ~1 GB staging + main table). Size `pgdata` volume accordingly.
- No Alembic. Schema-changing deploys require dropping the `cards` table and re-running `POST /admin/refresh`. Acceptable trade-off for v1 since rebuild takes minutes.
- CORS: `CORS_ORIGINS` env, default `*`. Override in production.
- `statement_timeout = 5000ms` on the read pool via `connect_args={'options': '-c statement_timeout=5000'}`. Bounds runaway regex/`ILIKE` queries. The refresh worker uses a separate pool without the timeout.

---

## §10 Config (env)

| Var | Default | Required |
| --- | --- | --- |
| `DATABASE_URL` | — | yes |
| `HF_EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | no |
| `HF_EMBED_DIM` | `384` | yes (must match model) |
| `HF_EMBED_DEVICE` | `cpu` | no |
| `EMBED_BATCH` | `256` | no |
| `BULK_SOURCE_TYPE` | `default_cards` | no |
| `WEB_CONCURRENCY` | `1` | no — raise after profiling; each worker loads the embedding model (~80MB MiniLM) |
| `CORS_ORIGINS` | `*` | no — comma-separated list; `*` allows any origin |
| `LOG_LEVEL` | `INFO` | no |

### §6.27 `POST /cards/search/validate` (D17.6)

DSL preflight — compiles `q` through the same parser + compiler pipeline as `/cards/search` without touching the DB. Success → 200 `{ok: true, ast_summary: str, applied_default_filter: str | null}`; failure → 422 `{object: "error", status, code, message, details, did_you_mean: [str] | null}`. `did_you_mean` populates via `difflib.get_close_matches(offending_key, ALL_KNOWN_KEYS, n=3, cutoff=0.6)` where `ALL_KNOWN_KEYS` unions every supported prefix with `_REJECTED_KEYS` (§5.2), so a §5.2 typo like `st:war` still surfaces a hint. `did_you_mean` is `null` on parse errors (no key to correct). Kills the DSL-tune-by-retrying loop for agents.

### §6.26 `POST /cards/collection` (D17.5)

Scryfall-parity bulk lookup. Body `{identifiers: [{id?: uuid, oracle_id?: uuid, name?: str}]}`, capped at 75 entries. Response `{object: "list", data: [<card>...], not_found: [<identifier>...]}` — `data` preserves the request order; identifiers with no match come back under `not_found` with their original shape. One SQL:

```sql
SELECT id, oracle_id, name, raw, predicted_labels, label_scores
FROM cards
WHERE id::text = ANY(CAST(:ids AS text[]))
   OR oracle_id::text = ANY(CAST(:oids AS text[]))
   OR LOWER(name) = ANY(:names)
```

Kills the N-request loop of agents rebuilding decklists from stored ids. Over-cap body → 422 via Pydantic's `max_length=75`. Empty identifiers → 200 + empty `data` (no DB round-trip). Non-goal: printing-level identifiers (`set + collector_number`, `multiverse_id`, `mtgo_id`, `arena_id`) — §1.2 no-printings still applies.

### §6.28 `POST /cards/tag-counts`

Per-tag card counts across a set of cards, in one aggregate query. Body `{"names": [<str>...]}` (case-insensitive, `max_length=1000`). Resolves the names to `oracle_id`s and aggregates the flattened `oracle_tags` side table (same flattening as the `otag:` predicate (§5) and `/cards/:id/tags`, so a card counts for a tag's ancestors too; `COUNT(DISTINCT oracle_id)` collapses reprints). One SQL:

```sql
WITH oid AS (SELECT DISTINCT oracle_id FROM cards WHERE LOWER(name) = ANY(:names))
SELECT ot.tag AS tag, COUNT(DISTINCT ot.oracle_id) AS n
FROM oracle_tags ot JOIN oid ON oid.oracle_id = ot.oracle_id
GROUP BY ot.tag
```

Response `{"object": "tag_counts", "counts": {"<tag>": <n>, ...}}`. Empty `names` → `{"counts": {}}` with no DB round-trip. Powers the Decklist wizard's theme chips (how many deck cards serve each theme). MCP tool `tag_counts`.

### §6.29 `GET /decks/:id/evaluate`

Grades a saved deck's HEAD decklist against the `docs/edh-guide.md` template. Composes `deck_service.get_deck` + the §6.7 `_analyze_pipeline` on one connection, then a pure tips engine (`src/biblioplex/decklist/evaluate.py`, `evaluate_deck(analysis)`) derives the output — the engine is reused by the Decklist wizard's turn-7 step, so the rules live in one place.

Response `{"object": "deck_evaluation", "deck_id", "name", "summary", "tips", "analysis"}`. `summary` carries `total_cards`, `lands`, `nonland`, `curve_peak`, `coverage_pct` (null unless an inventory is synced), `overlap` (cards serving 2+ slots, qty-weighted), and per-slot `{count, target, delta}`. `tips` is an ordered list of `{code, severity, message}`: `severity: "warn"` for a hard template gap (lands below 38, a functional slot under target) or `"info"` for curve shaping (off-peak, MV5 5-drop allergy), light plan, overlap, and partial ownership. The full §6.7 payload rides along under `analysis` so a client renders the curve/slot bars without a second call. Unknown/malformed id → `404 not_found`. MCP tool `evaluate_deck`.

---

## §11 Milestones (driver for `todo.md`)

- **M0 Scaffold** — repo, `docker-compose.yml`, `schema.sql`, FastAPI app skeleton, health check.
- **M1 Ingest** — bulk download, stream-parse, staging + collapse, `POST /admin/refresh`.
- **M2 Card by ID + named** — `GET /cards/:id`, `GET /cards/named?exact=&fuzzy=`, raw-JSON round-trip.
- **M3 Search DSL** — parser, AST, SQL translation for all operators in §5.1.
- **M4 Search endpoint** — `GET /cards/search`, pagination, ordering.
- **M5 Embeddings** — model load, batch encode at end of refresh.
- **M6 Similar endpoint** — `GET /cards/:id/similar`.
- **M7 Projection** — `?projection=` on all three endpoints.
- **M8 Docs & smoke** — README usage, curl examples, agent-friendly OpenAPI tags.
- **M12 Decklist analyze + slot substitutes** — `POST /decks/analyze` (§6.7) and `GET /cards/:id/substitutes` (§6.8). Consumer rung for the rules-layer classifier and `predicted_labels` columns shipped under `docs/design/template-classifier-plan.md` Step 5.

---

## §12 Open questions (queue as drift)

- §5.1 `is:permanent` / `is:spell` exact derivation rules (follow Scryfall: permanent = artifact ∨ creature ∨ enchantment ∨ land ∨ planeswalker ∨ battle; spell = non-land).
- §7 — depth limit on dotted paths? Default cap at 6; reject deeper.
- §3.4 — reversible-card row `id` derivation: Scryfall provides per-face object UUIDs in some cases via the API but not via bulk. Choose `<parent_id>:0` / `:1` synthetic if absent. Confirm no downstream collision risk with real Scryfall UUIDs (none — synthetic uses `:` separator, invalid in UUID).
