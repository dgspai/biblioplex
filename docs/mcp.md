# MCP server

## Install on an AI agent (MCP)

Biblioplex exposes its HTTP API as Model Context Protocol tools at `/mcp` (streamable HTTP transport, via `fastapi-mcp`): every route except `POST /admin/refresh` and `GET /health/live`, which is 76 tools. **20 of them write data**, including `delete_deck`, `clear_inventory_collection` and `clear_wishlist_collection`, so an MCP client can change or wipe your collection and deck library. One command to wire it into Claude Code against a running stack:

```bash
claude mcp add --transport http biblioplex http://localhost:8000/mcp
```

Any other MCP-capable client (Claude Desktop, Cursor, Zed, Continue, custom SDK) adds the same URL as a streamable-HTTP server. For clients configured with JSON, the usual shape is below (an example: check your client's docs for the exact keys; drop `headers` if you have no token):

```json
{
  "mcpServers": {
    "biblioplex": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
    }
  }
}
```

### Authentication

**Bearer auth.** Set `MCP_AUTH_TOKEN` on the server (generate with `openssl rand -hex 32`, add to your `.env` and `docker compose exec api env | grep MCP_AUTH_TOKEN` to verify it forwarded). Clients then pass the token on every request:

```bash
claude mcp add --transport http \
  --header "Authorization: Bearer $MCP_AUTH_TOKEN" \
  biblioplex http://localhost:8000/mcp
```

Empty `MCP_AUTH_TOKEN` (default) leaves `/mcp` open. Non-empty enforces `Authorization: Bearer <token>` on every `/mcp*` request via constant-time compare (`hmac.compare_digest` — timing-attack surface exists even on a shared secret); a missing or wrong token gets a Scryfall-shaped `401`. All non-`/mcp` paths are unaffected: the REST routes, including the mutating ones, stay unauthenticated on port 8000 and behind the web container's `/api/` proxy on 8080. Set a token whenever the API is reachable by anything other than you, and read [SECURITY.md](../SECURITY.md) before exposing it.

### How tools are derived

Tools are auto-derived from the OpenAPI schema — one tool per operationId, named after the route function (`search_cards`, `analyze_decklist`, `get_similar_cards`, …). `POST /admin/refresh` is deliberately excluded from the tool list; it re-downloads Scryfall's bulk data and stays human-triggered.

The tool contract (for contributors): every new endpoint's function name = its OpenAPI operationId = its MCP tool name; decorator `summary` + `description` populate the tool card; `Query`/`Path`/`Body(description=, examples=…)` populate the parameter hints an agent sees before calling.

## Tools

76 tools, one per route, named after the route function. **Writes data?** "yes" means the tool changes stored state; "no (read-only compute)" is a `POST` that only computes over the body you send.

By group:

- **Cards and search (18):** `search_cards`, `validate_search`, `autocomplete_cards`, `get_card_by_name`, `get_card_by_id`, `random_card`, `lookup_collection`, `get_similar_cards`, `get_substitutes`, `get_card_tags`, `get_card_explain`, `get_card_related`, `get_card_combos`, `tag_counts`, `resolve_pt`, `list_tags`, `list_keywords`, `list_sets`
- **Labels (2, write):** `set_card_labels`, `clear_card_labels`
- **Combos and commanders (3):** `search_combos`, `get_combo`, `suggest_commanders`
- **Stateless decklist analysis (16):** `analyze_decklist`, `suggest_decklist`, `decklist_bracket`, `decklist_combos`, `decklist_near_combos`, `decklist_manabase`, `decklist_legality`, `decklist_upgrade`, `decklist_mulligan`, `goldfish_decklist`, `price_decklist`, `synergy_decklist`, `sideboard_suggest_decklist`, `compare_decklists`, `diff_decklists`, `export_decklist`
- **Deck library (19):** `create_deck`, `upload_deck`, `list_decks`, `get_deck`, `update_deck`, `delete_deck`, `evaluate_stored_deck`, `list_deck_cards`, `get_deck_completion_plan`, `list_deck_folders`, `rename_deck_folder`, `add_deck_version`, `list_deck_versions`, `get_deck_version`, `download_deck_version`, `restore_deck_version`, `list_deck_branches`, `fork_deck`, `set_deck_head`
- **Collection (14):** `sync_deckbox_inventory`, `upload_deckbox_inventory`, `list_inventory_cards`, `get_inventory_summary_stats`, `list_owned_oracle_ids`, `upsert_inventory_card`, `clear_inventory_collection`, `sync_wishlist_csv`, `upload_wishlist_csv`, `list_wishlist_cards`, `get_wishlist_summary_stats`, `list_wanted_oracle_ids`, `upsert_wishlist_card`, `clear_wishlist_collection`
- **Import history (3):** `list_uploads_history`, `download_upload`, `delete_upload_row`
- **Health (1):** `health`

Full list, in `tools/list` order:

| # | Tool | HTTP | Writes data? | Description |
|---|---|---|---|---|
| 1 | `health` | GET `/health` | no | Liveness + readiness probe |
| 2 | `search_cards` | GET `/cards/search` | no | DSL search over the card corpus |
| 3 | `autocomplete_cards` | GET `/cards/autocomplete` | no | Prefix + trigram name autocomplete |
| 4 | `get_card_by_name` | GET `/cards/named` | no | Single-card lookup by name (exact or fuzzy) |
| 5 | `get_similar_cards` | GET `/cards/{card_id}/similar` | no | Top-K nearest cards by embedding cosine distance |
| 6 | `get_substitutes` | GET `/cards/{card_id}/substitutes` | no | Slot-constrained top-K similar cards |
| 7 | `get_card_tags` | GET `/cards/{card_id}/tags` | no | Oracle tags (Scryfall Tagger) for a card |
| 8 | `get_card_combos` | GET `/cards/{card_id}/combos` | no | Commander Spellbook combos containing this card |
| 9 | `get_card_explain` | GET `/cards/{card_id}/explain` | no | Predicted-label explainability |
| 10 | `get_card_related` | GET `/cards/{card_id}/related` | no | Related cards (tokens, meld halves, combo pieces) |
| 11 | `random_card` | GET `/cards/random` | no | Random card (optionally satisfying a DSL filter) |
| 12 | `get_card_by_id` | GET `/cards/{card_id}` | no | Single card lookup by Scryfall UUID |
| 13 | `set_card_labels` | PUT `/cards/{card_id}/labels` | **yes** | Pin a manual slot-label override for one card |
| 14 | `clear_card_labels` | DELETE `/cards/{card_id}/labels` | **yes** | Clear a card's manual override, reverting to the rules verdict |
| 15 | `lookup_collection` | POST `/cards/collection` | no (read-only compute) | Bulk card lookup by id / oracle_id / name |
| 16 | `tag_counts` | POST `/cards/tag-counts` | no (read-only compute) | Per-tag card counts across a set of cards |
| 17 | `resolve_pt` | POST `/cards/resolve-pt` | no (read-only compute) | Resolve Portuguese card names to oracle ids |
| 18 | `validate_search` | POST `/cards/search/validate` | no (read-only compute) | DSL preflight — parse `q`, report ok/error + did_you_mean |
| 19 | `search_combos` | GET `/combos/search` | no | Search Commander Spellbook combos |
| 20 | `get_combo` | GET `/combos/{combo_id}` | no | Single Commander Spellbook combo |
| 21 | `suggest_commanders` | GET `/commanders/suggest` | no | Suggest commanders by oracle tags + color identity |
| 22 | `list_tags` | GET `/tags` | no | List oracle-tag slugs with hit counts |
| 23 | `list_keywords` | GET `/keywords` | no | List Scryfall keyword strings with hit counts |
| 24 | `list_sets` | GET `/sets` | no | List Scryfall set codes with release dates and hit counts |
| 25 | `analyze_decklist` | POST `/decks/analyze` | no (read-only compute) | Analyze a decklist against the template slot targets |
| 26 | `compare_decklists` | POST `/decks/compare` | no (read-only compute) | Compare two decklists — added, removed, changed, and slot deltas |
| 27 | `diff_decklists` | POST `/decks/diff` | no (read-only compute) | Line-level diff between two decklists |
| 28 | `export_decklist` | POST `/decks/export` | no (read-only compute) | Format a decklist in Arena / MTGO / plain-text style |
| 29 | `price_decklist` | POST `/decks/price` | no (read-only compute) | Sum a decklist's cheapest-printing USD prices with breakdowns |
| 30 | `suggest_decklist` | POST `/decks/suggest` | no (read-only compute) | Suggest fills for underfilled slot targets |
| 31 | `sideboard_suggest_decklist` | POST `/decks/sideboard-suggest` | no (read-only compute) | Suggest sideboard cards for common meta-hate slots |
| 32 | `decklist_combos` | POST `/decks/combos` | no (read-only compute) | Commander Spellbook combos present in a decklist |
| 33 | `decklist_near_combos` | POST `/decks/near-combos` | no (read-only compute) | Combos the deck is N cards away from completing |
| 34 | `synergy_decklist` | POST `/decks/synergy` | no (read-only compute) | Pairwise card co-occurrence signal (shared tags + combos) |
| 35 | `decklist_mulligan` | POST `/decks/mulligan` | no (read-only compute) | Sample opening hands and report keep-rate heuristics |
| 36 | `goldfish_decklist` | POST `/decks/goldfish` | no (read-only compute) | Deterministic goldfish (solitaire) sim over N turns |
| 37 | `decklist_bracket` | POST `/decks/bracket` | no (read-only compute) | Estimate the WOTC Commander bracket 1–5 for a decklist |
| 38 | `decklist_upgrade` | POST `/decks/upgrade` | no (read-only compute) | Propose per-card swap upgrades ranked by EDHrec-rank delta |
| 39 | `decklist_manabase` | POST `/decks/manabase` | no (read-only compute) | Recommend a mana base for a commander |
| 40 | `decklist_legality` | POST `/decks/legality` | no (read-only compute) | Check a decklist against a format's legality + build rules |
| 41 | `create_deck` | POST `/decks` | **yes** | Store a new deck from pasted text |
| 42 | `list_decks` | GET `/decks` | no | List stored decks |
| 43 | `upload_deck` | POST `/decks/upload` | **yes** | Store a new deck from an uploaded export file |
| 44 | `get_deck_completion_plan` | GET `/decks/completion-plan` | no | What to buy next to finish whole decks |
| 45 | `list_deck_folders` | GET `/decks/folders` | no | List deck folders with their counts |
| 46 | `rename_deck_folder` | POST `/decks/folders/rename` | **yes** | Rename or move a folder subtree |
| 47 | `get_deck` | GET `/decks/{deck_id}` | no | Fetch one stored deck with its current decklist |
| 48 | `update_deck` | PATCH `/decks/{deck_id}` | **yes** | Update a stored deck's metadata and/or decklist |
| 49 | `delete_deck` | DELETE `/decks/{deck_id}` | **yes** | Delete a stored deck and its whole history |
| 50 | `evaluate_stored_deck` | GET `/decks/{deck_id}/evaluate` | no | Evaluate a stored deck against the EDH template |
| 51 | `add_deck_version` | POST `/decks/{deck_id}/versions` | **yes** | Import a new version onto an existing deck |
| 52 | `list_deck_versions` | GET `/decks/{deck_id}/versions` | no | List a deck's version history |
| 53 | `list_deck_cards` | GET `/decks/{deck_id}/cards` | no | List a deck's cards as table rows with ownership |
| 54 | `list_deck_branches` | GET `/decks/{deck_id}/branches` | no | Compare every synced branch's head against the deck's HEAD |
| 55 | `get_deck_version` | GET `/decks/{deck_id}/versions/{version}` | no | Fetch one past version's decklist |
| 56 | `download_deck_version` | GET `/decks/{deck_id}/versions/{version}/download` | no | Download a past version as a file |
| 57 | `restore_deck_version` | POST `/decks/{deck_id}/versions/{version}/restore` | **yes** | Restore a past version |
| 58 | `fork_deck` | POST `/decks/{deck_id}/fork` | **yes** | Fork a deck into a new, independent deck |
| 59 | `set_deck_head` | POST `/decks/{deck_id}/head` | **yes** | Promote a version to head, or clear the pin |
| 60 | `sync_deckbox_inventory` | POST `/inventory/sync/deckbox` | **yes** | Sync collection inventory from Deckbox URL or CSV text |
| 61 | `upload_deckbox_inventory` | POST `/inventory/sync/upload` | **yes** | Sync collection inventory from an uploaded Deckbox CSV file |
| 62 | `list_inventory_cards` | GET `/inventory` | no | List owned inventory cards with pagination and search |
| 63 | `clear_inventory_collection` | DELETE `/inventory` | **yes** | Clear entire inventory collection |
| 64 | `get_inventory_summary_stats` | GET `/inventory/summary` | no | Get summary metrics of current inventory |
| 65 | `list_owned_oracle_ids` | GET `/inventory/oracle-ids` | no | Flat list of every owned oracle id |
| 66 | `upsert_inventory_card` | POST `/inventory/cards` | **yes** | Set the owned quantity of a single card |
| 67 | `sync_wishlist_csv` | POST `/wishlist/sync` | **yes** | Sync the wishlist from Deckbox CSV text |
| 68 | `upload_wishlist_csv` | POST `/wishlist/sync/upload` | **yes** | Sync the wishlist from an uploaded Deckbox CSV file |
| 69 | `list_wishlist_cards` | GET `/wishlist` | no | List wanted cards with pagination and search |
| 70 | `clear_wishlist_collection` | DELETE `/wishlist` | **yes** | Clear the entire wishlist |
| 71 | `get_wishlist_summary_stats` | GET `/wishlist/summary` | no | Get summary metrics of the wishlist |
| 72 | `list_wanted_oracle_ids` | GET `/wishlist/oracle-ids` | no | Flat list of every wanted oracle id |
| 73 | `upsert_wishlist_card` | POST `/wishlist/cards` | **yes** | Set the wanted quantity of a single card |
| 74 | `list_uploads_history` | GET `/uploads` | no | List previously imported CSV files |
| 75 | `download_upload` | GET `/uploads/{upload_id}/download` | no | Download a previously imported CSV verbatim |
| 76 | `delete_upload_row` | DELETE `/uploads/{upload_id}` | **yes** | Delete one stored import |

## Example prompts

These work against a stack with a collection and a few decks imported. The tool chain in each is what an agent typically calls; every step was checked over REST.

1. **"Which Commander-legal red-green ramp creatures do I already own, and which is most played?"**
   `search_cards(q="in:inventory pred:ramp t:creature id<=rg", order="edhrec")`, then `get_card_by_id` for details. Combines your collection (`in:inventory`) with the role classifier (`pred:`).
2. **"Evaluate my <deck name> deck. What's its bracket, which combos are in it, which am I one card away from, and what should I add for card advantage?"**
   `list_decks` → `evaluate_stored_deck` → `get_deck` → `decklist_bracket` → `decklist_combos` → `decklist_near_combos(missing=1)` → `suggest_decklist` or `search_cards(q="pred:card_advantage ...")`.
3. **"I have $50. Which of my decks can I finish first, and what exactly do I need to buy? Skip cards already on my wishlist."**
   `get_deck_completion_plan(budget=50)` → `list_wanted_oracle_ids` → `lookup_collection` for card details and prices.

## Agent-facing notes

- **No auth on the REST API, no rate limiting.** By design; `/mcp` has optional bearer auth (above). Deploy behind whatever your environment uses; see [SECURITY.md](../SECURITY.md).
- **CORS closed by default** (`CORS_ORIGINS` empty). Cross-site browser writes are rejected; see [SECURITY.md](../SECURITY.md).
- **5s statement timeout** on the read pool (`statement_timeout=5000ms`). Bounds runaway `ILIKE`/regex queries. The refresh worker uses a separate pool with no timeout.
- **Tool list size.** `tools/list` is about 86 KB, roughly 20k tokens of client context. Biblioplex has no setting to expose a subset of tools.
- **Write-tool flags.** If your client supports tool annotations, write tools are flagged.
- **Error envelope** is Scryfall-shaped for every error, including request-validation failures: `{"object":"error","status":<int>,"code":"<slug>","details":"<msg>"}`. Codes in use: `not_found`, `bad_request`, `unsupported_format`, `unsupported_operator`, `invalid_projection`.
- **Projection** uses comma-separated dotted paths; depth cap 6; missing paths resolve to `null` to keep the response shape stable across cards.
- **Response compression**: `GZipMiddleware(minimum_size=1000)` compresses responses ≥1 KB. Single-card GETs stay uncompressed; large search / analyze responses shrink dramatically. `nginx.conf` has no gzip directive, so in-app compression is not redundant.
- **HTTP caching.** `/cards/search` and `/cards/:id` send no `ETag` and never return `304`; every response is fresh (`Cache-Control: no-cache`), so collection, wishlist and label changes show up in the next search. Only the catalog endpoints (`/tags`, `/keywords`, `/sets`) send a weak `ETag` keyed on the last completed refresh and answer `If-None-Match` with a bodyless `304`.
