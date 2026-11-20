## What and why

<!-- One logical change per PR. Link the issue if there is one. -->

<!-- Existing installs need to act (schema change, manual step)? Add a paragraph that
starts with "NOTICE: " and gives the step inline, e.g.
NOTICE: Before upgrading, run `CREATE INDEX IF NOT EXISTS x ON cards (y);`
The release job copies it into CHANGELOG.md. No code fences inside it. -->

## Checklist

- [ ] `make check` is green
- [ ] Tests added or updated (written first when there is behaviour to test)
- [ ] Docs updated if this adds public surface (endpoint, DSL operator, top-level directory)
- [ ] Schema change? This description has a `NOTICE:` paragraph with the `ALTER TABLE ... IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` migration (CI checks it)
- [ ] New `Settings` field? Also added to `docker-compose.yml` under `services.api.environment:`
- [ ] Edited `web/`? Ran `make web-reload` and checked it in the browser
