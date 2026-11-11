## What and why

<!-- One logical change per PR. Link the issue if there is one. -->

## Checklist

- [ ] `make check` is green
- [ ] Tests added or updated (written first when there is behaviour to test)
- [ ] Docs updated if this adds public surface (endpoint, DSL operator, top-level directory)
- [ ] Schema change? The commit body has an `ALTER TABLE ... IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` migration recipe
- [ ] New `Settings` field? Also added to `docker-compose.yml` under `services.api.environment:`
- [ ] Edited `web/`? Ran `make web-reload` and checked it in the browser
