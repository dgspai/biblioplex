-- Biblioplex schema (per tech_spec.md §3, §9).
-- Single-pass bootstrap. No Alembic. Re-running requires DROP TABLE first.
--
-- The cards.embedding vector dimension is hard-coded to 384 (matching
-- HF_EMBED_DIM default for sentence-transformers/all-MiniLM-L6-v2 per §10).
-- Changing the embedding model requires the manual re-embed snippet in §8.2.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- PG17 marks array_to_string as STABLE (locale-sensitive). Generated columns
-- require IMMUTABLE expressions, so wrap it. Safe here — keywords are ASCII.
CREATE OR REPLACE FUNCTION immutable_array_join(text[], text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT array_to_string($1, $2) $$;

-- §3.1 cards table.
-- Search-projection columns (name_search/type_search/oracle_search/
-- mana_cost_search/colors_eff) are NOT GENERATED — populated by ingest per
-- §3.3 (Postgres forbids subqueries/jsonb_array_elements in generated cols).
CREATE TABLE IF NOT EXISTS cards (
    id                 uuid        PRIMARY KEY,
    oracle_id          uuid        NOT NULL UNIQUE,
    name               text        NOT NULL,
    type_line          text,
    oracle_text        text,
    mana_cost          text,
    cmc                numeric,
    colors             text[],
    color_identity     text[],
    keywords           text[],
    produced_mana      text[],
    power              text,
    toughness          text,
    loyalty            text,
    defense            text,
    layout             text,
    rarity             text,
    legalities         jsonb,
    card_faces         jsonb,
    all_parts          jsonb,
    released_at        date,

    -- §3.1 D1.9: EDHrec rank + cheapest-printing USD price.
    -- `edhrec_rank` is per-oracle, passed through from Scryfall verbatim.
    -- `price_usd` is the MIN over all printings of the same oracle_id,
    -- computed at collapse time via a window function (see collapse.py).
    edhrec_rank        int,
    price_usd          numeric,

    -- Face-flattened search columns (ingest-populated, see §3.3).
    name_search        text        NOT NULL,
    type_search        text        NOT NULL,
    oracle_search      text        NOT NULL,
    mana_cost_search   text        NOT NULL,
    colors_eff         text[]      NOT NULL,

    -- §5.1: union of `games` across every printing of this oracle, populated
    -- at collapse time via an `array_agg(DISTINCT ...)` CTE so `game:arena`
    -- matches a card that started paper-only and later picked up Arena.
    -- The kept printing's `raw->'games'` is the original (first-printing)
    -- array; `games_eff` is the union and is what the search DSL reads.
    games_eff          text[]      NOT NULL DEFAULT '{}',

    -- §6.1: canonical WUBRG-cycle position 0–31 for `order=color`.
    -- Computed in Python at ingest from `colors_eff` (see ingest/color_sort.py).
    -- Default 0 (colorless) covers the pre-ingest state when schema is
    -- bootstrapped; real values land at the next refresh.
    color_sort_key     int         NOT NULL DEFAULT 0,

    -- Sort key for order=name (leading "A "/"An "/"The " stripped, lowercased).
    -- PG17 rejects ILIKE / regex 'i' flag in generated cols (not immutable);
    -- lower() + plain regex is immutable and equivalent here.
    name_sort_key      text GENERATED ALWAYS AS (
        regexp_replace(lower(name), '^(a|an|the)\s+', '')
    ) STORED,

    -- §3.1: legendary creature OR oracle text contains "can be your commander".
    is_commander       boolean GENERATED ALWAYS AS (
        lower(type_line) LIKE '%legendary%'
        AND (
            lower(type_line) LIKE '%creature%'
            OR position('can be your commander' in lower(oracle_search)) > 0
        )
    ) STORED,

    -- §8.3: embedding source text.
    -- Normalized so MiniLM attention isn't burned on noise that doesn't
    -- carry mechanical meaning: card-name self-references (replaced with
    -- '~', Scryfall's own placeholder), mana symbols ({W}, {2}, {T}, ...),
    -- and reminder text in parens. Final whitespace collapsed.
    -- All ops are IMMUTABLE so the STORED generated column is legal.
    embed_source       text GENERATED ALWAYS AS (
        trim(regexp_replace(
            regexp_replace(
                regexp_replace(
                    coalesce(type_search, '')
                    || ' ' || coalesce(replace(oracle_search, name, '~'), '')
                    || ' ' || coalesce(immutable_array_join(keywords, ' '), ''),
                    '\([^)]*\)', ' ', 'g'     -- (reminder text)
                ),
                '\{[^}]*\}', ' ', 'g'         -- {W}{U}{T} mana/tap symbols
            ),
            '\s+', ' ', 'g'                   -- collapse repeated whitespace
        ))
    ) STORED,

    embedding          vector(384),

    -- D15.24: data-driven "any number of copies" exemption for the
    -- legality checker. Matches oracle text patterns from cards whose own
    -- text overrides the format's default copy cap:
    --   * "A deck can have any number of cards named X" (Rat Colony,
    --      Relentless Rats, Dragon's Approach, Persistent Petitioners…)
    --   * "A deck can have up to <word> cards named X" (Seven Dwarves,
    --      Nazgûl, Hare Apparent)
    -- Both variants are treated as "not bounded by the format's 1/4-copy
    -- cap" — the checker uses this flag instead of a curated frozen set,
    -- killing the drift class of bug where new "any-number" cards ship
    -- without a code update. Kept STORED (not virtual) so the regex fires
    -- once at collapse rather than per-row on every legality request.
    has_unlimited_copies boolean GENERATED ALWAYS AS (
        COALESCE(oracle_search, '') ~*
        'a deck can have (any number of|up to [a-z]+) cards named'
    ) STORED,

    -- D16.3: delta-refresh gate. Records `md5(oracle_search)` at the moment
    -- the current `embedding` was written; owned entirely by the embed pass
    -- (collapse never touches it). Next refresh's embed SELECT compares the
    -- stored value against a freshly-computed `md5(oracle_search)` on each
    -- row — a match means the text hasn't changed since last embed, so the
    -- row drops out of the pending set. DEFAULT '' guarantees the first
    -- refresh after an in-place ALTER re-embeds every row exactly once
    -- (empty hash IS DISTINCT FROM every real md5 output).
    oracle_text_hash   text        NOT NULL DEFAULT '',

    -- Rules-layer classifier output (template-classifier-plan.md Step 5).
    -- `predicted_labels` = subset of LABELS scoring >= 0.5 in
    -- `classify_by_rules`; defaults to empty so freshly-collapsed rows
    -- (pre-classify in the refresh runner) won't blow up the read path.
    -- `label_scores` = full {label: score} map for UI display + tuning.
    -- Populated by ingest/refresh.py:default_classify post-embed.
    predicted_labels   text[]      NOT NULL DEFAULT '{}'::text[],
    label_scores       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- D15.3: per-label rule ids that fired. Shape: `{label: [rule_id, ...]}`.
    -- Sourced from `classify_by_rules_ex` alongside `label_scores`.
    label_reasons      jsonb       NOT NULL DEFAULT '{}'::jsonb,

    raw                jsonb       NOT NULL
);

-- §3.2 indexes (excluding cards_embedding_hnsw — built post-ingest by
-- the refresh runner per §4.2 step 6 via HNSW_BUILD_SQL; bulk-then-index
-- is ~10× faster than incremental for our volume).
CREATE INDEX IF NOT EXISTS cards_name_trgm
    ON cards USING gin (name_search gin_trgm_ops);
CREATE INDEX IF NOT EXISTS cards_oracle_trgm
    ON cards USING gin (oracle_search gin_trgm_ops);
CREATE INDEX IF NOT EXISTS cards_type_trgm
    ON cards USING gin (type_search gin_trgm_ops);
CREATE INDEX IF NOT EXISTS cards_colors_gin
    ON cards USING gin (colors_eff);
CREATE INDEX IF NOT EXISTS cards_games_eff_gin
    ON cards USING gin (games_eff);
CREATE INDEX IF NOT EXISTS cards_color_identity_gin
    ON cards USING gin (color_identity);
CREATE INDEX IF NOT EXISTS cards_keywords_gin
    ON cards USING gin (keywords);
CREATE INDEX IF NOT EXISTS cards_produced_gin
    ON cards USING gin (produced_mana);
CREATE INDEX IF NOT EXISTS cards_legalities_gin
    ON cards USING gin (legalities jsonb_path_ops);
CREATE INDEX IF NOT EXISTS cards_is_commander_idx
    ON cards (is_commander) WHERE is_commander;
CREATE INDEX IF NOT EXISTS cards_name_sort_key_idx
    ON cards (name_sort_key);

-- §6.4 D10.10: supporting indexes for `/cards/named`.
--   * `cards_name_lower_idx` — functional btree backing the exact-mode
--     query `LOWER(name) = LOWER(:name)`. Without it Postgres seq-scans
--     ~30k rows on every exact lookup.
--   * `cards_name_sort_key_trgm` — pg_trgm GIN backing the fuzzy-mode
--     query `similarity(name_sort_key, :name) >= 0.4 ORDER BY similarity(...)`.
--     The existing `cards_name_sort_key_idx` btree above stays — it
--     serves equality + `ORDER BY name_sort_key` for `order=name` and
--     coexists with this GIN (different access paths).
-- Both are `IF NOT EXISTS` so an operator who has already booted the
-- stack can re-apply just these statements to pick up the indexes
-- without the destructive `docker compose down -v` reset path.
CREATE INDEX IF NOT EXISTS cards_name_lower_idx
    ON cards (lower(name));
-- Bug.21: `/cards/autocomplete` fires `LOWER(name) LIKE :prefix || '%'`.
-- The btree above uses the default `text_ops` opclass, which does NOT
-- back LIKE prefix patterns — the planner would seq-scan ~30k rows on
-- every keystroke. `text_pattern_ops` supports `LIKE 'foo%'` as an index
-- range scan. Coexists with `cards_name_lower_idx` above — equality uses
-- the collation-aware idx; LIKE prefix uses this one.
CREATE INDEX IF NOT EXISTS cards_name_lower_pattern_idx
    ON cards (lower(name) text_pattern_ops);
CREATE INDEX IF NOT EXISTS cards_name_sort_key_trgm
    ON cards USING gin (name_sort_key gin_trgm_ops);

-- §3.1 D1.9: btree on rank + price for order=edhrec / order=usd.
-- Explicit NULLS LAST on the ASC default so unranked/unpriced cards sink
-- under both ASC and DESC sorts (NULLS LAST is PG's default for ASC, but
-- being explicit keeps the index usable when callers add DESC NULLS LAST).
CREATE INDEX IF NOT EXISTS cards_edhrec_rank_idx
    ON cards (edhrec_rank NULLS LAST);
CREATE INDEX IF NOT EXISTS cards_price_usd_idx
    ON cards (price_usd NULLS LAST);

-- §6.1: btree on the WUBRG-cycle position for `order=color`.
CREATE INDEX IF NOT EXISTS cards_color_sort_key_idx
    ON cards (color_sort_key);

-- template-classifier-plan.md Step 5: GIN on the predicted-label array
-- backs the DSL `pred:<label>` clause (`predicted_labels @> ARRAY[X]`).
CREATE INDEX IF NOT EXISTS cards_predicted_labels_gin
    ON cards USING gin (predicted_labels);

-- D10.14: partial btree backing the embed loop's paginated pending
-- select (`WHERE embedding IS NULL ORDER BY id LIMIT :batch`, see
-- embed/batch.py:SELECT_PENDING_SQL). Without it, each page scans an
-- increasingly-late NOT-NULL prefix as embeddings fill in — O(N/batch
-- × N) total work. Partial index only holds NULL rows; the planner
-- picks it automatically for the `IS NULL` predicate. IF NOT EXISTS
-- so operators can apply this statement in place.
CREATE INDEX IF NOT EXISTS cards_embedding_pending_idx
    ON cards (id) WHERE embedding IS NULL;

-- Oracle tags (Scryfall Tagger). One row per (oracle_id, tag-slug). Tag
-- hierarchy is flattened at ingest — parents copy their descendants'
-- taggings — so the DSL `otag:foo` predicate is a flat indexed lookup.
-- TRUNCATEd and rebuilt every refresh; no FK to cards (refresh order
-- handles the dependency and FK adds nothing).
CREATE TABLE IF NOT EXISTS oracle_tags (
    oracle_id uuid NOT NULL,
    tag       text NOT NULL,
    PRIMARY KEY (oracle_id, tag)
);
CREATE INDEX IF NOT EXISTS oracle_tags_tag_idx ON oracle_tags (tag);

-- Portuguese printed names → oracle_id. One row per (printed_name,
-- oracle_id): a name can map to more than one oracle (rare), and an
-- oracle can carry several printed spellings across sets. Populated
-- OUT-OF-BAND by `scripts/build_pt_names.py` from Scryfall's `all_cards`
-- bulk (lang=pt) — NOT by the refresh pipeline, since PT printed names
-- only change when a new set is released and `all_cards` is ~10× the
-- `default_cards` the refresh streams. No FK to cards: the script writes
-- oracle_ids Scryfall vouches for and a stale row simply never matches.
-- IF NOT EXISTS so existing deployments apply the DDL in-place; the
-- commit body carries the exec recipe.
CREATE TABLE IF NOT EXISTS pt_names (
    printed_name text NOT NULL,
    oracle_id    uuid NOT NULL,
    PRIMARY KEY (printed_name, oracle_id)
);
-- btree on the folded name backs the exact pass (LOWER(printed_name) =
-- LOWER(:input)); GIN trgm backs the fuzzy fallback, mirroring the
-- cards_name_* pair used by decklist/resolve.py.
CREATE INDEX IF NOT EXISTS pt_names_lower_idx
    ON pt_names (LOWER(printed_name));
CREATE INDEX IF NOT EXISTS pt_names_lower_trgm
    ON pt_names USING gin (LOWER(printed_name) gin_trgm_ops);

-- D14.1 Commander Spellbook combos. One row per known combo, keyed on the
-- source's stable id. `oracle_ids` is the ordered list of cards required
-- (uuid[] so PG's `<@` array-subset operator can efficiently gate deck
-- membership checks). `results` is a flat text[] (e.g. `{"Infinite mana",
-- "Win the game"}`) — kept in-array rather than jsonb because we only
-- ever surface it as-is. Optional data source — the refresh runner skips
-- this ingest entirely when COMBOS_SOURCE_URL is empty. IF NOT EXISTS so
-- existing deployments can apply the DDL in-place before triggering the
-- next refresh; the D14.1 commit body carries the exec recipe.
CREATE TABLE IF NOT EXISTS combos (
    id             text  PRIMARY KEY,
    oracle_ids     uuid[]  NOT NULL,
    color_identity text[]  NOT NULL DEFAULT '{}',
    prereqs        text,
    steps          text,
    results        text[]  NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS combos_oracle_ids_gin
    ON combos USING gin (oracle_ids);
CREATE INDEX IF NOT EXISTS combos_color_identity_gin
    ON combos USING gin (color_identity);
-- D16.7: pg_trgm GIN on the prose columns backs `/combos/search?q=`.
-- Without these, `WHERE COALESCE(prereqs, '') ILIKE :q OR COALESCE(steps, '')
-- ILIKE :q` seq-scans the combos table (~96k rows on a full Spellbook v2
-- ingest). IF NOT EXISTS so existing deployments can apply the two
-- statements in-place before the next refresh.
CREATE INDEX IF NOT EXISTS combos_prereqs_trgm
    ON combos USING gin (prereqs gin_trgm_ops);
CREATE INDEX IF NOT EXISTS combos_steps_trgm
    ON combos USING gin (steps gin_trgm_ops);

-- §4.2 step 3: staging table fed by ingest before the collapse step.
-- Same shape as `cards` MINUS:
--   * generated columns (name_sort_key, is_commander, embed_source)
--   * `embedding` (populated post-collapse, §4.2 step 5)
-- No indexes — staging is write-once-per-refresh, then truncated.
-- No PRIMARY KEY: duplicate `id`s across reprints are expected; the
-- collapse step's DISTINCT ON (oracle_id) does the picking, and the
-- INSERT INTO cards ... ON CONFLICT (oracle_id) refreshes the oracle's row.
CREATE TABLE IF NOT EXISTS cards_staging (
    id                 uuid        NOT NULL,
    oracle_id          uuid        NOT NULL,
    name               text        NOT NULL,
    type_line          text,
    oracle_text        text,
    mana_cost          text,
    cmc                numeric,
    colors             text[],
    color_identity     text[],
    keywords           text[],
    produced_mana      text[],
    power              text,
    toughness          text,
    loyalty            text,
    defense            text,
    layout             text,
    rarity             text,
    legalities         jsonb,
    card_faces         jsonb,
    all_parts          jsonb,
    released_at        date,
    edhrec_rank        int,
    price_usd          numeric,
    name_search        text        NOT NULL,
    type_search        text        NOT NULL,
    oracle_search      text        NOT NULL,
    mana_cost_search   text        NOT NULL,
    colors_eff         text[]      NOT NULL,
    -- §5.1: per-printing raw `games` array (paper/mtgo/arena/...). Collapse
    -- unions these across printings into `cards.games_eff` via a CTE.
    games              text[],
    -- §6.1: WUBRG-cycle position (per-oracle, identical across printings).
    color_sort_key     int,
    raw                jsonb       NOT NULL
);

-- D18.1: Inventory tracking table. Stores user owned card quantities,
-- linked to `cards.oracle_id`. Includes optional set_code/set_name from export.
CREATE TABLE IF NOT EXISTS inventory (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    oracle_id          uuid        NOT NULL REFERENCES cards(oracle_id) ON DELETE CASCADE,
    quantity           int         NOT NULL DEFAULT 1 CHECK (quantity >= 0),
    foil_quantity      int         NOT NULL DEFAULT 0 CHECK (foil_quantity >= 0),
    card_name          text        NOT NULL,
    set_code           text,
    set_name           text,
    source             text        NOT NULL DEFAULT 'deckbox',
    -- D18.13: price of the printing the user actually owns, as reported by
    -- the export. `cards.price_usd` is the CHEAPEST printing across the
    -- whole oracle (D14.11), so joining to it under-values a collection.
    source_price_usd   numeric(10,2),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS inventory_oracle_id_idx ON inventory (oracle_id);
CREATE INDEX IF NOT EXISTS inventory_card_name_lower_idx ON inventory (lower(card_name));

-- D18.1: Inventory sync settings and audit metadata table.
CREATE TABLE IF NOT EXISTS inventory_sync_settings (
    id                 int         PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    deckbox_url        text,
    last_synced_at     timestamptz,
    last_status        text,
    last_card_count    int
);

-- D18.8: Wishlist table — cards the user WANTS, from a Deckbox wishlist
-- export. Deliberately a separate table from `inventory` rather than a
-- `list_type` column on it: every ownership query (DSL `is:owned`, deck
-- coverage, `/inventory/oracle-ids`) would otherwise need an `AND
-- list_type = 'owned'` filter that is silently wrong when forgotten.
-- No `foil_quantity` — a wishlist records "I want N of this", not finish.
-- No companion sync-settings table either: `max(updated_at)` IS the
-- last-synced timestamp, so there is nothing extra to persist.
CREATE TABLE IF NOT EXISTS wishlist (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    oracle_id          uuid        NOT NULL REFERENCES cards(oracle_id) ON DELETE CASCADE,
    quantity           int         NOT NULL DEFAULT 1 CHECK (quantity >= 0),
    card_name          text        NOT NULL,
    set_code           text,
    set_name           text,
    source             text        NOT NULL DEFAULT 'deckbox',
    source_price_usd   numeric(10,2),   -- D18.13, see `inventory`
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS wishlist_oracle_id_idx ON wishlist (oracle_id);
CREATE INDEX IF NOT EXISTS wishlist_card_name_lower_idx ON wishlist (lower(card_name));

-- D18.17: import history. Every CSV that goes through a sync endpoint is
-- kept verbatim so an operator can re-download exactly what was applied and
-- diff two syncs. Content lives in a `bytea` rather than on disk: a Deckbox
-- export is <1 MB, they arrive a few times a month, and a column needs no
-- volume mount, no path config, and no separate backup story. Revisit if
-- this ever holds more than a few hundred MB.
CREATE TABLE IF NOT EXISTS uploads (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    kind               text        NOT NULL CHECK (kind IN ('inventory', 'wishlist')),
    filename           text        NOT NULL,
    content            bytea       NOT NULL,
    size_bytes         int         NOT NULL,
    mode               text        NOT NULL,
    total_cards        int         NOT NULL DEFAULT 0,
    distinct_cards     int         NOT NULL DEFAULT 0,
    unmatched_count    int         NOT NULL DEFAULT 0,
    uploaded_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS uploads_uploaded_at_idx ON uploads (uploaded_at DESC);

-- D19.1: stored decks. `decks` is identity + user-owned metadata ONLY —
-- the card content lives in `deck_versions` and the current decklist IS
-- the highest-numbered version. There is deliberately no
-- `decks.decklist` column: it would be a cache of `deck_versions.decklist`
-- with nothing keeping the two honest, so any write that updated one and
-- not the other would silently detach a deck from its own history.
--
-- Content is stored as the decklist TEXT rather than resolved
-- `(oracle_id, qty)` rows because every `/decks/*` endpoint already
-- consumes a decklist string: text needs no resolution at write time, no
-- re-serialisation at read time, and cannot rot when a refresh changes
-- what a name resolves to. The cost is that cross-deck questions ("which
-- decks run Sol Ring") are a text scan; the upgrade path is a DERIVED
-- `deck_cards` index rebuilt on write, not a change of source of truth.
--
-- `commander` is an override, not a cache. It is normally parsed from the
-- versioned decklist's `Commander` section, but three of the reference
-- exports in `data/example_deck_exports/` (every Deckbox one) carry no
-- such section, so without a column those decks could never drive
-- `/decks/legality` or `/decks/bracket`. Effective value is
-- `COALESCE(decks.commander, <parsed from latest version>)`.
--
-- The three service URLs are three nullable columns rather than a
-- `deck_links(deck_id, service, url)` table: the set is fixed at three and
-- does not grow. Same reasoning as `inventory_sync_settings.deckbox_url`,
-- and they are load-bearing — a stored Moxfield/Archidekt URL is what
-- D14.18 re-fetches to append a new version. Deckbox cannot be fetched
-- automatically (Bug.32 — the export endpoint returns an HTML login page),
-- so `deckbox_url` is a link only.
--
-- IF NOT EXISTS so existing deployments can apply this in-place; the
-- D19.1 commit body carries the exec recipe.
CREATE TABLE IF NOT EXISTS decks (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name           text        NOT NULL,
    format         text        NOT NULL DEFAULT 'commander',
    commander      text,
    -- D19.11: same override semantics as `commander` — normally parsed
    -- from the versioned decklist's `Companion` section, set explicitly
    -- for exports that carry no such section (which is all of them today).
    companion      text,
    notes          text,
    deckbox_url    text,
    moxfield_url   text,
    archidekt_url  text,
    -- D19.16: nested folder as a path string (`Commander/Budget`).
    -- NULL means unfiled. See the folder block below for why this is a
    -- column rather than a `folders` table.
    folder         text,
    -- D19.33: git-like HEAD for the version model below. NULL means
    -- "newest version" — every existing deck keeps working with zero
    -- backfill, and the columns ship before D19.34 wires anything to
    -- read them. `head_pinned` marks an explicit promote (`head_version`
    -- set on purpose) so the next import does not silently steal head
    -- back — git's detached HEAD in one boolean.
    head_version   integer,
    head_pinned    boolean     NOT NULL DEFAULT false,
    -- D19.38: the ONE branch allowed to move head. NULL keeps today's
    -- back-compat behaviour ("follow every import"), so no backfill is
    -- needed. A deck synced from two services otherwise has its current
    -- content flip with sync ORDER — whichever branch imported last steals
    -- head, dragging `coverage_pct` / `missing_usd` / the card table with
    -- it even though nothing is actually broken.
    trunk_branch   text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- D19.1: append-only history. One row per state the decklist has ever
-- been in — imports AND manual edits both append, so a diff between any
-- two points is complete rather than skipping hand-made changes.
--
-- `decklist` is the canonical (section-parsed, re-serialised) text and is
-- what diffs and every `/decks/*` endpoint read. `raw_content` is the
-- original uploaded file kept verbatim so a download hands back the exact
-- CSV or HTML that was imported and a later parser fix can be replayed
-- against its source; it is NULL for `paste` / `edit` / `restore`
-- versions, which have no file. Same bytea-not-disk reasoning as
-- `uploads` (D18.17): a deck export is a few KB, a column needs no volume
-- mount, no path config and no separate backup story.
--
-- `restore` copies an old version's text into a NEW version rather than
-- truncating back to it — history is never rewritten.
--
-- No retention policy: 500 edits of one deck is ~2 MB. Add pruning if a
-- real deck ever gets there.
CREATE TABLE IF NOT EXISTS deck_versions (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    deck_id          uuid        NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    version          int         NOT NULL CHECK (version >= 1),
    decklist         text        NOT NULL,
    raw_content      bytea,
    filename         text,
    -- D19.41: 'fork' marks a deck's version 1 as seeded from ANOTHER
    -- deck's HEAD rather than an import/paste/edit/restore of its own
    -- history — none of the other five values are honest about that
    -- origin (it isn't a restore: nothing is being reapplied onto the
    -- SAME deck's timeline).
    source           text        NOT NULL
                                 CHECK (source IN ('upload', 'paste', 'url', 'edit', 'restore', 'fork')),
    size_bytes       int,
    total_cards      int         NOT NULL DEFAULT 0,
    distinct_cards   int         NOT NULL DEFAULT 0,
    unresolved_count int         NOT NULL DEFAULT 0,
    -- D19.33: one branch per source site, matching the three service-URL
    -- columns above plus `main`. No `branches` table — a branch has no
    -- independent existence, it IS the set of versions sharing a label,
    -- the same call D19.16 made for folders. Version numbers stay GLOBAL
    -- per deck (this column is a label plus a filter, not a namespace),
    -- so `UNIQUE (deck_id, version)` below and every shipped
    -- `/decks/{id}/versions/{n}` URL keep working unchanged.
    branch           text        NOT NULL DEFAULT 'main'
                                 CHECK (branch IN ('main', 'deckbox', 'moxfield', 'archidekt')),
    -- D19.33: a commit-style message per version. Nullable — most
    -- versions (plain imports) have nothing worth saying.
    message          text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (deck_id, version)
);

-- Serves both reads that matter: "latest version of this deck" (LIMIT 1)
-- and the newest-first history listing.
CREATE INDEX IF NOT EXISTS deck_versions_deck_idx
    ON deck_versions (deck_id, version DESC);

-- D19.33: latest version ON A GIVEN BRANCH — the lookup D19.34's
-- per-branch dedup and head auto-follow both need.
CREATE INDEX IF NOT EXISTS deck_versions_branch_idx
    ON deck_versions (deck_id, branch, version DESC);

-- D19.16: deck folders. A nullable PATH STRING on the deck, not a
-- `folders` table with an FK. A folder here has no independent existence:
-- it is the set of decks whose `folder` shares a prefix. That removes the
-- only two failure modes a table would have protected against — a folder
-- cannot be orphaned, and a cycle cannot be constructed — at the cost of
-- typos and case variants silently creating phantom folders
-- (`Commander` and `commander` are two folders). D19.17's one-call
-- subtree rename is the mitigation for that cost.
--
-- `text_pattern_ops` is load-bearing. Subtree listing runs
-- `folder LIKE 'Commander/%'`, and the default btree opclass does not
-- serve a prefix LIKE — the index would be built and never used. Same
-- reasoning as `cards_name_lower_pattern_idx`.
--
-- The in-place recipe for existing deployments is in the D19.16 commit body.
CREATE INDEX IF NOT EXISTS decks_folder_idx
    ON decks (folder text_pattern_ops);

-- D19.19: DERIVED index over the decklist text. Not a second source of
-- truth — `deck_versions.decklist` remains authoritative and these rows
-- are regenerated from it on every version write and after a card
-- refresh, so the table can be dropped and rebuilt at will. That is what
-- keeps D19.1's "text, not rows" decision intact while still allowing
-- cross-deck queries.
--
-- It exists because inventory coverage depends on BOTH the deck and the
-- inventory, so it cannot be cached on the deck (a sync stales it
-- instantly), and sorting the library by coverage means computing it for
-- every deck before paginating — a full resolve per deck per request
-- against text storage.
--
-- `oracle_id` is NULLABLE with `card_name` retained: an unresolved name
-- must still count toward the coverage DENOMINATOR, or every percentage
-- silently inflates as resolution degrades.
--
-- PARTITION INVARIANT: unlike `parse_sections` (D19.10, where commander
-- and companion deliberately sit inside `main`), these rows ARE a
-- partition — commander, main-minus-commander-and-companion, sideboard.
-- SUM(qty) is therefore the deck size. Writing the section buckets in
-- directly would report a 101-card Commander deck.
--
-- The in-place recipe for existing deployments is in the D19.19 commit body.
CREATE TABLE IF NOT EXISTS deck_cards (
    deck_id    uuid NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    section    text NOT NULL CHECK (section IN ('commander', 'main', 'sideboard')),
    card_name  text NOT NULL,
    oracle_id  uuid,
    qty        int  NOT NULL CHECK (qty > 0)
);

CREATE INDEX IF NOT EXISTS deck_cards_deck_idx ON deck_cards (deck_id);
CREATE INDEX IF NOT EXISTS deck_cards_oracle_idx ON deck_cards (oracle_id);
