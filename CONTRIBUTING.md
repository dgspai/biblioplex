# Contributing

Thanks for helping out. Bug reports and small focused PRs are the most useful.

## Dev setup

Requires Python 3.14+, Node (for the JS checks), `envsubst` (gettext) and Docker.

```bash
python -m venv .venv
make install                   # pip install -e ".[dev]"
cp .env.example .env           # set DB_PASSWORD
make up                        # Postgres + pgvector, api, web; card data ingests on first boot
```

The Makefile uses a repo-local `.venv` when it exists, else `~/.virtualenvs/biblioplex`.
Point it elsewhere with `make <target> VENV=/path`. `make help` lists every target.

More detail (smoke test, local dev targets): [docs/development.md](docs/development.md).

## The gate

```bash
make check
```

runs ruff, mypy, pytest, `check-web`, `check-links` and `check-served-soft`.
It must be green for every change. The tests need no database and no network: they use
in-process fakes and a mocked embedding model. `check-served` only does something when
a local stack is running.

## Web assets are baked, not mounted

Any edit under `web/` needs `make web-reload` before the browser sees it. The web image
COPYs `index.html`, `style.css`, `helpers.js` and `js/` (one file per screen, concatenated into `app.js`) at build time and
renders `app.js` when the container starts. Nothing is bind-mounted, so
`docker compose restart web` serves the same stale files. `make check-web` only proves
the template parses; `make check-served` diffs what nginx serves against your tree.

## Schema changes

`schema.sql` runs only on an empty `pgdata` volume. Any new column or index must also
ship an in-place migration recipe in the commit body, so existing deployments can apply
it without `make down-v`:

```sql
ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...;
CREATE INDEX IF NOT EXISTS ...;
```

## New settings

A new field on `Settings` (`src/biblioplex/config.py`) must also be added to the
`services.api.environment:` block in `docker-compose.yml` as `NAME: ${NAME:-default}`.
Otherwise the container never sees it and silently uses the default.

## Commits and tests

- One logical change per commit. Conventional commits: `<type>(scope): <subject>` with
  type `feat`, `fix`, `test`, `docs`, `chore` or `refactor`.
- Write the test first when there is behaviour to test.
- Make new commits rather than amending ones you have already pushed.
- Update the README only when you add public surface (an endpoint, a DSL operator, a
  top-level directory).

## Pull requests

Fill in the PR template. Design history lives in [docs/design/](docs/design/index.md).
Rules aimed at AI coding agents are in [CLAUDE.md](CLAUDE.md).

Everyone taking part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).

Security issues: see [SECURITY.md](SECURITY.md), do not file a public issue.

## Releasing

The version lives only in `pyproject.toml`; the app reads it from package metadata (`/health`, the Settings About line, the OpenAPI schema). Bump it there, run `make install` so an editable install picks it up, then tag `vX.Y.Z`.
