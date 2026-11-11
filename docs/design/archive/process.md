# Process

How deliverables in [`todo.md`](./todo.md) get turned into code that satisfies [`tech_spec.md`](../tech_spec.md). One cycle per deliverable (`D#.#`). One commit per `D#.#`. Rolling on `master`.

This file is the framework itself. Update it when the framework changes.

---

## Cycle — per Deliverable

Each `D#.#` runs the same 12-step loop. Skip steps only where the rule allows.

### 1. Anchor

- Locate the relevant section(s) of `tech_spec.md` (e.g. §3.1, §17, §6).
- Read them plus immediate neighbors.
- (Deferred: `review_notes.md` clarifications. Not consulted in this iteration.)

### 2. Restate

- Write the required behavior in your own words (in the commit body, PR description, or a scratch note — not a new file).
- Surface every assumption. If any spec phrase is genuinely ambiguous, see step 9 (Drift) — do not silently guess.

### 3. Plan

- List the files touched.
- State the public API delta (new exports, signature changes).
- Enumerate the test list (unit + property tests). Mark which the spec mandates.

### 4. Red

- Write failing tests first.
- Property tests are red-first where the spec mandates them (`D1.1`, `D1.2`, `D2.4`, all of `M5`, `D11.3`).
- **Skip step 4 only when the deliverable has no behavior to test** (e.g. `D0.1` scaffolding, pure type declarations).

### 5. Green

- Minimum code to pass the tests written in step 4.
- No extra features, no speculative abstraction.

### 6. Refactor

- Apply the Core Three lens: Performance, Readability, Maintainability.
- Tests must stay green throughout.

### 7. Gate

- `ruff check` + `mypy` + `pytest` all green.
- Runs per `D#.#`. No deferring to the end of a milestone.

### 8. Smoke

- Required when the deliverable adds or changes a **public engine API** (e.g. `engine.tick`, `engine.click`, `engine.snapshot`).
- Skip when the change is purely internal.

### 9. Drift

- If the spec is ambiguous, contradictory, or silent on something this deliverable needed, write a best-guess implementation **and** flag the gap in `todo.md` under `## Spec Drift` → subsection for the relevant `§`.
- Drift entries record: section, question, what was assumed, why.
- Drift is queued, not blocking. Resolve later in a dedicated pass.

### 10. Docs

- Seed `docs/` incrementally per §19 of the spec — do not defer all docs to M15.
- Docstring on every public symbol introduced or changed.
- Update `readme.md` **only** when the deliverable adds public surface or a new top-level directory. Otherwise leave it.
- At the end of each milestone, do a readme review pass regardless.

### 10.1 MCP-facing route contract

`/mcp` is mounted by `fastapi-mcp` and auto-derives one MCP tool per OpenAPI operation (D17.1). New HTTP endpoints MUST be authored so an agent picking a tool from `tools/list` can call it without reading the source:

- Function name **is** the MCP tool name. `generate_unique_id_function` on the app (`main._operation_id_from_route_name`) hard-wires `operationId := route.name`. Name the route function verb-noun (`search_cards`, `analyze_decklist`) — not framework noise (`endpoint_handler`, `get`). Function names must be unique across routers.
- Decorator MUST set `summary=` (one-line tool card) and `description=` (multi-paragraph context: what/why/limits/error codes/response shape).
- Every `Query`/`Path`/`Body` parameter MUST carry `description=` and, where a non-trivial value is expected, `examples=[...]`. Missing param descriptions = agents flying blind.
- Documented error responses go under `responses={422: _ERROR_RESPONSE, ...}` so 4xx bodies appear in the schema instead of bare `{}`.
- Any endpoint that must NOT be agent-callable (heavy background jobs, destructive ops) is added to `main.MCP_EXCLUDED_OPERATIONS`. Default: exclude nothing; keep the surface narrow only when the operation would harm the deployment or the upstream (see `trigger_refresh`).
- The `tests/test_mcp_mount.py::test_mcp_tools_expose_read_endpoints` pin lists every expected tool by name — new endpoints must be appended there in the same deliverable that ships them.

