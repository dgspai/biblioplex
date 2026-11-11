## Delivery

One logical change per commit. Conventional commits:
`<type>(scope): <subject>` — `feat` / `fix` / `test` / `docs` / `chore` / `refactor`.
Always create NEW commits — never `--amend` a shipped one. When a pre-commit hook
fails, fix and commit again.


- **Red-first**: tests before code, except where there is no behaviour to test
  (scaffolding, pure types, Makefile or settings edits).
- **Restate**: before coding a UI or behaviour spec described in prose, write the
  expected behaviour and your assumptions into the commit body. Ask when ambiguity
  would invalidate the implementation choice — do not silently guess. An agent
  running unattended cannot ask, and should stop and say so rather than pick.
- **README**: update only when the change adds public surface — an endpoint, a DSL
  operator, a top-level directory. Otherwise leave it.

The former 12-step cycle, the `todo.md` work log and the Spec Drift register are
retired to [`docs/design/archive/`](./docs/design/archive/index.md). Source docstrings still cite
their `§` sections; those citations describe history and were left as written.

## Schema changes

`schema.sql` runs **only** on empty `pgdata` (mounted read-only at `/docker-entrypoint-initdb.d/schema.sql`). Existing deployments do not re-run it.

Any new column or index MUST ship with an in-place migration recipe in the commit body — `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` or `CREATE INDEX IF NOT EXISTS ...`. Operators pick these up without a destructive `make down-v`. Example recipe (the `combos` trigram indexes):

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS combos_prereqs_trgm ON combos USING gin (prereqs gin_trgm_ops);
CREATE INDEX IF NOT EXISTS combos_steps_trgm   ON combos USING gin (steps gin_trgm_ops);
```

## Env vars

New field on `Settings` (`src/biblioplex/config.py`) → **also add** to `docker-compose.yml` `services.api.environment:` block as `<NAME>: ${<NAME>:-<default>}`.

`.env` alone is not enough. Docker-compose interpolates `${...}` in the compose file itself but only forwards keys named under `environment:`. Unlisted vars never reach the api process — the container silently reads the Pydantic default and `/health` shows nothing wrong. Truth check on a live stack: `docker compose exec api env | grep <VAR>`.

Web-side vars follow the same rule under `services.web.environment:`.

## Web assets are baked, not mounted

**Any edit under `web/` requires `make web-reload` before the browser sees it.** Not just `.env` changes — `web/Dockerfile` COPYs `index.html`, `style.css`, `helpers.js` and `js/` into the IMAGE at build time, and `10-render-app-js.sh` concatenates `js/*.js` into `app.js` via `envsubst` at container START. Nothing is bind-mounted, so `docker compose restart web` re-serves the same stale bytes.

This is invisible to `make check-web`, which renders the template to a temp file and never makes an HTTP request — four M19 deliverables shipped green while the container served pre-edit assets (D19.12). `make check-served` closes it by diffing what nginx actually returns against the working tree; `make check` runs it automatically and skips loudly when no stack is up.

## Runbook

`make` is the source of truth. `make help` lists every target. Never invoke bare `pytest` / `docker compose` when a make target exists — it strips the venv path and skips the conventions the targets encode.

- `make check` — Gate (ruff + mypy + pytest + `check-web` + `check-served-soft`).
- `make check-web` — concatenates `web/js/*.js`, renders via envsubst + `node --check` (Bug.29 gate; D16.13). Proves the template PARSES, not that it is served.
- `make check-served` — diffs the four assets nginx actually serves against the working tree (D19.12). Hard-fails with no stack; `check-served-soft` skips loudly instead and is what `make check` runs.
- `make test ARGS="-k <expr>"` — filter subset.
- `make up` / `make down` / `make down-v` (drops `pgdata` — schema rebuild path).
- `make refresh` — `POST /admin/refresh` against a running stack.
- `make smoke` — end-to-end smoke against a running stack.
- `make web-reload` — rebuild + force-recreate web. REQUIRED after ANY `web/*` edit, not only `.env` (D16.10, D19.12).
