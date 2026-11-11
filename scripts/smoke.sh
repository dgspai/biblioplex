#!/usr/bin/env bash
#
# End-to-end smoke test for Biblioplex (D8.3, tech_spec.md §11 M8).
#
# Boots, refreshes, and exercises every read endpoint against a running
# compose stack. Not a unit test — talks to the real DB. Run AFTER
# `docker compose up -d --build` from the repo root.
#
# Steps (per the M8 prompt):
#   1. Wait for /health to respond 200 (≤60s).
#   2. POST /admin/refresh, capture job_id, assert status=started.
#   3. Poll /health.refresh.running until false (≤SMOKE_TIMEOUT_SECONDS).
#   4. Assert cards_count > 0.
#   5. GET /cards/search?q=t:dragon → total_cards > 0, data non-empty.
#   6. GET /cards/named?fuzzy=lightning bolt → 200, name contains
#      "Lightning Bolt".
#   7. GET /cards/:id using the first id from search → 200.
#   8. GET /cards/:id/similar?limit=5 → 200, list of 5.
#   9. GET /cards/search?q=t:dragon&projection=name,cmc → each entry has
#      only those keys.
#  10. GET /cards/search?q=invason&order=relevance → total_cards > 0
#      (D11.1 fuzzy name match + relevance ordering).
#  11. POST /decks/analyze with a small ramp-heavy decklist → counts.ramp >= 2,
#      no unresolved names (§6.7, D12.4).
#  12. GET /cards/:id/substitutes?slot=ramp&limit=5 against Sol Ring → ≥1
#      result, every result carries 'ramp' in predicted_labels (§6.8, D12.5).
#  13. POST /decks/suggest with the same tiny decklist → deck_suggestions
#      object, at least one underfilled slot returns at least one candidate
#      (§6.9, D13.3).
#  14. POST /mcp tools/list — MCP transport responds with a tool list
#  15. Deck branch seam (D19.34/D19.37/D19.38): an import moves head AND
#      rebuilds the derived deck_cards index; a PINNED deck's import does
#      neither. Every layer is unit-pinned, but nothing else catches a
#      regression BETWEEN them — a version appended while head silently
#      fails to move leaves the card table describing content the deck is
#      not on, and a fake-conn test passes straight through that.
#      containing `search_cards` and NOT `trigger_refresh` (D17.1).
#  15. Exit 0 on success, non-zero with diagnostic on first failure.
#
# Env:
#   SMOKE_BASE_URL          default http://localhost:8000
#   SMOKE_TIMEOUT_SECONDS   refresh-completion budget; default 1800 (30m)
#   SMOKE_HEALTH_WAIT       initial /health wait budget; default 60
#   SMOKE_POLL_INTERVAL     seconds between refresh polls; default 10
#
# Requires: bash, curl, jq.

set -euo pipefail

BASE_URL="${SMOKE_BASE_URL:-http://localhost:8000}"
REFRESH_TIMEOUT="${SMOKE_TIMEOUT_SECONDS:-1800}"
HEALTH_WAIT="${SMOKE_HEALTH_WAIT:-60}"
POLL_INTERVAL="${SMOKE_POLL_INTERVAL:-10}"

step() {
    printf '\n[smoke] %s\n' "$*" >&2
}

fail() {
    printf '\n[smoke] FAIL: %s\n' "$*" >&2
    exit 1
}

require_bin() {
    command -v "$1" >/dev/null 2>&1 || fail "missing required binary: $1"
}

require_bin curl
require_bin jq

step "1/15 waiting for /health (≤${HEALTH_WAIT}s)"
deadline=$(( $(date +%s) + HEALTH_WAIT ))
while :; do
    if curl -fsS "${BASE_URL}/health" >/dev/null 2>&1; then
        break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        fail "/health did not return 200 within ${HEALTH_WAIT}s"
    fi
    sleep 2
done

