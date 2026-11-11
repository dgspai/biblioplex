#!/bin/sh
# Build app.js from web/js/*.js (concatenated in filename order, so the
# browser still loads ONE classic script) with the DEFAULT_Q env var.
# envsubst's whitelist arg restricts substitution to ${DEFAULT_Q} so JS
# template literals (`${...}`) in the rest of the file pass through intact.
set -eu
: "${DEFAULT_Q:=}"
cat /usr/share/nginx/html/js/*.js | envsubst '${DEFAULT_Q}' \
  > /usr/share/nginx/html/app.js