### 11. Track

Update `todo.md` in the same commit as the code. `todo.md` is organized into three top-level sections:

- `## Pending` — not yet started. Deliverables grouped under `### M# · …`; drift entries grouped under `### Spec Drift / §X.Y`.
- `## In Progress` — mid-cycle checkpoints (`[~]`).
- `## Done` — shipped. Same sub-grouping as Pending (`### M# · …` for deliverables, `### Spec Drift / §X.Y` for resolved drift).

Tracking is **move-based**, not mark-in-place. On each commit:

- **Move on state change.** When a deliverable starts → cut from `## Pending` and paste under `## In Progress` with `[~]`. When it ships → cut from `## In Progress` (or `## Pending` if no checkpoint was needed) and paste under `## Done` with `[x]`, into the matching `### M#` bucket. Same flow for drift entries — resolved drift moves to `## Done` → `### Spec Drift / §X.Y`.
- **Do not leave `[x]` items in Pending or In Progress.** The section a row lives in must match its checkbox state.
- **Append new deliverables** uncovered during the cycle to `## Pending` under the right `### M#`. New scope goes in the same milestone as a new `D#.#` (next free number), or a later milestone if it's downstream. Do not silently expand an existing deliverable.
- **Append drift entries** to `## Pending` under `### Spec Drift / §X.Y` if step 9 fired.

`todo.md` lags reality by at most one commit. If a cycle is split across multiple commits (rare — only when refactor or follow-up justifies it), the final commit performs the move.

### 12. Commit

- Conventional commit. Scope is the milestone. Subject names the deliverable.
- Format: `<type>(M#): D#.# <subject>`
- Example: `feat(M5): D5.1 TickContext + dispatch + effect-chain bound`
- Types: `feat` for production code, `test` if commit is tests-only, `docs` for docs-only, `chore` for scaffolding, `refactor` for behavior-preserving changes.

---

## Rules

- **TDD red-first.** Tests before code, except when there is no behavior to test.
- **One commit per `D#.#`.** Granular history for bisect. No squash.
- **Rolling on `master`.** No long-lived feature branches.
- **Milestone `M#`** is a logical ship point, not a branch.
- **Blocked deliverable** — never silently guess. Either drift-flag (step 9) and proceed with best guess, or stop and ask the user when ambiguity would invalidate the implementation choice.
- **Spec is source of truth.** Implementation must conform to spec, not the other way around. When the spec is wrong, write a drift entry — do not edit the spec inline mid-cycle.

---

## Spec Drift

Drift entries live in `todo.md` under `## Pending` → `### Spec Drift / §X.Y`, organized by spec section (`### Spec Drift / §3.1`, `### Spec Drift / §6`, …). When resolved (`[x]`), move the entry to `## Done` → `### Spec Drift / §X.Y` per step 11. Format per entry:

```
- [ ] §X.Y — <one-line question>
  - Assumed: <what the implementation did>
  - Reason: <why this guess>
  - Resolve: <what would settle it — test case, author decision, …>
```

Drift is reviewed at the end of each milestone, or when a downstream deliverable depends on a drifted assumption.

---

## Quick reference

| Step        | Skip condition                                                       |
| ----------- | -------------------------------------------------------------------- |
| 1 Anchor    | never                                                                |
| 2 Restate   | never                                                                |
| 3 Plan      | never                                                                |
| 4 Red       | no behavior to test                                                  |
| 5 Green     | tests-only deliverable                                               |
| 6 Refactor  | nothing to refactor                                                  |
| 7 Gate      | never                                                                |
| 8 Smoke     | no public API change                                                 |
| 9 Drift     | no ambiguity hit                                                     |
| 10 Docs     | readme only on surface change; docstrings + `docs/` always when relevant |
| 11 Track    | never — row moved to its new section in the same commit                |
| 12 Commit   | never                                                                |

