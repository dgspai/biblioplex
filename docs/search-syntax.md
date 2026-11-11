# Search syntax

`GET /cards/search?q=...` (the search box in the web UI, and the `search_cards` MCP tool) takes a Scryfall-style query. Most of [Scryfall's syntax](https://scryfall.com/docs/syntax) works as-is: `t:`, `o:`, `c:`, `id:`, `cmc`, `pow`, `kw:`, `is:commander`, `f:`, `game:`, `usd`, `otag:`/`function:`, regex names, `OR`, `-` negation and parentheses. Biblioplex adds a few operators on top, listed below.

**Results are scoped by `SEARCH_DEFAULT_FILTER`.** The server ANDs this fragment onto every query; `.env.example` sets it to `game:paper f:commander`, so out of the box you search paper, Commander-legal cards only. The applied filter comes back in the `X-Applied-Filter` response header. Clear the variable to search the whole corpus (see [configuration.md](configuration.md)).

Sort order is a query parameter, not a search operator: `?q=t:dragon&order=edhrec`. Writing `order=edhrec` inside `q` returns `422 unsupported_operator`.

## Biblioplex extensions

Operators with no Scryfall equivalent, or that read data Scryfall doesn't have (your collection, the role classifier, Commander Spellbook combos):

| Operator | Matches |
|---|---|
| `pred:<label>` | Cards the rules classifier puts in a deckbuilding role: `ramp`, `card_advantage`, `targeted_disrupt`, `mass_disrupt`, `plan`. An unknown label returns `422`. |
| `has:combo` / `combo:has` | Cards in any ingested Commander Spellbook combo |
| `combo:"Card Name"` | Cards that appear in a combo with that card |
| `combo:<id>` | Cards in one specific combo (e.g. `combo:spellbook-123`) |
| `edhrec<N`, `edhrec:N` | Filter by EDHREC rank (Scryfall can only sort by it) |
| `in:inventory`, `is:owned`, `is:unowned`, `owned>=N`, `owned:0` | Your collection (imported from a Deckbox CSV or set per card) |
| `in:wishlist`, `is:wanted`, `is:unwanted`, `wanted>=N` | Your wishlist |
| bare words with typos | Name matching tolerates typos via trigram similarity (`invason` finds Invasion cards) |

`otag:` / `function:` use Scryfall Tagger slugs. Look slugs up with `GET /tags?prefix=...` rather than guessing: for example the board-wipe slugs are `boardwipe` and `sweeper-one-sided`, not `board-wipe`.

### Tested examples

Result counts were measured on 2026-10-03 with the default `game:paper f:commander` filter, a capped combo ingest (about 2,000 combos) and a small demo collection, so your numbers will differ.

| Query | Results | What it finds |
|---|---|---|
| `t:dragon pred:ramp` | 31 | Dragons that ramp |
| `pred:card_advantage c:u cmc<=2` | 158 | Cheap blue card advantage |
| `otag:removal c:r usd<1` | 1,826 | Red removal under $1 |
| `has:combo t:artifact` | 233 | Artifacts that are part of a known combo |
| `combo:"Isochron Scepter"` | 5 | Cards that combo with Isochron Scepter |
| `is:commander id<=esper edhrec<500` | 9 | Popular Esper-or-narrower commanders |
| `is:unowned is:wanted` | 7 | Your shopping list: wanted, not owned |
| `otag:sweeper-one-sided id<=wb` | 92 | One-sided board wipes in Orzhov colours |
| `function:boardwipe c:w cmc<=4` | 81 | Cheap white board wipes |
| `o:"draw a card" -t:land pred:ramp` | 181 | Non-land ramp that also draws |
| `pred:mass_disrupt usd<2 edhrec<3000` | 71 | Cheap, played mass interaction |
| `owned:0 t:legendary t:dragon` | 135 | Legendary dragons you don't own |
| `invason` | 68 | Typo-tolerant name match |

## DSL reference

Full operator list and out-of-DSL rejection list in the [tech spec](design/tech_spec.md) (contributor-level detail). Worked examples:

```text
t:dragon c:r                     # red dragons (implicit AND)
o:flying pow>=5                  # flyers with power ≥ 5
is:commander id<=esper           # legal Esper-or-narrower commanders
commander:WUB                    # color-identity subset of Esper (Scryfall sugar for id<=WUB)
kw:trample produces:g            # green producers with trample
o:"draw a card" -t:land          # card-draw non-lands
/^Soul/ t:creature               # creatures whose name starts with "Soul"
game:paper f:commander           # paper-printed commander-legal cards
invason                          # typo-tolerant: matches "Invasion of Ikoria/Tarkir/…" via pg_trgm word_similarity
combo:has                        # cards that appear in ANY ingested Commander Spellbook combo
combo:spellbook-123              # cards in the combo with source-id `spellbook-123`
combo:"Isochron Scepter"         # cards that combo alongside Isochron Scepter (quoted = card name)
has:combo                        # same as `combo:has`, agent-facing `has:` prefix parity with Scryfall
is:reserved                      # on Wizards' Reserved List
edhrec<10000                     # top 10k EDHrec-ranked cards
edhrec:1                         # the current #1 EDHrec card
usd<5 f:commander                # Commander-legal cards under $5
usd<0.5 t:creature               # bulk creatures for casual builds
in:inventory                     # cards you own at least one copy of (any printing)
is:owned                         # synonym of in:inventory
is:unowned                       # cards absent from your inventory
owned>=4 t:creature              # creature playsets you already own
owned:0 f:commander              # Commander-legal cards you are missing
in:wishlist                      # cards on your Deckbox wishlist
is:wanted                        # synonym of in:wishlist
is:unwanted                      # cards not on your wishlist
wanted>=2 t:land                 # lands you want more than one of
is:unowned is:wanted             # your shopping list (after importing a wishlist)
```

Bare words and quoted phrases match `name_search` via `ILIKE %X%` OR `name_search %> X` (word_similarity ≥ 0.4) — typos and partial words still surface results. Pair with `order=relevance` to rank closest-name matches first.
