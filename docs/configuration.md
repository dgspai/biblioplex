# Configuration

## Config (env)

Environment variables the API reads. Compose supplies most of them; see `.env.example`.

| Var | Default | Required | Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | — | set by compose | `postgresql+psycopg://user:pass@host:5432/db`. `docker-compose.yml` builds it from `DB_PASSWORD`, so under compose don't set it. Required only when running the API outside compose. |
| `DB_PASSWORD` | — | yes (compose) | substituted into `DATABASE_URL` by `docker-compose.yml`. Set it before the first `docker compose up`: Postgres keeps the original password in the `pgdata` volume, so changing it later breaks the API. Fix with `ALTER USER` in the db container, or `docker compose down -v` (destroys all data). |
| `HF_EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | no | swapping requires `scripts/reembed.py`. Weights cache in the `hfcache` volume (`HF_HOME=/models`); the model loads from that cache when present, and HuggingFace is contacted only when it isn't cached (normally the first start). |
| `HF_EMBED_DIM` | `384` | no | must match the model's `get_sentence_embedding_dimension()`; mismatch fails fast at startup. Changing dim requires `ALTER TABLE cards DROP COLUMN embedding; ADD COLUMN embedding vector(<N>);` then re-embed. |
| `HF_EMBED_DEVICE` | `cpu` | no | `cpu` or `cuda` |
| `EMBED_BATCH` | `256` | no | rows per encode pass during refresh |
| `BULK_SOURCE_TYPE` | `default_cards` | no | `default_cards` or `oracle_cards` |
| `WEB_CONCURRENCY` | `1` | no | each worker loads its own embedding model (~90MB MiniLM-L6) |
| `CORS_ORIGINS` | empty | no | comma-separated list of browser origins allowed to call the API cross-site. Empty (default) allows none, and cross-site `POST`/`PUT`/`DELETE` requests are rejected by an `Origin` check; requests without an `Origin` header (curl, MCP clients) are unaffected. See [SECURITY.md](../SECURITY.md). |
| `LOG_LEVEL` | `INFO` | no | |
| `SEARCH_DEFAULT_FILTER` | empty (`.env.example` sets `game:paper f:commander`) | no | DSL fragment AND-ed onto every `/cards/search` query before parse. User `q` is wrapped in parens so DSL `OR` inside it still binds before the AND. Example: `f:commander` to scope a deployment to EDH-legal cards. Adds to the rendered query length; very long filters can push otherwise-valid `q` past the 1000-char cap and surface a filter-aware 422. |
| `AUTO_REFRESH_IF_EMPTY` | `true` | no | When true, the `lifespan` hook checks `cards` at boot and kicks `/admin/refresh` as a background task if it's empty (or if no card has an embedding yet, i.e. an interrupted first ingest). Set false on read-only replicas. |
| `MCP_AUTH_TOKEN` | empty | no | Bearer token required on every `/mcp` request when set (generate with `openssl rand -hex 32`). Empty leaves `/mcp` open. REST routes are not covered. See [mcp.md](mcp.md#authentication). |
| `BIND_ADDR` (compose) | `127.0.0.1` | no | Host interface the api (8000) and web (8080) ports are published on. Only set `0.0.0.0` on a trusted network; see [SECURITY.md](../SECURITY.md). |
| `DEFAULT_Q` (web) | `game:paper f:commander` | no | Initial DSL query the UI pre-fills the search box with and runs on first paint. Rendered into `web/app.js` at container start via `envsubst` (whitelist scoped so JS template literals pass through). Set empty to disable the auto-search. |
| `COMBOS_SOURCE_URL` | `https://json.commanderspellbook.com/variants.json.gz` | no | Commander Spellbook bulk combo file (gzipped or plain `variants.json`). Defaults to Spellbook's published bulk file; set it empty to disable the combo ingest step entirely. When set, refreshes fetch the URL after the oracle-tag ingest, resolve card names against `cards`, and populate the `combos` side table. Best-effort: fetch/parse failure keeps the previous combos intact and the cards refresh committed. See [architecture.md](architecture.md#scryfall-data-sources) for the format. |
