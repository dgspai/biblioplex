# Override per-call: `make test VENV=.venv` or export VENV in your shell.
VENV ?= $(firstword $(wildcard .venv) $(HOME)/.virtualenvs/biblioplex)
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip
BIN  := $(VENV)/bin

# Pass-through args: `make test ARGS="-k named"` / `make lint ARGS=--fix`.
ARGS ?=

# xdist parallelism for `make test`. Default 0 (serial) — measured 15s
# parallel vs 10s serial on the current ~700-test suite because most
# tests use in-process fakes with no real I/O, so xdist's per-worker
# fixed cost dominates. Flip to `auto` (or an integer N) once the
# suite grows past ~1500 tests and the amortization flips.
XDIST ?= 0

.PHONY: help install hooks audit lint typecheck test check check-web check-links check-served check-served-soft up down down-v refresh smoke reembed pt-names eval-similarity regen-projection web-reload

help:  ## Print available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "  VENV=$(VENV)  (override: make <target> VENV=/path)"
	@echo "  ARGS=$(ARGS)  (extra args, e.g. make test ARGS=\"-k named\")"

install:  ## pip install -e ".[dev]" into $(VENV)
	$(PIP) install -e ".[dev]"

hooks:  ## install git hooks (gitleaks, personal-data paths, noreply email on push)
	$(BIN)/pre-commit install --hook-type pre-commit --hook-type pre-push

audit:  ## pip-audit: known CVEs in installed dependencies
	$(BIN)/pip-audit --skip-editable $(ARGS)

lint:  ## ruff check src tests  (ARGS=--fix to autofix)
	$(BIN)/ruff check src tests $(ARGS)

typecheck:  ## mypy src
	$(BIN)/mypy src $(ARGS)

test:  ## pytest -n $(XDIST) (ARGS="-k foo" etc; XDIST=0 for serial)
	@if [ "$(XDIST)" = "0" ]; then \
	    $(BIN)/pytest $(ARGS); \
	else \
	    $(BIN)/pytest -n $(XDIST) $(ARGS); \
	fi

check: lint typecheck test check-web check-links check-served-soft  ## lint + typecheck + test + JS parse + md links + served-asset diff (the Gate)

check-web:  ## node --check on web/js/*.js rendered into app.js (Bug.29 gate)
	@command -v envsubst >/dev/null || { echo "envsubst missing (install gettext)"; exit 1; }
	@command -v node >/dev/null || { echo "node missing (install nodejs)"; exit 1; }
	@t=$$(mktemp --suffix=.js) && cat web/js/*.js | DEFAULT_Q="" envsubst '$${DEFAULT_Q}' > "$$t" \
	  && node --check "$$t" && rm -f "$$t" && echo "check-web OK"

check-links:  ## every relative link in tracked *.md resolves
	@$(PY) scripts/check_links.py

check-served:  ## diff what nginx SERVES against the working tree (D19.12)
	@bash scripts/check_served.sh

check-served-soft:  ## check-served, but skip loudly when no stack is up
	@bash scripts/check_served.sh --skip-if-absent

up:  ## docker compose up --build
	docker compose up --build

down:  ## docker compose down
	docker compose down

down-v:  ## docker compose down -v (drops pgdata volume — schema rebuild path)
	docker compose down -v

web-reload:  ## rebuild + force-recreate web — REQUIRED after ANY web/* edit
	docker compose build web && docker compose up -d --force-recreate web

refresh:  ## POST /admin/refresh
	curl -fsS -XPOST http://localhost:8000/admin/refresh

smoke:  ## bash scripts/smoke.sh
	bash scripts/smoke.sh

reembed:  ## docker exec into api, run scripts/reembed.py
	docker exec biblioplex-api-1 python scripts/reembed.py

pt-names:  ## rebuild pt_names from Scryfall all_cards (docker exec into api)
	docker exec biblioplex-api-1 python scripts/build_pt_names.py

eval-similarity:  ## python scripts/eval_similarity.py
	$(PY) scripts/eval_similarity.py

regen-projection:  ## python scripts/regen_projection_whitelist.py
	$(PY) scripts/regen_projection_whitelist.py
