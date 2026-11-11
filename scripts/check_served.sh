#!/usr/bin/env bash
#
# Served-vs-shipped web asset check (D19.12).
#
# `make check-web` renders web/js/*.js to a TEMP FILE and runs
# `node --check` on it. That proves the template is valid JavaScript. It
# does NOT prove the running stack serves it — and it cannot, because it
# never makes an HTTP request.
#
# The gap is real and has bitten: web/Dockerfile COPYs index.html,
# style.css, helpers.js and web/js/ into the IMAGE at build time,
# and 10-render-app-js.sh renders app.js at container START. So every
# web/* edit is invisible to the browser until `make web-reload`. Four
# deliverables shipped green while the container served pre-edit assets.
#
# This script fetches what nginx actually returns and diffs it against
# what the working tree would produce.
#
# app.js needs care: the container renders it through envsubst with its
# own DEFAULT_Q. Rendering locally with a different value would report a
# false mismatch, so DEFAULT_Q is read OUT OF the running container
# rather than guessed.
#
# Usage:
#   scripts/check_served.sh              # hard-fail if the stack is absent
#   scripts/check_served.sh --skip-if-absent   # skip loudly instead
#
# Env:
#   CHECK_SERVED_URL   default http://localhost:8080
#   DEFAULT_Q          fallback when the container cannot be queried
#
# Requires: bash, curl, envsubst, diff.

set -euo pipefail

BASE_URL="${CHECK_SERVED_URL:-http://localhost:8080}"
SKIP_IF_ABSENT=0
[ "${1:-}" = "--skip-if-absent" ] && SKIP_IF_ABSENT=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$REPO_ROOT/web"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# --- is the stack up? -------------------------------------------------
if ! curl -fsS -o /dev/null --max-time 5 "$BASE_URL/index.html" 2>/dev/null; then
  if [ "$SKIP_IF_ABSENT" -eq 1 ]; then
    echo "check-served SKIPPED — nothing serving at $BASE_URL"
    echo "                       (start the stack with \`make up\` to enable this check)"
    exit 0
  fi
  echo "check-served FAILED — nothing serving at $BASE_URL" >&2
  echo "  Start the stack first: make up" >&2
  exit 1
fi

# --- DEFAULT_Q, read from the container that did the rendering --------
CONTAINER_Q=""
Q_SOURCE="fallback"
if command -v docker >/dev/null 2>&1; then
  if CONTAINER_Q="$(cd "$REPO_ROOT" && docker compose exec -T web printenv DEFAULT_Q 2>/dev/null)"; then
    CONTAINER_Q="${CONTAINER_Q%$'\r'}"
    CONTAINER_Q="${CONTAINER_Q%$'\n'}"
    Q_SOURCE="container"
  else
    CONTAINER_Q=""
  fi
fi
if [ "$Q_SOURCE" = "fallback" ]; then
  CONTAINER_Q="${DEFAULT_Q:-game:paper f:commander}"
fi

# --- compare each asset ------------------------------------------------
STALE=()

compare_asset() {
  local url_path="$1" local_file="$2" label="$3"
  local served="$WORK_DIR/served-$label"

  if ! curl -fsS -o "$served" --max-time 10 "$BASE_URL/$url_path"; then
    echo "  MISSING  $url_path — not served (HTTP error)"
    STALE+=("$url_path")
    return
  fi

  if diff -q "$served" "$local_file" >/dev/null 2>&1; then
    printf '  OK       %-16s %s bytes\n' "$url_path" "$(wc -c < "$served" | tr -d ' ')"
  else
    local served_bytes tree_bytes first_diff
    served_bytes="$(wc -c < "$served" | tr -d ' ')"
    tree_bytes="$(wc -c < "$local_file" | tr -d ' ')"
    # `|| true`: diff exits 1 when files differ, and `set -e` + pipefail
    # would kill the script before it printed the diagnostic — which is the
    # one moment this script exists for.
    first_diff="$(diff "$served" "$local_file" | head -4 | sed 's/^/             /' || true)"
    printf '  STALE    %-16s served %s bytes, tree %s bytes\n' \
      "$url_path" "$served_bytes" "$tree_bytes"
    echo "$first_diff"
    STALE+=("$url_path")
  fi
}

echo "check-served: $BASE_URL  (DEFAULT_Q from $Q_SOURCE)"

# app.js is generated — concatenate web/js/*.js and render the way the container did.
DEFAULT_Q="$CONTAINER_Q" envsubst '${DEFAULT_Q}' \
  < <(cat "$WEB_DIR"/js/*.js) > "$WORK_DIR/app.js.expected"
compare_asset "app.js" "$WORK_DIR/app.js.expected" "app.js"

# The rest are copied verbatim into the image.
compare_asset "index.html" "$WEB_DIR/index.html" "index.html"
compare_asset "style.css"  "$WEB_DIR/style.css"  "style.css"
compare_asset "helpers.js" "$WEB_DIR/helpers.js" "helpers.js"

if [ "${#STALE[@]}" -gt 0 ]; then
  echo
  echo "check-served FAILED — the stack is serving ${#STALE[@]} stale asset(s):" >&2
  for asset in "${STALE[@]}"; do echo "    $asset" >&2; done
  echo >&2
  echo "  The browser is NOT running your working tree. Rebuild and recreate:" >&2
  echo "    make web-reload" >&2
  exit 1
fi

echo "check-served OK — all 4 assets match the working tree"