step "2/15 POST /admin/refresh"
refresh_body=$(curl -fsS -X POST "${BASE_URL}/admin/refresh")
status=$(printf '%s' "$refresh_body" | jq -r '.status')
job_id=$(printf '%s' "$refresh_body" | jq -r '.job_id')
case "$status" in
    started)
        printf '[smoke] refresh started, job_id=%s\n' "$job_id" >&2
        ;;
    already_running)
        printf '[smoke] refresh already running, job_id=%s — will poll until it finishes\n' "$job_id" >&2
        ;;
    *)
        fail "unexpected refresh status: ${status} (body=${refresh_body})"
        ;;
esac

step "3/15 polling /health.refresh.running until false (≤${REFRESH_TIMEOUT}s)"
deadline=$(( $(date +%s) + REFRESH_TIMEOUT ))
while :; do
    health=$(curl -fsS "${BASE_URL}/health")
    running=$(printf '%s' "$health" | jq -r '.refresh.running')
    if [ "$running" = "false" ]; then
        break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        fail "refresh did not finish within ${REFRESH_TIMEOUT}s; last /health=${health}"
    fi
    sleep "$POLL_INTERVAL"
done

step "4/15 asserting cards_count > 0"
cards_count=$(printf '%s' "$health" | jq -r '.cards_count')
if ! [[ "$cards_count" =~ ^[0-9]+$ ]] || [ "$cards_count" -le 0 ]; then
    fail "cards_count not > 0 after refresh (got ${cards_count}); body=${health}"
fi
printf '[smoke] cards_count=%s\n' "$cards_count" >&2

step "5/15 GET /cards/search?q=t:dragon"
search_url="${BASE_URL}/cards/search?q=$(printf 't:dragon' | jq -sRr @uri)"
search_body=$(curl -fsS "$search_url")
total_cards=$(printf '%s' "$search_body" | jq -r '.total_cards')
data_len=$(printf '%s' "$search_body" | jq -r '.data | length')
if ! [[ "$total_cards" =~ ^[0-9]+$ ]] || [ "$total_cards" -le 0 ]; then
    fail "search t:dragon returned total_cards=${total_cards}"
fi
if [ "$data_len" -le 0 ]; then
    fail "search t:dragon returned empty data array"
fi
printf '[smoke] search t:dragon → total_cards=%s, page=%s\n' "$total_cards" "$data_len" >&2

step "6/15 GET /cards/named?fuzzy=lightning bolt"
named_url="${BASE_URL}/cards/named?fuzzy=$(printf 'lightning bolt' | jq -sRr @uri)"
named_body=$(curl -fsS "$named_url")
named_name=$(printf '%s' "$named_body" | jq -r '.name')
if [[ "$named_name" != *"Lightning Bolt"* ]]; then
    fail "fuzzy named=lightning bolt → name '${named_name}' does not contain 'Lightning Bolt'"
fi
printf '[smoke] fuzzy → %s\n' "$named_name" >&2

step "7/15 GET /cards/:id (first id from t:dragon search)"
first_id=$(printf '%s' "$search_body" | jq -r '.data[0].id')
if [ -z "$first_id" ] || [ "$first_id" = "null" ]; then
    fail "could not extract first card id from search response"
fi
card_body=$(curl -fsS "${BASE_URL}/cards/${first_id}")
card_id=$(printf '%s' "$card_body" | jq -r '.id')
if [ "$card_id" != "$first_id" ]; then
    fail "GET /cards/${first_id} returned id=${card_id}"
fi
printf '[smoke] /cards/%s → ok\n' "$first_id" >&2

step "8/15 GET /cards/${first_id}/similar?limit=5"
similar_body=$(curl -fsS "${BASE_URL}/cards/${first_id}/similar?limit=5")
similar_obj=$(printf '%s' "$similar_body" | jq -r '.object')
similar_len=$(printf '%s' "$similar_body" | jq -r '.data | length')
if [ "$similar_obj" != "list" ]; then
    fail "/similar object=${similar_obj}, expected 'list'"
