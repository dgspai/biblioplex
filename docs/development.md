# Development

## Local dev

```bash
make install     # pip install -e ".[dev]" into $(VENV)
make check       # the gate: lint + typecheck + test + web/JS/link checks
```

Individual gates: `make lint`, `make typecheck`, `make test`. Pass extra args with `ARGS=`, e.g. `make test ARGS="-k named"` or `make lint ARGS=--fix`. Override the venv with `make <target> VENV=/path` (defaults to `.venv` when present).

OpenAPI is served at `/openapi.json` and the Swagger UI at `/docs`.

## Smoke test

```bash
make up        # docker compose up --build
make smoke     # bash scripts/smoke.sh
```

`scripts/smoke.sh` exercises the live API end-to-end against a running compose stack. It **triggers a full refresh**, so point it at a disposable stack, not one holding data you care about. It waits for `/health`, triggers the refresh, polls until ingest finishes, then hits `/cards/search`, `/cards/named?fuzzy=`, `/cards/:id`, `/cards/:id/similar`, and a projected search. Exits non-zero with a diagnostic on the first failure. Env-var overrides bypass `make smoke` (call the script directly):

```bash
SMOKE_BASE_URL=http://api:8000 scripts/smoke.sh     # custom host
SMOKE_TIMEOUT_SECONDS=3600 scripts/smoke.sh         # longer ingest budget
```

The default refresh timeout is 1800 s (30 min). Cards, tags and embeddings finish in about 3 minutes on a 12-core desktop CPU; the refresh only reports done once the combo stage (one bulk-file download) has finished too.
