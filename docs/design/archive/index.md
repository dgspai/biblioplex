# Archive

Superseded working documents, kept for history and for the citations that still
point at them from source docstrings and older commits. **Nothing here is
current guidance.**

| File | What it was | Retired |
|---|---|---|
| [process.md](./process.md) | The 12-step per-deliverable delivery cycle: restate, red-first, gate, drift, track, commit. Its durable rules now live inline in [/CLAUDE.md](../../../CLAUDE.md); the ceremony around them is gone. | 2026-08-24 |
| [todo.md](./todo.md) | Work tracking (`## Pending` / `## In Progress` / `## Done`) and the `## Spec Drift` log, 136KB at retirement. Shipped rows were archived to [docs/changelog.md](../../changelog.md) as they landed, so the delivery history is there rather than here. | 2026-08-24 |
| [process-md-auditor.md](./process-md-auditor.md) | A subagent that audited commits against `process.md`. Archived with its subject. | 2026-08-24 |

## Why

`todo.md` was a single 136KB file that `process.md` required every deliverable
to edit. That is workable for one person working serially and hostile to
anything concurrent — several agents each rewriting the same file produce a
merge conflict per run, and the conflict is in the tracking document rather than
in the code.

Citations to `§` sections of these files survive in source docstrings and in
`README.md`, `tech_spec.md` and `docs/changelog.md`. They were left as written:
they are accurate about the history they describe, and rewriting them would edit
the record rather than the process.