fi
if [ "$similar_len" -ne 5 ]; then
    fail "/similar?limit=5 returned ${similar_len} rows, expected 5"
fi
printf '[smoke] /similar → %s rows\n' "$similar_len" >&2

step "9/15 GET /cards/search?q=t:dragon&projection=name,cmc"
proj_url="${BASE_URL}/cards/search?q=$(printf 't:dragon' | jq -sRr @uri)&projection=name,cmc"
proj_body=$(curl -fsS "$proj_url")
# Every entry's key set must equal {"name","cmc"} exactly.
bad_entry=$(printf '%s' "$proj_body" | jq -r '
    .data
    | map(keys | sort | tojson)
    | unique
    | map(select(. != "[\"cmc\",\"name\"]"))
    | .[0] // empty
')
if [ -n "$bad_entry" ]; then
    fail "projection returned unexpected key set: ${bad_entry}"
fi
proj_len=$(printf '%s' "$proj_body" | jq -r '.data | length')
printf '[smoke] projection → %s entries, each with exactly {name,cmc}\n' "$proj_len" >&2

step "10/15 GET /cards/search?q=invason&order=relevance (D11.1 fuzzy + rank)"
fuzzy_url="${BASE_URL}/cards/search?q=$(printf 'invason' | jq -sRr @uri)&order=relevance"
fuzzy_body=$(curl -fsS "$fuzzy_url")
fuzzy_total=$(printf '%s' "$fuzzy_body" | jq -r '.total_cards')
if ! [[ "$fuzzy_total" =~ ^[0-9]+$ ]] || [ "$fuzzy_total" -le 0 ]; then
    fail "fuzzy q=invason order=relevance returned total_cards=${fuzzy_total} — expected Invasion-named cards"
fi
fuzzy_top=$(printf '%s' "$fuzzy_body" | jq -r '.data[0].name')
printf '[smoke] fuzzy q=invason order=relevance → total_cards=%s, top=%s\n' "$fuzzy_total" "$fuzzy_top" >&2

step "11/15 POST /decks/analyze (D12.4)"
deck_body=$(curl -fsS -X POST -H 'Content-Type: application/json' \
    --data '{"decklist": "1 Sol Ring\n1 Arcane Signet\n1 Cultivate\n1 Counterspell\n1 Lightning Bolt\n8 Forest"}' \
    "${BASE_URL}/decks/analyze")
deck_obj=$(printf '%s' "$deck_body" | jq -r '.object')
if [ "$deck_obj" != "deck_analysis" ]; then
    fail "/decks/analyze object=${deck_obj}, expected 'deck_analysis' (body=${deck_body})"
fi
ramp_count=$(printf '%s' "$deck_body" | jq -r '.counts.ramp')
if ! [[ "$ramp_count" =~ ^[0-9]+$ ]] || [ "$ramp_count" -lt 2 ]; then
    fail "/decks/analyze counts.ramp=${ramp_count}, expected ≥2 (Sol Ring + Arcane Signet + Cultivate)"
fi
unresolved_len=$(printf '%s' "$deck_body" | jq -r '.unresolved | length')
if [ "$unresolved_len" -ne 0 ]; then
    unresolved=$(printf '%s' "$deck_body" | jq -r '.unresolved | join(",")')
    fail "/decks/analyze unresolved=[${unresolved}], expected none"
fi
land_count=$(printf '%s' "$deck_body" | jq -r '.counts.land')
printf '[smoke] /decks/analyze → ramp=%s, land=%s, unresolved=0\n' "$ramp_count" "$land_count" >&2

step "12/15 GET /cards/:id/substitutes?slot=ramp (D12.5)"
sol_id=$(printf '%s' "$deck_body" | jq -r '.cards[] | select(.name == "Sol Ring") | .id')
if [ -z "$sol_id" ] || [ "$sol_id" = "null" ]; then
    fail "could not extract Sol Ring id from /decks/analyze response"
fi
subs_body=$(curl -fsS "${BASE_URL}/cards/${sol_id}/substitutes?slot=ramp&limit=5")
subs_obj=$(printf '%s' "$subs_body" | jq -r '.object')
subs_len=$(printf '%s' "$subs_body" | jq -r '.data | length')
if [ "$subs_obj" != "list" ]; then
    fail "/substitutes object=${subs_obj}, expected 'list'"
fi
if [ "$subs_len" -lt 1 ]; then
    fail "/substitutes?slot=ramp returned 0 results, expected ≥1"
fi
# Every result must carry 'ramp' in predicted_labels.
non_ramp=$(printf '%s' "$subs_body" \
    | jq -r '.data[] | select((.predicted_labels // []) | index("ramp") | not) | .name' \
    | head -n1)
if [ -n "$non_ramp" ]; then
    fail "/substitutes?slot=ramp returned non-ramp card: ${non_ramp}"
fi
printf '[smoke] /substitutes ramp → %s rows, all with ramp\n' "$subs_len" >&2

step "13/15 POST /decks/suggest (D13.3)"
suggest_body=$(curl -fsS -X POST -H 'Content-Type: application/json' \
    --data '{"decklist": "1 Sol Ring\n1 Arcane Signet\n1 Cultivate\n1 Counterspell\n1 Lightning Bolt\n8 Forest"}' \
    "${BASE_URL}/decks/suggest")
suggest_obj=$(printf '%s' "$suggest_body" | jq -r '.object')
if [ "$suggest_obj" != "deck_suggestions" ]; then
    fail "/decks/suggest object=${suggest_obj}, expected 'deck_suggestions' (body=${suggest_body})"
fi
suggest_slot_count=$(printf '%s' "$suggest_body" | jq -r '.suggestions | length')
if ! [[ "$suggest_slot_count" =~ ^[0-9]+$ ]] || [ "$suggest_slot_count" -lt 1 ]; then
    fail "/decks/suggest returned no suggestion slots (any underfilled slot should surface at least one candidate)"
fi
# At least ONE slot list must be non-empty. Aggregate lengths across all
# slots and assert > 0.
total_suggestions=$(printf '%s' "$suggest_body" \
    | jq -r '[.suggestions[] | length] | add // 0')
if [ "$total_suggestions" -lt 1 ]; then
    fail "/decks/suggest returned 0 candidates across all slots"
fi
printf '[smoke] /decks/suggest → %s slots, %s total candidates\n' "$suggest_slot_count" "$total_suggestions" >&2

step "14/15 deck branch seam — import moves head + rebuilds deck_cards; pinned does not"
# D19.34's trap, end to end. The derived index is rebuilt in the same
# transaction as append_version, but ONLY when head actually moves — so
# this exercises the seam between three shipped layers rather than any one
# of them. total_cards from GET /decks/:id/cards is read from the index, so
# it is the honest witness: if the rebuild is skipped or wrongly fired, the
# number disagrees with head. Deliberately not asserting coverage_pct —
# that depends on what the operator happens to own.
seam_deck=$(curl -fsS -X POST -H 'Content-Type: application/json' \
    --data '{"name":"smoke branch seam","decklist":"1 Sol Ring\n1 Arcane Signet\n","format":"commander"}' \
    "${BASE_URL}/decks")
seam_id=$(printf '%s' "$seam_deck" | jq -r '.id')
[ -z "$seam_id" ] || [ "$seam_id" = "null" ] && fail "could not create smoke deck; body=${seam_deck}"

seam_cards() { curl -fsS "${BASE_URL}/decks/${seam_id}/cards" | jq -r '.summary.total_cards'; }
seam_head()  { curl -fsS "${BASE_URL}/decks/${seam_id}" | jq -r '.version'; }

v1_head=$(seam_head); v1_cards=$(seam_cards)
[ "$v1_cards" = "2" ] || fail "seam: fresh deck should index 2 cards, got ${v1_cards}"

# Unpinned import: head must move AND the index must follow it.
curl -fsS -o /dev/null -X PATCH -H 'Content-Type: application/json' \
    --data '{"decklist":"1 Sol Ring\n1 Arcane Signet\n1 Cultivate\n","branch":"moxfield","message":"smoke seam"}' \
    "${BASE_URL}/decks/${seam_id}"
v2_head=$(seam_head); v2_cards=$(seam_cards)
[ "$v2_head" -gt "$v1_head" ] || fail "seam: head did not move on import (${v1_head} -> ${v2_head})"
[ "$v2_cards" = "3" ] || fail "seam: index did not follow head; expected 3 cards, got ${v2_cards}"

# Pin head, then import again: NEITHER may move. This is the half that
# regresses silently — the import still succeeds and still creates a
# version, so only the index and the pointer reveal the bug.
curl -fsS -o /dev/null -X POST -H 'Content-Type: application/json' \
    --data "{\"version\": ${v2_head}}" "${BASE_URL}/decks/${seam_id}/head"
curl -fsS -o /dev/null -X PATCH -H 'Content-Type: application/json' \
    --data '{"decklist":"1 Sol Ring\n","branch":"deckbox","message":"smoke seam pinned"}' \
    "${BASE_URL}/decks/${seam_id}"
v3_head=$(seam_head); v3_cards=$(seam_cards)
[ "$v3_head" = "$v2_head" ] || fail "seam: PINNED head moved (${v2_head} -> ${v3_head})"
[ "$v3_cards" = "3" ] || fail "seam: PINNED deck's index was rebuilt; expected 3 cards, got ${v3_cards}"

curl -fsS -o /dev/null -X DELETE "${BASE_URL}/decks/${seam_id}"
printf '[smoke] deck branch seam → head %s->%s with index 2->3, pinned import left both at %s/%s\n' \
    "$v1_head" "$v2_head" "$v3_head" "$v3_cards" >&2

step "15/15 POST /mcp tools/list (D17.1 — MCP transport handshake)"
# MCP streamable-http speaks JSON-RPC 2.0 over POST. `tools/list` is the
# first thing any client (Claude Code, Cursor, custom SDK) fires after
# `initialize`. We short-circuit here — hit tools/list directly and read
# the SSE-framed response. Requires the Accept header both content types
# per the spec.
mcp_body=$(curl -fsS -X POST -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
    "${BASE_URL}/mcp")
# Response can be framed as SSE (`data: {...}`) or bare JSON depending on
# transport negotiation — strip an optional `data: ` prefix before parse.
mcp_json=$(printf '%s' "$mcp_body" | sed -n 's/^data: //p' | head -n1)
[ -z "$mcp_json" ] && mcp_json="$mcp_body"
mcp_names=$(printf '%s' "$mcp_json" | jq -r '.result.tools[].name' 2>/dev/null || true)
if [ -z "$mcp_names" ]; then
    fail "/mcp tools/list returned no tool names; body=${mcp_body}"
fi
if ! printf '%s\n' "$mcp_names" | grep -qx 'search_cards'; then
    fail "/mcp tools/list missing 'search_cards' tool; got: $(printf '%s' "$mcp_names" | tr '\n' ',')"
fi
if printf '%s\n' "$mcp_names" | grep -qx 'trigger_refresh'; then
    fail "/mcp tools/list must NOT expose 'trigger_refresh' (agents cannot kick refreshes)"
fi
mcp_count=$(printf '%s\n' "$mcp_names" | wc -l | tr -d ' ')
printf '[smoke] /mcp → %s tools, search_cards present, trigger_refresh excluded\n' "$mcp_count" >&2

step "ALL SMOKE CHECKS PASSED"
