// -------- Decks tab (D19.6) --------
// Stored decks with their full version history. Everything here talks to
// the D19.4/D19.5 routes; the version-to-version diff reuses the existing
// POST /decks/diff rather than a bespoke endpoint.

let _decksCurrent = [];
let _deckSelectedId = null;
let _deckVersions = [];
let _deckDetailData = null;
// D19.35: null means "the version timeline shows every branch, interleaved
// by version DESC" — the row's explicit ask, not one list per branch.
let _deckTimelineFilter = null;
// D19.35: null means "the card table follows the deck's head pointer" (the
// pre-existing behaviour, unchanged). A branch name here means the table
// is VIEWING that branch's own latest version instead — never a promote.
let _deckViewBranch = null;

// Deckbox is deliberately absent from this map. Its export endpoint serves
// anonymous callers an HTML login page (Bug.32), so a "pull" against it
// could only ever fail — and offering a control that cannot succeed is the
// mistake D18.19 is still cleaning up. The Deckbox URL still renders as a
// plain link below; only the action is withheld.
const DECK_LINK_FIELDS = [
  { key: "deckbox_url", label: "Deckbox" },
  { key: "moxfield_url", label: "Moxfield" },
  { key: "archidekt_url", label: "Archidekt" },
];

function _deckStatus(el, msg, isError) {
  if (!el) return;
  el.innerHTML = msg
    ? `<span class="${isError ? "upload-warn" : "help"}">${escapeHtml(msg)}</span>`
    : "";
}

// One place four branches are spelled out, matching `deck_versions.branch`'s
// CHECK (D19.33) — reused by the import selector, the version-timeline
// filter chips, and the branch-head navigator, so a fifth branch added
// later only needs to change here.
const DECK_BRANCHES = ["main", "deckbox", "moxfield", "archidekt"];

// D19.26: one place that knows which metadata fields exist, shared by the
// multipart and JSON import paths. Two copies of that list is exactly what
// drifts when a field is added later. D19.35 adds `branch` and `message`
// to the SAME list rather than a second collector.
//
// Blank values are OMITTED rather than sent as "": the columns mean NULL
// when unset, and an empty string would sail past D19.4's hostname check
// as a stored non-URL. Hostname validation itself stays server-side — one
// validator, or the rule drifts between tiers. `branch` carries an explicit
// `reset` because it is a <select> with no blank option — resetting it to
// "" the way a text input resets would leave nothing selected.
const DECK_META_INPUTS = [
  { key: "deckbox_url", el: () => deckImportDeckboxEl },
  { key: "moxfield_url", el: () => deckImportMoxfieldEl },
  { key: "archidekt_url", el: () => deckImportArchidektEl },
  { key: "notes", el: () => deckImportNotesEl },
  { key: "branch", el: () => deckImportBranchEl, reset: "main" },
  { key: "message", el: () => deckImportMessageEl },
];

// D19.37: `inputs` defaults to `DECK_META_INPUTS` (the top import card's
// fields) so every EXISTING call site is unchanged, but the collector
// itself is generic over any list shaped `{key, el, reset?}` — the deck
// detail pane's "Import new version" control (branch + message only)
// reuses this same function against its own two-entry list rather than
// a third hand-rolled collector.
function _deckImportMeta(inputs) {
  const list = inputs || DECK_META_INPUTS;
  const out = {};
  for (const f of list) {
    const el = f.el();
    const value = el && el.value ? el.value.trim() : "";
    if (value) out[f.key] = value;
  }
  return out;
}

// D19.37: branch + message for the deck-detail "Import new version"
// control. Elements are looked up by id at call time (like DECK_META_INPUTS
// does for the top form) because `_renderDeckDetail` recreates this markup
// on every deck load.
async function _deckApiSend(path, method, body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { "content-type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch("/api" + path, opts);
  const parsed = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(parsed.details || resp.statusText);
  return parsed;
}

function _renderDecksList(list) {
  _decksCurrent = list.data || [];
  if (!_decksCurrent.length) {
    decksListEl.innerHTML = emptyStateHtml("No decks yet",
      "Import a decklist from Moxfield, Archidekt or Deckbox to get started.",
      { label: "Import a deck", tab: "decks", focus: "#deckFile" });
    return;
  }
  decksListEl.innerHTML = `<ul class="deck-list">${_decksCurrent.map((d) => {
    const cmd = d.commander || d.companion
      ? `<span class="deck-cmd">${escapeHtml([d.commander, d.companion].filter(Boolean).join(" · "))}</span>`
      : `<span class="help">no commander</span>`;
    const warn = d.unresolved_count
      ? `<span class="upload-warn">${d.unresolved_count} unresolved</span>`
      : "";
    return `<li>
      <button type="button" class="deck-row${d.id === _deckSelectedId ? " selected" : ""}" data-deck-id="${escapeHtml(d.id)}">
        <span class="deck-name">${escapeHtml(d.name)}</span>
        ${cmd}
        <span class="deck-meta help">${plural(d.total_cards, "card")} · v${d.version} · ${escapeHtml(d.format)} · ${escapeHtml(_deckCoverageLabel(d))}${_deckMissingCostLabel(d) ? ` · <span class="deck-cost">${escapeHtml(_deckMissingCostLabel(d))}</span>` : ""} ${warn}</span>
      </button>
    </li>`;
  }).join("")}</ul>`;
}

// D19.44: "" means no filter and is deliberately NOT sent as an empty
// parameter — `?completion=` would be a value the server whitelist rejects,
// where omitting it means "all decks". Same three-valued care `_deckFolderQuery`
// takes, and the same reason: the distinction is exactly what a later
// refactor flattens.
function _deckCompletionQuery() {
  const v = deckCompletionEl ? deckCompletionEl.value : "";
  return v ? `&completion=${encodeURIComponent(v)}` : "";
}

// -------- Completion plan (D19.46) --------
//
// Renders BOTH plans the API returns, side by side. That is the whole point
// of the endpoint: at budget=10 the two complete the same decks while
// buying 20 cards vs 5, and showing one would hide that a choice exists.
// The UI therefore never picks a winner and never re-ranks — every number
// here is server-computed (D19.45), because a second implementation of
// "what does this deck still need" is exactly how Bug.36 happened.

function _planMoney(usd) {
  return `$${Number(usd).toFixed(2)}`;
}

// `by_cards` may include a deck whose gap has an unpriced card, so its total
// is a FLOOR. Saying "at least" is the difference between an honest number
// and one that quietly omits cards — the rule `unpriced_missing` set.
function _planTotal(plan) {
  const money = plan.spend_usd_is_floor ? `at least ${_planMoney(plan.spend_usd)}` : _planMoney(plan.spend_usd);
  const decks = `${plan.decks_completed} deck${plan.decks_completed === 1 ? "" : "s"}`;
  return `${decks} · ${plan.cards_to_buy} card${plan.cards_to_buy === 1 ? "" : "s"} · ${money}`;
}

function _planStep(step) {
  const cards = step.cards.map((c) => {
    const price = c.price_usd == null ? "—" : _planMoney(c.price_usd);
    return `<li><span class="plan-qty">${c.qty}x</span> ${escapeHtml(c.name)} <span class="plan-price">${price}</span></li>`;
  }).join("");
  // A step that costs nothing is the marginal-cost property made visible:
  // an earlier step already bought this deck's whole gap.
  const free = step.marginal_cards === 0 ? ` <span class="plan-free">already covered</span>` : "";
  const partial = step.cost_is_partial ? ` <span class="upload-warn">price incomplete</span>` : "";
  return `<li class="plan-step">
    <div class="plan-step-head"><strong>${escapeHtml(step.deck_name)}</strong>${free}${partial}</div>
    <div class="help">+${step.marginal_cards} cards · +${_planMoney(step.marginal_usd)} · running ${_planMoney(step.running_usd)}</div>
    ${cards ? `<ul class="plan-cards">${cards}</ul>` : ""}
  </li>`;
}

function _planColumn(title, blurb, plan) {
  const steps = plan.steps.length
    ? `<ol class="plan-steps">${plan.steps.map(_planStep).join("")}</ol>`
    : `<p class="empty">Nothing fits these limits.</p>`;
  return `<div class="plan-col">
    <h4>${escapeHtml(title)}</h4>
    <p class="help">${escapeHtml(blurb)}</p>
    <p class="plan-total">${escapeHtml(_planTotal(plan))}</p>
    ${steps}
  </div>`;
}

function _renderCompletionPlan(res) {
  // Decks skipped for having an unpriced gap are LISTED, not hidden — that
  // is how the cheapest-looking deck silently vanishes from a plan.
  const skipped = res.by_cost.skipped_unpriced || [];
  const skippedHtml = skipped.length
    ? `<p class="help plan-skipped">Not costed (a missing card has no price): ${
        skipped.map((s) => escapeHtml(s.deck_name)).join(", ")
      }. They can still appear under “fewest cards”.</p>`
    : "";
  deckPlanResultEl.innerHTML = `
    <div class="plan-cols">
      ${_planColumn("Cheapest", "Spend as little as possible.", res.by_cost)}
      ${_planColumn("Fewest cards", "Buy as few cards as possible.", res.by_cards)}
    </div>
    ${skippedHtml}
    <p class="help">${escapeHtml(res.note)}</p>`;
}

async function loadCompletionPlan() {
  if (!deckPlanResultEl) return;
  const parts = [];
  const budget = deckPlanBudgetEl && deckPlanBudgetEl.value.trim();
  const maxCards = deckPlanMaxCardsEl && deckPlanMaxCardsEl.value.trim();
  if (budget) parts.push(`budget=${encodeURIComponent(budget)}`);
  if (maxCards) parts.push(`max_cards=${encodeURIComponent(maxCards)}`);
  const qs = parts.length ? `?${parts.join("&")}` : "";
  _deckStatus(deckPlanStatusEl, "Planning…");
  try {
    const res = await api(`/decks/completion-plan${qs}`);
    _renderCompletionPlan(res);
    _deckStatus(deckPlanStatusEl, `${plural(res.incomplete_decks, "incomplete deck")} considered.`);
  } catch (err) {
    deckPlanResultEl.innerHTML = "";
    _deckStatus(deckPlanStatusEl, err.message || "failed", true);
  }
}

async function refreshDecksList() {
  if (!decksListEl) return;
  // F15: every path that changes the deck set (import, delete, move, sync)
  // ends here, so the folder counts are re-fetched with the list and cannot
  // stay at "All decks 0" after the first import.
  refreshDeckFolders();
  const q = deckQEl && deckQEl.value.trim() ? `&q=${encodeURIComponent(deckQEl.value.trim())}` : "";
  try {
    _renderDecksList(await api(`/decks?limit=200${q}${_deckFolderQuery()}${_deckSortQuery()}${_deckCompletionQuery()}`));
  } catch (err) {
    decksListEl.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
  }
}

function _renderDeckLinks(deck) {
  const links = DECK_LINK_FIELDS
    .filter((f) => deck[f.key])
    .map((f) => `<a class="ext-link" href="${escapeHtml(deck[f.key])}" target="_blank" rel="noopener noreferrer">${f.label}</a>`)
    .join(" ");
  return links ? `<div class="deck-links">${links}</div>` : "";
}

// D19.35: filter chips over the ALREADY-FETCHED interleaved version list —
// no second request, and no per-branch layout. The list itself stays ONE
// list ordered by version DESC; a chip click narrows which rows of that
// same list are shown.
function _renderTimelineFilterChips() {
  const counts = {};
  for (const v of _deckVersions) counts[v.branch] = (counts[v.branch] || 0) + 1;
  const chip = (branch, label, count, selected) =>
    `<button type="button" class="deck-branch-chip deck-branch-filter${selected ? " selected" : ""}"
       data-timeline-branch="${branch === null ? "" : escapeHtml(branch)}">${escapeHtml(label)} <span class="help">${count}</span></button>`;
  const branchChips = DECK_BRANCHES.filter((b) => counts[b]).map((b) =>
    chip(b, b, counts[b], _deckTimelineFilter === b)
  ).join("");
  return `<div class="deck-branch-chips deck-timeline-filter">
    ${chip(null, "All", _deckVersions.length, _deckTimelineFilter === null)}
    ${branchChips}
  </div>`;
}

// D19.48: version rows deliberately do NOT show unresolved_count.
//
// It is a write-time snapshot from resolve_names, which fuzzy-matches where
// the derived deck_cards index does not, and nothing ever recomputes it. So
// after Bug.36 fixed the collector-number parse, every pre-fix version row
// reported a number produced by a parser that no longer exists — and on a
// HEAD version it contradicted the header badge outright (1 here, 0 there,
// same content, same screen).
//
// The current state has an accurate home: the derived badge and D19.47's
// named list. The column stays in GET /decks/:id/versions as history; it
// just stops being rendered as though it described the present.
function _renderVersionRows() {
  if (!_deckVersions.length) return `<p class="empty">No versions.</p>`;
  // Head-aware, not merely "the globally highest version number" — a
  // pinned deck can have newer writes on OTHER branches that never moved
  // the pointer (D19.34), and the badge must follow the pointer or it
  // lies about which version is actually current.
  const headVersion = _deckDetailData ? _deckDetailData.version : _deckVersions[0].version;
  const rows = _deckTimelineFilter
    ? _deckVersions.filter((v) => v.branch === _deckTimelineFilter)
    : _deckVersions;
  if (!rows.length) return `<p class="empty">No versions on ${escapeHtml(_deckTimelineFilter)} yet.</p>`;
  return `<ul class="upload-list">${rows.map((v) => {
    const when = new Date(v.created_at).toLocaleString();
    const file = v.filename ? ` · ${escapeHtml(v.filename)}` : "";
    const isCurrent = v.version === headVersion;
    const branchChip = `<span class="deck-branch-chip deck-branch-chip-sm" data-branch-badge>${escapeHtml(v.branch)}</span>`;
    const msg = v.message ? `<div class="upload-meta deck-version-msg">${escapeHtml(v.message)}</div>` : "";
    return `<li class="upload-row">
      <div class="upload-main">
        <span class="upload-kind">v${v.version}</span>
        ${branchChip}
        <span class="upload-name">${escapeHtml(v.source)}${file}</span>
        <span class="help">${escapeHtml(when)}</span>
        ${isCurrent ? `<span class="deck-current">current</span>` : ""}
      </div>
      ${msg}
      <div class="upload-meta help">
        ${plural(v.total_cards, "card")} · ${v.distinct_cards} unique · ${escapeHtml(_fmtBytes(v.size_bytes))}
      </div>
      <div class="upload-actions">
        <a class="ext-link" href="/api/decks/${encodeURIComponent(_deckSelectedId)}/versions/${v.version}/download?name=${encodeURIComponent(_deckCurrentName())}">Download</a>
        ${isCurrent ? "" : `<button type="button" class="btn btn--ghost btn--sm deck-restore" data-version="${v.version}">Restore</button>`}
      </div>
    </li>`;
  }).join("")}</ul>`;
}

function _deckCurrentName() {
  const hit = _decksCurrent.find((d) => d.id === _deckSelectedId);
  return hit ? hit.name : "deck";
}

// ---- D19.27: editable deck metadata ------------------------------------
//
// METADATA only. D19.22 removed decklist editing by author decision — new
// versions come from imports and syncs — and `update_deck` appends a
// version solely when `decklist` is supplied, so nothing here can create
// one. `_deckMetaPayload` is pinned to never carry a decklist.
//
// Hostname validation stays server-side (D19.4); a wrong-host URL comes
// back as a 422 whose `details` we surface verbatim.

// Column, label, and whether blanking it is allowed. `name` is NOT NULL,
// so a blank name is DROPPED rather than nulled — the others are nullable
// and blanking them must actually unset them.
const DECK_META_FIELDS = [
  { key: "name", label: "Name", nullable: false },
  { key: "format", label: "Format", nullable: false },
  { key: "folder", label: "Folder", nullable: true },
  { key: "commander", label: "Commander", nullable: true },
  { key: "companion", label: "Companion", nullable: true },
  { key: "notes", label: "Description", nullable: true, multiline: true },
  { key: "deckbox_url", label: "Deckbox URL", nullable: true, url: true },
  { key: "moxfield_url", label: "Moxfield URL", nullable: true, url: true },
  { key: "archidekt_url", label: "Archidekt URL", nullable: true, url: true },
  // D19.38: which branch is allowed to move head. Empty option = null =
  // "follow every import" (today's behaviour) — a select, not free text,
  // since the value space is exactly the four `VALID_BRANCHES` plus that.
  {
    key: "trunk_branch", label: "Current version follows", nullable: true, select: true,
    options: ["", "main", "deckbox", "moxfield", "archidekt"],
  },
];

function _renderDeckMetaEditor(deck) {
  const rows = DECK_META_FIELDS.map((f) => {
    const id = `deckMeta_${f.key}`;
    const value = escapeHtml(deck[f.key] == null ? "" : String(deck[f.key]));
    let input;
    if (f.select) {
      const opts = f.options.map((o) => {
        const label = o || "follow every import";
        const sel = o === (deck[f.key] || "") ? " selected" : "";
        return `<option value="${escapeHtml(o)}"${sel}>${escapeHtml(label)}</option>`;
      }).join("");
      input = `<select id="${id}">${opts}</select>`;
    } else if (f.multiline) {
      input = `<textarea id="${id}" rows="3">${value}</textarea>`;
    } else {
      input = `<input id="${id}" type="${f.url ? "url" : "text"}" value="${value}">`;
    }
    return `<label for="${id}">${f.label}</label>${input}`;
  }).join("");
  return `
    <details id="deckMetaPanel" class="deck-meta-panel">
      <summary class="help">Edit details</summary>
      <p class="help">Saving details does not create a new version — only an import or sync does. It does move the deck to the top of "recently touched".</p>
      <div class="deck-meta-grid">${rows}</div>
      <div class="deck-meta-actions">
        <button type="button" id="deckMetaSave">Save details</button>
        <span id="deckMetaStatus" role="status" aria-live="polite"></span>
      </div>
    </details>`;
}

// `read(key)` is injected so this is testable without a DOM.
function _deckMetaPayload(read) {
  const out = {};
  for (const f of DECK_META_FIELDS) {
    const raw = read(f.key);
    const value = raw == null ? "" : String(raw).trim();
    if (value) {
      out[f.key] = value;
    } else if (f.nullable) {
      // `null`, not "" and not omission: omitting keeps the old value, and
      // an empty string stores a non-URL that D19.4 would have to special-
      // case. `null` is the only spelling that unsets a nullable column.
      out[f.key] = null;
    }
    // Non-nullable and blank -> dropped entirely. `decks.name` is NOT NULL.
  }
  return out;
}

async function _saveDeckMeta() {
  const status = $("#deckMetaStatus");
  const payload = _deckMetaPayload((key) => {
    const el = $(`#deckMeta_${key}`);
    return el ? el.value : "";
  });
  _deckStatus(status, "Saving…", false);
  try {
    await _deckApiSend(`/decks/${encodeURIComponent(_deckSelectedId)}`, "PATCH", payload);
    await refreshDecksList();
    await _loadDeck(_deckSelectedId);
    // Confirm AFTER the reload, not before: `_loadDeck` re-renders the
    // whole detail pane, so a message set beforehand is destroyed and the
    // save looks like it did nothing. Re-open the panel for the same
    // reason — a <details> re-renders closed.
    const panel = $("#deckMetaPanel");
    if (panel) panel.open = true;
    _deckStatus($("#deckMetaStatus"), "Saved.", false);
  } catch (err) {
    _deckStatus(status, err.message || "save failed", true);
  }
}

// ---- D19.37: import a new version onto THIS deck --------------------------
//
// The control the branch model was built for. Distinct from the top
// "Import a deck" card, which only ever creates a brand-new deck at
// version 1 (D19.36's drift entry) — this appends onto `_deckSelectedId`.
// File goes to the new `POST /decks/:id/versions`; paste reuses the
// already-shipped `PATCH /decks/:id` (D19.34), so no backend surface is
// duplicated for the paste path.
function _renderDeckVersionImport() {
  return `
    <details id="deckVersionImportPanel" class="deck-meta-panel deck-version-import">
      <summary class="help">Import new version</summary>
      <form id="deckVersionImportForm" class="inv-sync">
        <div class="inv-sync-row">
          <label for="deckVersionFile">Export file:</label>
          <input id="deckVersionFile" type="file" accept=".txt,.csv,.html,.htm,text/plain,text/csv,text/html">
          <label for="deckVersionBranch" class="visually-hidden">List</label>
          <select id="deckVersionBranch">
            <option value="main" selected>main</option>
            <option value="deckbox">deckbox</option>
            <option value="moxfield">moxfield</option>
            <option value="archidekt">archidekt</option>
          </select>
          <button type="submit" id="deckVersionImportBtn">Import new version</button>
        </div>
        <details class="inv-paste">
          <summary class="help">…or paste a decklist</summary>
          <label for="deckVersionPasteText" class="visually-hidden">Decklist text</label>
          <textarea id="deckVersionPasteText" rows="6" placeholder="1 Sol Ring"></textarea>
        </details>
        <label for="deckVersionMessage" class="visually-hidden">Message</label>
        <input id="deckVersionMessage" type="text" autocomplete="off" placeholder="What changed in this import">
      </form>
      <div id="deckVersionImportStatus" role="status" aria-live="polite"></div>
    </details>`;
}

// D19.39: the header's commander/companion follow the version being
// VIEWED, not head — `deck` supplies the OVERRIDE fields (a property of
// the deck, not of any one branch), `commander`/`companion` are the
// EFFECTIVE values to show. An override is marked "(fixed)" so a
// commander that does not change when the branch does reads as a
// deliberate pin rather than a bug.
function _deckCommanderCompanionHtml(deck, commander, companion) {
  const fixed = (on) => (on ? ` <span class="help" title="Explicit override — same on every list">(fixed)</span>` : "");
  const c = commander ? ` · ${escapeHtml(commander)}${fixed(deck.commander_override)}` : "";
  const p = companion ? ` · companion ${escapeHtml(companion)}${fixed(deck.companion_override)}` : "";
  return `${c}${p}`;
}

// Updates the header in place rather than re-rendering the whole detail
// pane — `_renderDeckDetail` also rebuilds the meta-editor and version
// import panels, which would lose their open/closed state on every
// branch-nav click.
function _updateDeckHeaderCommander(commander, companion) {
  const el = $("#deckHeaderCC");
  if (!el || !_deckDetailData) return;
  el.innerHTML = _deckCommanderCompanionHtml(_deckDetailData, commander, companion);
}

function _renderDeckDetail(deck) {
  // D19.35: the branch-head navigator and the head-aware "current" badge
  // in the timeline both need the fetched deck object, not just what was
  // passed into this render call.
  _deckDetailData = deck;
  const opts = _deckVersions.map((v) => `<option value="${v.version}">v${v.version}</option>`).join("");
  const newest = _deckVersions.length ? _deckVersions[0].version : 1;
  const prev = _deckVersions.length > 1 ? _deckVersions[1].version : newest;
  // `head_pinned` must be a VISIBLE state — a pinned deck whose head has
  // stopped auto-following imports must say so, or it reads as a bug
  // rather than a deliberate promote. D19.37 adds the un-pin control right
  // beside it: without one, a pin is a one-way door and every later
  // import keeps reporting success while the deck silently stops changing.
  const pinnedNote = deck.head_pinned
    ? `<p class="deck-head-pinned">\u{1F4CC} Pinned to v${deck.version} (${escapeHtml(deck.branch)}) <button type="button" id="deckUnpinBtn" class="btn btn--ghost btn--sm">Unpin</button></p>`
    : "";
  deckDetailEl.innerHTML = `
    <div class="deck-head">
      <h2>${escapeHtml(deck.name)}</h2>
      <button type="button" id="deckToWizardBtn" class="btn btn--ghost btn--sm">Open in Build</button>
      <button type="button" id="deckForkBtn" class="btn btn--ghost btn--sm">Fork</button>
      <button type="button" id="deckDeleteBtn" class="btn btn--danger btn--sm">Delete deck</button>
    </div>
    <p class="deck-sub">${escapeHtml(deck.format)}<span id="deckHeaderCC">${_deckCommanderCompanionHtml(deck, deck.commander, deck.companion)}</span> · ${plural(deck.total_cards, "card")} · ${deck.distinct_cards} unique${deck.unresolved_count ? ` · <span class="upload-warn">${deck.unresolved_count} unresolved</span>` : ""}</p>
    ${pinnedNote}
    <div id="deckScoreboard" class="deck-scoreboard"></div>
    <div id="deckSyncStrip" class="help deck-sync-strip"></div>
    ${_renderDeckLinks(deck)}
    ${_renderDeckMetaEditor(deck)}
    ${_renderDeckVersionImport()}

    <h3>Cards</h3>
    <div id="deckBranchNav" class="deck-branch-nav"></div>
    <div id="deckCards"></div>

    <h3>Evaluation</h3>
    <div class="deck-eval-head">
      <button type="button" id="deckEvalBtn" class="btn btn--ghost btn--sm">Evaluate</button>
    </div>
    <div id="deckEval"></div>

    <h3>Versions</h3>
    <div id="deckVersionsSection">
      ${_renderTimelineFilterChips()}
      <div id="deckVersions">${_renderVersionRows()}</div>
    </div>

    ${_deckVersions.length > 1 ? `<h3>Compare versions</h3>
    <form id="deckDiffForm" class="deck-diff-form">
      <label for="deckDiffA" class="visually-hidden">Baseline version</label>
      <select id="deckDiffA">${opts}</select>
      <span class="help">→</span>
      <label for="deckDiffB" class="visually-hidden">Compared version</label>
      <select id="deckDiffB">${opts}</select>
      <button type="submit">Diff</button>
    </form>
    <div id="deckDiffResult"></div>` : ""}`;
  _renderBranchNav();
  const aSel = $("#deckDiffA");
  const bSel = $("#deckDiffB");
  if (aSel) aSel.value = String(prev);
  if (bSel) bSel.value = String(newest);
}

// ---- Deck evaluation (GET /decks/:id/evaluate) --------------------------
//
// Summary + guide-driven tips for HEAD. The server does all the grading
// (decklist/evaluate.py); this only renders. `warn` tips are the guide's
// non-negotiables (lands, functional slots), `info` are shaping notes.

function _renderDeckEval(res) {
  const el = $("#deckEval");
  if (!el) return;
  const s = res.summary || {};
  const slots = s.slots || {};
  const slotRows = Object.keys(slots).map((lab) => {
    const v = slots[lab];
    const cls = v.delta < 0 ? "deck-eval-under" : "";
    const sign = v.delta > 0 ? "+" : "";
    return `<tr><td>${escapeHtml(lab.replace(/_/g, " "))}</td>
      <td class="qty">${v.count}</td><td class="qty">${v.target}</td>
      <td class="qty ${cls}">${sign}${v.delta}</td></tr>`;
  }).join("");
  const tips = (res.tips || []).map((t) =>
    `<li class="deck-eval-tip deck-eval-${escapeHtml(t.severity)}">${escapeHtml(t.message)}</li>`
  ).join("");
  const cov = s.coverage_pct != null ? ` · ${formatPct(s.coverage_pct)} owned` : "";
  const peak = s.curve_peak != null ? `MV${escapeHtml(String(s.curve_peak))}` : "—";
  el.innerHTML = `
    <p class="help">${plural(s.total_cards, "card")} · ${s.lands} lands · ${s.nonland} nonland · curve peak ${peak}${cov}</p>
    <table class="deck-eval-slots"><thead><tr>
      <th>Slot</th><th>Have</th><th>Target</th><th>&Delta;</th>
    </tr></thead><tbody>${slotRows}</tbody></table>
    ${tips ? `<ul class="deck-eval-tips">${tips}</ul>` : `<p class="empty">On template — no tips.</p>`}`;
}

// F15: the Evaluation panel is cached in the DOM; ownership changes made
// elsewhere (an inventory import) leave it stale ("0% owned" beside a list
// that says 32%). Re-run it when the Decks tab is re-entered with it showing.
function _refreshDeckEvalIfShown() {
  const el = $("#deckEval");
  if (_deckSelectedId && el && el.children.length > 0) _loadDeckEval(_deckSelectedId);
}

async function _loadDeckEval(deckId) {
  const el = $("#deckEval");
  if (el) el.innerHTML = `<p class="empty">Evaluating…</p>`;
  try {
    const res = await api(`/decks/${encodeURIComponent(deckId)}/evaluate`);
    _renderDeckEval(res);
  } catch (err) {
    if (el) el.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
  }
}

// ---- D19.22: deck card table --------------------------------------------
//
// The deck renders as a table of cards, not an editable blob. READ-ONLY by
// author decision: new versions come only from imports and syncs, so the
// textarea and its "save as new version" control are gone. `PATCH
// /decks/{id}` with a decklist still exists in the API — it is shipped,
// tested, MCP-exposed surface — this is a UI affordance being withdrawn,
// not a capability being deleted.
//
// Rows come from GET /decks/{id}/cards (D19.20), which is already
// partitioned into commander / main / sideboard and carries quantity-aware
// ownership. Nothing here re-parses a decklist.

let _deckCards = [];
let _deckCardSummary = null;
let _deckCardSort = { key: "name", dir: 1 };

// Commander first: a reader looks for it before the 99.
const _DECK_SECTION_ORDER = { commander: 0, main: 1, sideboard: 2 };
const _DECK_SECTION_LABEL = { commander: "Commander", main: "Mainboard", sideboard: "Sideboard" };

function _deckOwnBadge(row) {
  // Same vocabulary as the Inventory and Wishlist tabs (`_ownedBadge`):
  // 📦 for inventory, ⭐ for wishlist. A second icon set for the same
  // concept would read as a different concept.
  const owned = row.qty_owned || 0;
  const qty = row.qty || 0;
  const state = owned >= qty ? "full" : owned > 0 ? "partial" : "none";
  const label = `own ${owned} of ${qty}`;
  const box = state === "none"
    ? `<span class="deck-own-none" role="img" title="${label}" aria-label="${label}">—</span>`
    : `<span class="coll-icon deck-own-${state}" role="img" title="${label}" aria-label="${label}">${ICON_BOX}</span>`;
  // The count is not decorative: it carries the same information as the
  // icon for anyone who cannot distinguish the two states.
  const count = `<span class="deck-own-count">${owned}/${qty}</span>`;
  const wish = row.on_wishlist
    ? ` <span class="coll-icon wanted-icon" role="img" title="On your wishlist" aria-label="On your wishlist">${ICON_STAR}</span>`
    : "";
  return `${box}${count}${wish}`;
}

function _deckCardSortValue(row, key) {
  if (key === "qty") return row.qty || 0;
  if (key === "owned") return row.qty_owned || 0;
  if (key === "missing") return row.qty_missing || 0;
  if (key === "cmc") return row.cmc === null || row.cmc === undefined ? -1 : row.cmc;
  if (key === "price") return row.price_usd === null || row.price_usd === undefined ? -1 : row.price_usd;
  if (key === "type") return (row.type_line || "").toLowerCase();
  return (row.name || "").toLowerCase();
}

function _sortedDeckCards() {
  // Section is always the primary key — sorting across sections would mix
  // the commander into the 99 and break the table's structure.
  const { key, dir } = _deckCardSort;
  return _deckCards.slice().sort((a, b) => {
    const sa = _DECK_SECTION_ORDER[a.section] ?? 9;
    const sb = _DECK_SECTION_ORDER[b.section] ?? 9;
    if (sa !== sb) return sa - sb;
    const va = _deckCardSortValue(a, key);
    const vb = _deckCardSortValue(b, key);
    if (va < vb) return -dir;
    if (va > vb) return dir;
    return (a.name || "").localeCompare(b.name || "");
  });
}

function _deckCardRow(row) {
  // `resolved: false` rows have `id: null`. They still count toward the
  // deck total, so hiding them would make the table disagree with its own
  // summary — but they must not be clickable, or the click fetches
  // /cards/null.
  const clickable = row.resolved && row.id;
  const cls = clickable ? "deck-card-row" : "deck-card-row deck-card-unresolved";
  const idAttr = clickable ? ` data-id="${escapeHtml(row.id)}"` : "";
  const name = clickable
    ? escapeHtml(row.name)
    : `${escapeHtml(row.name)} <span class="help">(unresolved)</span>`;
  return `<tr class="${cls}"${idAttr}>
    <td class="qty">${row.qty}</td>
    <td class="deck-card-name">${name}</td>
    <td class="deck-card-type help">${escapeHtml(row.type_line || "")}</td>
    <td class="deck-card-cost">${row.mana_cost ? renderSymbols(row.mana_cost) : ""}</td>
    <td class="deck-card-price">${_deckCardPrice(row)}</td>
    <td class="deck-card-own">${_deckOwnBadge(row)}</td>
  </tr>`;
}

// D19.47: name the cards that failed to match, above the table.
//
// A `resolved: false` row still counts toward the deck's totals (D19.20), so
// it is visible in the table but indistinguishable from a card that merely
// has no price. The whole point here is to SEE the string that did not
// match, so names render verbatim as stored — trimming or prettifying them
// would hide the very thing being diagnosed.
//
// Renders nothing at all when there are none. An empty "Unresolved" heading
// is noise on every clean deck, and most decks are clean.
function _renderUnresolvedCards() {
  const bad = _deckCards.filter((c) => !c.resolved);
  if (!bad.length) return "";
  const items = bad
    .map((c) => `<li><span class="plan-qty">${c.qty}x</span> <code>${escapeHtml(c.name)}</code></li>`)
    .join("");
  return `<div class="deck-unresolved">
    <strong>${bad.length} unresolved card${bad.length === 1 ? "" : "s"}</strong>
    <p class="help">No card in the catalogue matches these names, so they cannot be priced or matched against your inventory — they still count toward the deck's totals.</p>
    <ul class="deck-unresolved-list">${items}</ul>
  </div>`;
}

function _renderDeckCardTable() {
  const el = $("#deckCards");
  if (!el) return;
  if (!_deckCards.length) {
    el.innerHTML = `<p class="empty">No cards.</p>`;
    return;
  }
  const rows = _sortedDeckCards();
  let out = _renderUnresolvedCards();
  let section = null;
  for (const row of rows) {
    if (row.section !== section) {
      section = row.section;
      const n = rows.filter((r) => r.section === section).reduce((a, r) => a + r.qty, 0);
      out += `<tr class="deck-card-section"><th colspan="6">${escapeHtml(_DECK_SECTION_LABEL[section] || section)} <span class="help">${n}</span></th></tr>`;
    }
    out += _deckCardRow(row);
  }
  const s = _deckCardSummary || {};
  const arrow = (k) => (_deckCardSort.key === k ? (_deckCardSort.dir > 0 ? " ▲" : " ▼") : "");
  el.innerHTML = `
    <p class="help deck-card-summary">${plural(s.total_cards || 0, "card")} · ${s.owned_cards || 0} owned · ${s.missing_cards || 0} missing${
      s.coverage_pct === null || s.coverage_pct === undefined ? "" : ` · ${formatPct(s.coverage_pct)} coverage`
    }${_deckTotalValueLabel(s) ? ` · <span class="deck-cost" title="Rough prices, cheapest printing per card, from the last card refresh">${escapeHtml(_deckTotalValueLabel(s))}</span>` : ""}${_deckMissingCostLabel(s) ? ` · <span class="deck-cost" title="Rough prices, cheapest printing per card, from the last card refresh">${escapeHtml(_deckMissingCostLabel(s))}</span>` : ""}${s.unresolved_cards ? ` · <span class="upload-warn">${s.unresolved_cards} unresolved</span>` : ""}</p>
    <table class="deck-card-table">
      <thead><tr>
        <th class="qty"><button type="button" class="deck-sort" data-sort="qty">#${arrow("qty")}</button></th>
        <th><button type="button" class="deck-sort" data-sort="name">Card${arrow("name")}</button></th>
        <th><button type="button" class="deck-sort" data-sort="type">Type${arrow("type")}</button></th>
        <th><button type="button" class="deck-sort" data-sort="cmc">Cost${arrow("cmc")}</button></th>
        <th class="deck-card-price"><button type="button" class="deck-sort" data-sort="price">Price${arrow("price")}</button></th>
        <th><button type="button" class="deck-sort" data-sort="owned">Owned${arrow("owned")}</button></th>
      </tr></thead>
      <tbody>${out}</tbody>
    </table>`;
}

// `version` omitted -> the deck's HEAD, read from the derived index
// (unchanged D19.20 behaviour). `version` given -> D19.35's branch-head
// navigator, computed live server-side and NEVER written to the index —
// viewing another branch's head must not become promoting it.
async function _loadDeckCards(deckId, version) {
  const el = $("#deckCards");
  if (!el) return;
  el.innerHTML = `<p class="loading">Loading cards…</p>`;
  try {
    const qs = version ? `?version=${encodeURIComponent(version)}` : "";
    const body = await api(`/decks/${encodeURIComponent(deckId)}/cards${qs}`);
    _deckCards = body.data || [];
    _deckCardSummary = body.summary || null;
    _renderDeckCardTable();
    // D19.39: the header must describe the version being VIEWED. `version`
    // given -> the API already computed the effective commander/companion
    // for THAT version (D19.39's backend addition); `version` omitted ->
    // head, whose values are already on `_deckDetailData` from the deck GET.
    if (version) {
      _updateDeckHeaderCommander(body.commander, body.companion);
    } else if (_deckDetailData) {
      _updateDeckHeaderCommander(_deckDetailData.commander, _deckDetailData.companion);
    }
  } catch (err) {
    el.innerHTML = `<p class="state-error" role="alert">Cards unavailable: ${escapeHtml(err.message || "network")}</p>`;
  }
}

// ---- D19.35: branch-head navigator ---------------------------------------
//
// The card table follows the deck's HEAD by default (unchanged). Selecting
// another branch VIEWS that branch's own latest version — its "head" in
// the git sense, i.e. its newest write, whether or not that write ever
// moved the deck's pointer. Viewing is not promoting: only the explicit
// `POST /decks/{id}/head` control (D19.34) pins the pointer, and nothing
// here calls it.
//
// `_deckVersions` is already sorted version DESC (the API's own order), so
// the first row matching a branch IS that branch's own latest — no extra
// request, no extra sort.
function _deckBranchHeadVersion(branch) {
  const hit = _deckVersions.find((v) => v.branch === branch);
  return hit ? hit.version : null;
}

function _renderBranchNav() {
  const el = $("#deckBranchNav");
  if (!el || !_deckDetailData) return;
  const deck = _deckDetailData;
  // Only one source has versions: there is nothing to switch between.
  if (!_deckViewBranch && DECK_BRANCHES.filter((b) => _deckBranchHeadVersion(b) !== null).length < 2) {
    el.innerHTML = "";
    return;
  }
  const headChip = `<button type="button" class="deck-branch-chip${_deckViewBranch === null ? " selected" : ""}" data-nav-head>Current</button>`;
  const branchChips = DECK_BRANCHES.map((b) => {
    const version = _deckBranchHeadVersion(b);
    const disabled = version === null;
    const cls = ["deck-branch-chip"];
    if (_deckViewBranch === b) cls.push("selected");
    if (disabled) cls.push("deck-branch-chip-empty");
    const headSuffix = b === deck.branch ? " · current" : "";
    return `<button type="button" class="${cls.join(" ")}" data-nav-branch="${b}"${disabled ? " disabled" : ""}>${escapeHtml(b)}${headSuffix}</button>`;
  }).join("");
  const viewedVersion = _deckViewBranch === null ? deck.version : _deckBranchHeadVersion(_deckViewBranch);
  const banner = _deckViewBranch !== null && viewedVersion !== deck.version
    ? `<p class="help deck-branch-viewing">Viewing <strong>${escapeHtml(_deckViewBranch)}</strong>'s latest version — v${viewedVersion}, not the deck's current version.</p>`
    : "";
  el.innerHTML = `<div class="deck-branch-chips">${headChip}${branchChips}</div>${banner}`;
}

// ---- D19.40: sync strip — "are my copies in sync?", no fetch ------------
//
// `GET /decks/{id}/branches` compares every branch's own stored head
// against the deck's HEAD server-side via `deck_card_rows()` — never raw
// text (D19.4's byte-for-byte storage means a Deckbox export and a
// Moxfield export of the identical deck differ textually, and this must
// still read as in sync). This renders those counts; it never pulls from
// Deckbox/Moxfield/Archidekt itself.
function _deckSyncChip(row) {
  const d = row.differs || {};
  const total = (d.added || 0) + (d.removed || 0) + (d.changed || 0);
  // Head's own row is trivially in sync with itself — flagged, not diffed.
  const inSync = row.is_head || total === 0;
  const mark = inSync ? "✓" : "✗";
  const suffix = inSync ? "" : ` ${total} card${total === 1 ? "" : "s"}`;
  const cls = `deck-branch-chip-sm ${inSync ? "deck-sync-ok" : "deck-sync-diff"}`;
  return `<span class="${cls}" title="v${row.version}">${escapeHtml(row.branch)} ${mark}${suffix}</span>`;
}

function _renderSyncStrip(rows) {
  const el = $("#deckSyncStrip");
  if (!el) return;
  // A single-branch deck has nothing to compare — an empty strip, not a
  // lone chip that reads as a broken comparison.
  el.innerHTML = rows && rows.length > 1 ? rows.map(_deckSyncChip).join(" · ") : "";
}

async function _loadDeckSyncStrip(deckId) {
  try {
    const body = await api(`/decks/${encodeURIComponent(deckId)}/branches`);
    _renderSyncStrip(body.data || []);
  } catch {
    _renderSyncStrip([]);
  }
}

// Render the wizard-style scoreboard (slot deltas + curve vs guide targets)
// on a saved deck's detail pane, from its HEAD decklist.
async function _renderDeckStats(deck) {
  const el = $("#deckScoreboard");
  if (!el || !deck || !deck.decklist) return;
  try {
    const a = await fetch("/api/decks/analyze", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ decklist: deck.decklist }),
    }).then((r) => (r.ok ? r.json() : null));
    if (!a) { el.innerHTML = ""; return; }
    const lands = (a.counts && a.counts.land) || 0;
    const nonland = Object.values(a.curve || {}).reduce((s, v) => s + v, 0);
    const b = await fetch("/api/decks/bracket", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ decklist: deck.decklist }),
    }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    const total = deck.total_cards ?? nonland + lands;
    const bl = b ? bracketLabel(b.bracket, total) : "";
    const bracket = bl ? ` · <span title="Rough estimate of the 1-5 power bracket">${bl}</span>` : "";
    const combos = await fetch("/api/decks/combos", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ decklist: deck.decklist }),
    }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    el.innerHTML = `<p class="help">${plural(total, "card")} · ${lands} lands / 38${bracket}</p>`
      + _renderDeckSlots(a) + _renderDeckCurve(a.curve, _CURVE_TARGET_MAP)
      + _renderComboPanel(combos && combos.combos, null);
  } catch (_e) { el.innerHTML = ""; }
}

async function _loadDeck(deckId) {
  _deckSelectedId = deckId;
  // Reset per-deck view state — otherwise selecting a different deck could
  // keep an old branch filter or navigator selection that has no meaning
  // for the newly-selected deck.
  _deckTimelineFilter = null;
  _deckViewBranch = null;
  deckDetailEl.innerHTML = `<p class="loading">Loading…</p>`;
  try {
    const [deck, versions] = await Promise.all([
      api(`/decks/${encodeURIComponent(deckId)}`),
      api(`/decks/${encodeURIComponent(deckId)}/versions?limit=500`),
    ]);
    _deckVersions = versions.data || [];
    _renderDeckDetail(deck);
    _renderDeckStats(deck);
    _loadDeckCards(deckId);
    _loadDeckSyncStrip(deckId);
    _renderDecksList({ data: _decksCurrent });
  } catch (err) {
    deckDetailEl.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
  }
}

function _renderDeckDiff(d, targetSel) {
  const el = $(targetSel || "#deckDiffResult");
  if (!el) return;
  // PR-style: one row per change, removals then additions, a quantity
  // change rendered as the pair (`-3 Sol Ring` then `+1 Sol Ring`) the way
  // a line edit shows in a code diff.
  //
  // The +/− prefix is not decoration. Colour alone excludes anyone who
  // cannot separate red from green, so the sign carries the same meaning
  // and the colour merely reinforces it.
  const row = (kind, sign, qty, name, note) => `<tr class="deck-diff-${kind}">
    <td class="deck-diff-sign" aria-hidden="true">${sign}</td>
    <td class="qty">${qty}</td>
    <td>${escapeHtml(name)}${note ? ` <span class="help">${escapeHtml(note)}</span>` : ""}</td>
  </tr>`;

  const rows = [];
  for (const x of d.removed || []) rows.push(row("del", "−", x.qty, x.name, ""));
  for (const x of d.added || []) rows.push(row("add", "+", x.qty, x.name, ""));
  for (const x of d.qty_changed || []) {
    rows.push(row("del", "−", x.from, x.name, `was ${x.from}`));
    rows.push(row("add", "+", x.to, x.name, `now ${x.to}`));
  }

  if (!rows.length) {
    el.innerHTML = `<p class="empty">Identical.</p>`;
    return;
  }
  el.innerHTML = `
    <p class="help">${d.a_total} cards → ${d.b_total} cards · ${d.unchanged_count} unchanged</p>
    <table class="deck-diff-table"><tbody>${rows.join("")}</tbody></table>`;
}

// ---- D19.36: post-import diff, two subtabs -------------------------------
//
// Reuses D19.6's two-fetch + POST /decks/diff pipeline and D19.23's
// PR-style table wholesale — no new diff endpoint, no second renderer.
// Both bases come from D19.34's `diff_bases` on the import response
// (`{main, branch}`, each a version number or null); the client never
// recomputes which version is "the base", only fetches and diffs what the
// server already decided.
let _deckImportDiffState = null;

const _DECK_IMPORT_DIFF_TABS = [
  { key: "main", label: "Compared with the main list" },
  { key: "branch", label: "Compared with previous import" },
];

// A null base means there is nothing on that side to diff against — a
// brand-new branch, or (for `main`) a deck that has never had a version on
// `main` at all. Diffing against an empty decklist would report the whole
// import as "added", which reads as a change that never happened, so this
// is an explicit empty state instead of a diff call.
const _DECK_IMPORT_DIFF_EMPTY = {
  main: "Nothing on the main list yet to compare against.",
  branch: "First import in this list — nothing to compare against.",
};

function _renderImportDiffTabs() {
  const state = _deckImportDiffState;
  if (!state) return "";
  return _DECK_IMPORT_DIFF_TABS.map(
    (t) =>
      `<button type="button" class="deck-branch-chip${state.active === t.key ? " selected" : ""}" data-diff-tab="${t.key}">${t.label}</button>`
  ).join("");
}

async function _showImportDiff(deckId, created) {
  const el = deckImportDiffEl;
  if (!el) return;
  // A deduped import changed nothing on its branch — no diff at all, per
  // the row's explicit ask, rather than a diff that would always be empty.
  if (created.version_created === false) {
    _deckImportDiffState = null;
    el.innerHTML = `<p class="help">No changes — identical to this list's previous version.</p>`;
    return;
  }
  if (!created.version_created || !created.diff_bases
      || (created.diff_bases.main == null && created.diff_bases.branch == null)) {
    // Nothing to compare against (first import): no diff UI at all.
    _deckImportDiffState = null;
    el.innerHTML = "";
    return;
  }
  _deckImportDiffState = {
    deckId,
    importedVersion: created.imported_version,
    bases: created.diff_bases,
    active: "main",
    cache: {},
  };
  el.innerHTML = `
    <p class="help">What changed:</p>
    <div class="deck-branch-chips" role="tablist">${_renderImportDiffTabs()}</div>
    <div id="deckImportDiffResult"></div>`;
  await _renderImportDiffTab("main");
}

async function _renderImportDiffTab(kind) {
  const state = _deckImportDiffState;
  const resultEl = $("#deckImportDiffResult");
  if (!state || !resultEl) return;
  state.active = kind;
  for (const btn of deckImportDiffEl.querySelectorAll(".deck-branch-chip")) {
    btn.classList.toggle("selected", btn.dataset.diffTab === kind);
  }
  const base = state.bases[kind];
  if (base === null || base === undefined) {
    resultEl.innerHTML = `<p class="empty">${_DECK_IMPORT_DIFF_EMPTY[kind]}</p>`;
    return;
  }
  if (state.cache[kind]) {
    _renderDeckDiff(state.cache[kind], "#deckImportDiffResult");
    return;
  }
  resultEl.innerHTML = `<p class="help">Diffing…</p>`;
  try {
    const [baseVer, newVer] = await Promise.all([
      api(`/decks/${encodeURIComponent(state.deckId)}/versions/${base}`),
      api(`/decks/${encodeURIComponent(state.deckId)}/versions/${state.importedVersion}`),
    ]);
    const diff = await _deckApiSend("/decks/diff", "POST", { a: baseVer.decklist, b: newVer.decklist });
    state.cache[kind] = diff;
    // The fetch is async; only paint if this tab is still the one selected.
    if (state.active === kind) _renderDeckDiff(diff, "#deckImportDiffResult");
  } catch (err) {
    resultEl.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
  }
}

if (deckImportDiffEl) {
  deckImportDiffEl.addEventListener("click", (e) => {
    const tab = e.target.closest("button[data-diff-tab]");
    if (tab) _renderImportDiffTab(tab.dataset.diffTab);
  });
}

// ---- D19.37: import a new version onto THIS deck, and un-pin head -------
//
// Distinct from the top "Import a deck" card, which only ever creates a
// brand-new deck at version 1 (D19.36's drift entry) — these append onto
// `_deckSelectedId`. File goes to the new `POST /decks/:id/versions`;
// paste reuses the already-shipped `PATCH /decks/:id` (D19.34), so no
// backend surface is duplicated for the paste path.
const DECK_VERSION_META_INPUTS = [
  { key: "branch", el: () => $("#deckVersionBranch"), reset: "main" },
  { key: "message", el: () => $("#deckVersionMessage") },
];

async function _importDeckVersion() {
  const deckId = _deckSelectedId;
  const status = $("#deckVersionImportStatus");
  const fileEl = $("#deckVersionFile");
  const pasteEl = $("#deckVersionPasteText");
  const file = fileEl && fileEl.files && fileEl.files[0];
  const pasted = pasteEl ? pasteEl.value.trim() : "";
  if (!file && !pasted) {
    _deckStatus(status, "Pick an export file or paste a decklist first.", true);
    return;
  }
  _deckStatus(status, "Importing…", false);
  if (deckImportDiffEl) deckImportDiffEl.innerHTML = "";
  _deckImportDiffState = null;
  const meta = _deckImportMeta(DECK_VERSION_META_INPUTS);
  try {
    let created;
    if (file) {
      // New multipart entry point onto an EXISTING deck (D19.37) — distinct
      // from `POST /decks/upload`, which always creates a new deck.
      const fd = new FormData();
      fd.append("file", file);
      for (const [k, v] of Object.entries(meta)) fd.append(k, v);
      const resp = await fetch(`/api/decks/${encodeURIComponent(deckId)}/versions`, { method: "POST", body: fd });
      created = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(created.details || resp.statusText);
    } else {
      // Paste-onto-an-existing-deck is the ALREADY-SHIPPED `PATCH` path
      // (D19.34) — not duplicated here.
      created = await _deckApiSend(`/decks/${encodeURIComponent(deckId)}`, "PATCH", { decklist: pasted, ...meta });
    }
    _deckStatus(status, `Imported v${created.imported_version} — ${created.total_cards} cards, ${created.unresolved_count} unresolved.`, false);
    // The exact same diff renderer the top import card uses (D19.36) — this
    // is the control that finally reaches it with a real, non-null base.
    await _showImportDiff(deckId, created);
    if (fileEl) fileEl.value = "";
    if (pasteEl) pasteEl.value = "";
    for (const f of DECK_VERSION_META_INPUTS) {
      const el = f.el();
      if (el) el.value = f.reset || "";
    }
    await refreshDecksList();
    await _loadDeck(deckId);
  } catch (err) {
    _deckStatus(status, err.message || "import failed", true);
  }
}

async function _unpinDeckHead() {
  try {
    await _deckApiSend(`/decks/${encodeURIComponent(_deckSelectedId)}/head`, "POST", { version: null });
    await refreshDecksList();
    await _loadDeck(_deckSelectedId);
  } catch (err) {
    _deckStatus(deckLibStatusEl, err.message || "failed", true);
  }
}

// ---- D19.25: library sort ------------------------------------------------
//
// Sorting is a SERVER concern (D19.21). The list is paginated, so
// reordering the rows already on screen would leave the top-coverage deck
// stranded on page 2 — the sort has to happen before LIMIT.

function _deckSortQuery() {
  const key = deckSortEl ? deckSortEl.value : "updated";
  const dir = deckSortDirEl ? deckSortDirEl.value : "desc";
  return `&sort=${encodeURIComponent(key)}&direction=${encodeURIComponent(dir)}`;
}

function _deckSortDefaultDir(key) {
  // The server defaults direction PER KEY: coverage descends (most
  // complete first, D19.21) while missing_cost ascends (cheapest to
  // finish first, D19.28). This UI always sends an explicit direction, so
  // picking a key has to move the direction control with it — otherwise
  // "cheapest to finish" silently lists the most expensive deck first.
  if (key === "missing_cost" || key === "name") return "asc";
  return "desc";
}

function _deckCardPrice(row) {
  // Unpriced is UNKNOWN, not free. ~5% of the corpus carries no
  // `price_usd`, and rendering those as "$0.00" would make a card look
  // worthless rather than unmeasured — the same distinction
  // `_deckMissingCostLabel` draws for a deck (D19.28/D19.29). A genuinely
  // $0.00 card still renders as $0.00, so the two stay tellable apart.
  const usd = row ? row.price_usd : null;
  if (usd === null || usd === undefined) return `<span class="help">—</span>`;
  return `$${usd.toFixed(2)}`;
}

function _deckTotalValueLabel(summary) {
  // What the WHOLE deck is worth, including cards already owned — as
  // opposed to `_deckMissingCostLabel`, which prices only the gap.
  const usd = summary ? summary.total_usd : null;
  if (usd === null || usd === undefined) return "";
  const unpriced = (summary && summary.unpriced_total) || 0;
  // A $0.00 deck whose cards are simply unpriced must say so, or it reads
  // as worthless (D19.28's rule, applied to the deck total).
  const suffix = unpriced ? ` \u00b7 ${unpriced} unpriced` : "";
  return `$${usd.toFixed(2)} value${suffix}`;
}

function _deckMissingCostLabel(src) {
  // `missing_usd` is null — not 0 — for a deck the derived index has not
  // covered yet (D19.19, and D19.24 until the rebuild is automatic).
  // "Not indexed" is not "costs nothing", the same distinction
  // `_deckCoverageLabel` draws for `coverage_pct`.
  const usd = src ? src.missing_usd : null;
  const unpriced = (src && src.unpriced_missing) || 0;
  if (usd === null || usd === undefined) return "cost unknown";
  // A finished deck gets no cost segment at all rather than a noisy
  // "$0.00" — but only when there is genuinely nothing left to buy.
  if (!usd && !unpriced) return "";
  // D19.28: ~5% of the corpus has no price, so a total that quietly omits
  // cards must say so. A silently partial figure is worse than an
  // admittedly incomplete one.
  const suffix = unpriced ? ` \u00b7 ${unpriced} unpriced` : "";
  return `$${usd.toFixed(2)} to finish${suffix}`;
}

function _deckCoverageLabel(deck) {
  // `coverage_pct` is null — not 0 — for a deck the derived index has not
  // covered yet (D19.19, and D19.24 until the rebuild is automatic).
  // Rendering null as 0% claims you own none of it, which is a different
  // and wrong statement.
  if (deck.coverage_pct === null || deck.coverage_pct === undefined) {
    return "coverage unknown";
  }
  return `${Math.round(deck.coverage_pct)}% owned`;
}

// ---- D19.18: folder tree -------------------------------------------------
//
// Folders are a path string on the deck (D19.16), not a table, so the tree
// is entirely a rendering of `GET /decks/folders`. That endpoint already
// DERIVES ancestors — a deck filed at `Commander/Budget` yields a
// `Commander` node with `deck_count: 0` and `total_deck_count: 1` — so this
// code must never re-infer a parent from a child's path. Two
// implementations of the same rule are free to disagree.

// Selection is three-valued, matching the three distinct list requests the
// API exposes. Conflating the last two either hides every unfiled deck or
// ignores the selection entirely.
const _FOLDER_ALL = null;      // omit `folder=`      -> no filter
const _FOLDER_UNFILED = "";    // `?folder=` empty    -> unfiled only
let _deckFolderSel = _FOLDER_ALL;
let _deckFolders = [];
let _deckUnfiledCount = 0;
const _deckFolderCollapsed = new Set();

function _deckFolderQuery() {
  if (_deckFolderSel === _FOLDER_ALL) return "";
  if (_deckFolderSel === _FOLDER_UNFILED) return "&folder=";
  return `&folder=${encodeURIComponent(_deckFolderSel)}&include_subfolders=true`;
}

function _folderHasChildren(path) {
  return _deckFolders.some((f) => f.path.startsWith(path + "/"));
}

function _folderHidden(path) {
  // Hidden when any ANCESTOR is collapsed — not just the direct parent.
  const parts = path.split("/");
  for (let i = 1; i < parts.length; i += 1) {
    if (_deckFolderCollapsed.has(parts.slice(0, i).join("/"))) return true;
  }
  return false;
}

function _folderRow(sel, label, count, extra) {
  const on = _deckFolderSel === sel;
  return `<li>
    <button type="button" class="deck-folder-row${on ? " selected" : ""}"
      data-folder="${sel === null ? "" : escapeHtml(sel)}"
      data-folder-kind="${sel === null ? "all" : sel === "" ? "unfiled" : "path"}">
      ${extra || ""}<span class="deck-folder-name">${escapeHtml(label)}</span>
      <span class="deck-folder-count help">${count}</span>
    </button>
  </li>`;
}

function _renderFolderTree() {
  if (!deckFoldersEl) return;
  const total = _deckFolders.reduce((n, f) => n + f.deck_count, 0) + _deckUnfiledCount;

  const nodes = _deckFolders.filter((f) => !_folderHidden(f.path)).map((f) => {
    const kids = _folderHasChildren(f.path);
    const collapsed = _deckFolderCollapsed.has(f.path);
    const toggle = kids
      ? `<span class="deck-folder-toggle" data-toggle="${escapeHtml(f.path)}" role="img"
           aria-label="${collapsed ? "Expand" : "Collapse"}">${collapsed ? "▸" : "▾"}</span>`
      : `<span class="deck-folder-toggle deck-folder-leaf"></span>`;
    // Both counts are shown when they differ: a parent holding no decks of
    // its own still reads sensibly as "0 here, 12 below".
    const count = f.deck_count === f.total_deck_count
      ? `${f.deck_count}`
      : `${f.deck_count} / ${f.total_deck_count}`;
    const row = _folderRow(f.path, f.name, count, toggle);
    return row.replace("<li>", `<li style="--depth:${f.depth}">`);
  }).join("");

  deckFoldersEl.innerHTML = `<ul class="deck-folder-tree">
    ${_folderRow(_FOLDER_ALL, "All decks", total, "")}
    ${nodes}
    ${_folderRow(_FOLDER_UNFILED, "Unfiled", _deckUnfiledCount, "")}
  </ul>
  <div class="deck-folder-actions">
    <button type="button" id="deckFolderRenameBtn" class="btn btn--ghost btn--sm">Rename folder…</button>
  </div>`;
}

async function refreshDeckFolders() {
  if (!deckFoldersEl) return;
  try {
    const body = await api("/decks/folders");
    _deckFolders = body.data || [];
    _deckUnfiledCount = body.unfiled_count || 0;
    _renderFolderTree();
  } catch (err) {
    deckFoldersEl.innerHTML = `<p class="state-error" role="alert">Folders unavailable: ${escapeHtml(err.message || "network")}</p>`;
  }
}

async function _deckMoveToFolder(deckId, folder) {
  // One deck moves with PATCH; a whole subtree moves with the rename
  // endpoint. Using rename for a single deck would drag its siblings.
  await _deckApiSend(`/decks/${encodeURIComponent(deckId)}`, "PATCH", { folder });
  await refreshDecksList();
}

async function _deckFolderRename(from, to) {
  // `to` empty/null unfiles the subtree — which is also how a folder is
  // deleted, since a folder has no row of its own.
  const out = await _deckApiSend("/decks/folders/rename", "POST", { from, to: to || null });
  if (_deckFolderSel && _deckFolderSel !== _FOLDER_UNFILED && to) {
    if (_deckFolderSel === from || _deckFolderSel.startsWith(from + "/")) {
      _deckFolderSel = to + _deckFolderSel.slice(from.length);
    }
  }
  await refreshDeckFolders();
  await refreshDecksList();
  return out;
}

if (deckFoldersEl) {
  deckFoldersEl.addEventListener("click", async (e) => {
    const toggle = e.target.closest("[data-toggle]");
    if (toggle) {
      const path = toggle.dataset.toggle;
      if (_deckFolderCollapsed.has(path)) _deckFolderCollapsed.delete(path);
      else _deckFolderCollapsed.add(path);
      _renderFolderTree();
      return;
    }
    const rename = e.target.closest("#deckFolderRenameBtn");
    if (rename) {
      const from = _deckFolderSel;
      if (!from) {
        window.alert("Pick a folder to rename first.");
        return;
      }
      const to = window.prompt(
        `Rename "${from}" to… (blank unfiles the whole subtree, which is how a folder is deleted)`,
        from,
      );
      if (to === null) return;
      try {
        const out = await _deckFolderRename(from, to.trim());
        _deckStatus(deckLibStatusEl, `Moved ${out.decks_moved} deck(s).`, false);
      } catch (err) {
        _deckStatus(deckLibStatusEl, err.message || "rename failed", true);
      }
      return;
    }
    const row = e.target.closest("button.deck-folder-row");
    if (!row) return;
    const kind = row.dataset.folderKind;
    _deckFolderSel = kind === "all" ? _FOLDER_ALL
      : kind === "unfiled" ? _FOLDER_UNFILED
      : row.dataset.folder;
    _renderFolderTree();
    await refreshDecksList();
  });
}

if (decksListEl) {
  decksListEl.addEventListener("click", (e) => {
    const btn = e.target.closest("button.deck-row[data-deck-id]");
    if (btn) _loadDeck(btn.dataset.deckId);
  });
}

if (deckPlanFormEl) {
  deckPlanFormEl.addEventListener("submit", (e) => { e.preventDefault(); loadCompletionPlan(); });
}
if (deckCompletionEl) {
  deckCompletionEl.addEventListener("change", () => { refreshDecksList(); });
}
if (deckSortEl) {
  deckSortEl.addEventListener("change", () => {
    if (deckSortDirEl) deckSortDirEl.value = _deckSortDefaultDir(deckSortEl.value);
    refreshDecksList();
  });
}
if (deckSortDirEl) deckSortDirEl.addEventListener("change", () => refreshDecksList());

if (deckSearchFormEl) {
  deckSearchFormEl.addEventListener("submit", (e) => {
    e.preventDefault();
    refreshDecksList();
  });
}
// The search field's native clear (✕) replaces the old Clear button.
if (deckQEl) {
  deckQEl.addEventListener("input", () => { if (deckQEl.value === "") refreshDecksList(); });
}

if (deckImportFormEl) {
  deckImportFormEl.addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = deckFileEl && deckFileEl.files && deckFileEl.files[0];
    const pasted = deckPasteTextEl ? deckPasteTextEl.value.trim() : "";
    if (!file && !pasted) {
      _deckStatus(deckImportStatusEl, "Pick an export file or paste a decklist first.", true);
      return;
    }
    _deckStatus(deckImportStatusEl, "Importing…", false);
    if (deckImportDiffEl) deckImportDiffEl.innerHTML = "";
    _deckImportDiffState = null;
    try {
      let created;
      if (file) {
        const fd = new FormData();
        fd.append("file", file);
        if (deckNameEl && deckNameEl.value.trim()) fd.append("name", deckNameEl.value.trim());
        for (const [k, v] of Object.entries(_deckImportMeta())) fd.append(k, v);
        const resp = await fetch("/api/decks/upload", { method: "POST", body: fd });
        created = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(created.details || resp.statusText);
      } else {
        const payload = { decklist: pasted, ..._deckImportMeta() };
        if (deckNameEl && deckNameEl.value.trim()) payload.name = deckNameEl.value.trim();
        created = await _deckApiSend("/decks", "POST", payload);
      }
      _deckStatus(deckImportStatusEl, `Imported "${created.name}" — ${created.total_cards} cards, ${created.unresolved_count} unresolved.`, false);
      await _showImportDiff(created.id, created);
      if (deckFileEl) deckFileEl.value = "";
      if (deckPasteTextEl) deckPasteTextEl.value = "";
      if (deckNameEl) deckNameEl.value = "";
      // Without this the next import silently inherits the previous deck's
      // links, description, branch and message. `reset` (D19.35) restores
      // a <select> to its default option rather than to a blank value
      // that no option carries.
      for (const f of DECK_META_INPUTS) {
        const el = f.el();
        if (el) el.value = f.reset || "";
      }
      await refreshDecksList();
      await _loadDeck(created.id);
    } catch (err) {
      _deckStatus(deckImportStatusEl, err.message || "import failed", true);
    }
  });
}

if (deckDetailEl) {
  // Card row -> the EXISTING card detail pipeline. `selectCard()` already
  // fetches /cards/:id and renders similar cards, oracle tags and predicted
  // labels; duplicating any of that here would rot independently.
  deckDetailEl.addEventListener("click", (e) => {
    const sort = e.target.closest("button.deck-sort[data-sort]");
    if (sort) {
      const key = sort.dataset.sort;
      if (_deckCardSort.key === key) _deckCardSort.dir *= -1;
      else _deckCardSort = { key, dir: key === "name" || key === "type" ? 1 : -1 };
      _renderDeckCardTable();
      return;
    }
    const row = e.target.closest("tr.deck-card-row[data-id]");
    if (!row) return;
    _setTab("search");
    selectCard(row.dataset.id);
  });

  deckDetailEl.addEventListener("click", async (e) => {
    if (e.target.closest("#deckMetaSave")) {
      await _saveDeckMeta();
      return;
    }
    // Evaluate HEAD against the edh-guide.md template — summary + tips.
    // Button-triggered (not part of the deck-open fetch) so opening a deck
    // stays a cheap read.
    if (e.target.closest("#deckEvalBtn")) {
      await _loadDeckEval(_deckSelectedId);
      return;
    }
    // Seed the Decklist wizard from this saved deck and jump to it.
    if (e.target.closest("#deckToWizardBtn")) {
      await _loadSavedDeck(_deckSelectedId);
      _setTab("build");
      return;
    }
    // D19.37: clears an existing head pin and resumes auto-follow.
    if (e.target.closest("#deckUnpinBtn")) {
      await _unpinDeckHead();
      return;
    }
    // D19.35: branch-head navigator. `data-nav-head` returns the card
    // table to the deck's actual pointer; `data-nav-branch` VIEWS a
    // branch's own latest version instead. Neither ever calls
    // `POST /decks/{id}/head` — viewing is not promoting.
    const navHead = e.target.closest("button[data-nav-head]");
    if (navHead) {
      _deckViewBranch = null;
      await _loadDeckCards(_deckSelectedId);
      _renderBranchNav();
      return;
    }
    const navBranch = e.target.closest("button[data-nav-branch]");
    if (navBranch && !navBranch.disabled) {
      _deckViewBranch = navBranch.dataset.navBranch;
      await _loadDeckCards(_deckSelectedId, _deckBranchHeadVersion(_deckViewBranch));
      _renderBranchNav();
      return;
    }
    // D19.35: timeline filter chip — narrows which rows of the SAME
    // already-fetched interleaved list are shown, no second request.
    const tlChip = e.target.closest("button.deck-branch-filter[data-timeline-branch]");
    if (tlChip) {
      _deckTimelineFilter = tlChip.dataset.timelineBranch || null;
      const section = $("#deckVersionsSection");
      if (section) {
        section.innerHTML = `${_renderTimelineFilterChips()}<div id="deckVersions">${_renderVersionRows()}</div>`;
      }
      return;
    }
    // D19.41: fork lands the user on the NEW deck — forking to keep
    // staring at the source would defeat the point of the control.
    if (e.target.closest("#deckForkBtn")) {
      try {
        const forked = await _deckApiSend(`/decks/${encodeURIComponent(_deckSelectedId)}/fork`, "POST", {});
        await refreshDecksList();
        await _loadDeck(forked.id);
      } catch (err) {
        _deckStatus(deckLibStatusEl, err.message || "failed", true);
      }
      return;
    }
    const restore = e.target.closest("button.deck-restore[data-version]");
    const delDeck = e.target.closest("#deckDeleteBtn");
    if (!restore && !delDeck) return;
    try {
      if (restore) {
        await _deckApiSend(`/decks/${encodeURIComponent(_deckSelectedId)}/versions/${restore.dataset.version}/restore`, "POST");
        await _loadDeck(_deckSelectedId);
      } else if (delDeck) {
        if (!window.confirm("Delete this deck and its entire version history? This cannot be undone.")) return;
        await _deckApiSend(`/decks/${encodeURIComponent(_deckSelectedId)}`, "DELETE");
        _deckSelectedId = null;
        _deckDetailData = null;
        deckDetailEl.innerHTML = `<p class="empty">Pick a deck to see its versions.</p>`;
        await refreshDecksList();
      }
    } catch (err) {
      _deckStatus(deckLibStatusEl, err.message || "failed", true);
    }
  });

  deckDetailEl.addEventListener("submit", async (e) => {
    if (e.target.closest("#deckVersionImportForm")) {
      e.preventDefault();
      await _importDeckVersion();
      return;
    }
    if (!e.target.closest("#deckDiffForm")) return;
    e.preventDefault();
    const a = $("#deckDiffA");
    const b = $("#deckDiffB");
    const out = $("#deckDiffResult");
    if (!a || !b || !out) return;
    out.innerHTML = `<p class="help">Diffing…</p>`;
    try {
      // Two fetches plus the EXISTING /decks/diff — no bespoke
      // version-diff endpoint exists, and none is needed (D19.5).
      const [va, vb] = await Promise.all([
        api(`/decks/${encodeURIComponent(_deckSelectedId)}/versions/${a.value}`),
        api(`/decks/${encodeURIComponent(_deckSelectedId)}/versions/${b.value}`),
      ]);
      _renderDeckDiff(await _deckApiSend("/decks/diff", "POST", { a: va.decklist, b: vb.decklist }));
    } catch (err) {
      out.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
    }
  });
}

if (tabDecksEl) {
  tabDecksEl.addEventListener("click", () => _setTab("decks"));
}

// D19.13 — boot LAST, after every module-level `let`/`const` above has
// initialised and every listener is wired.
//
// This used to run partway up the file, which was harmless only because
// `_setTab` did nothing but toggle panes. Now that it dispatches each
// tab's data loader, booting early means a `?tab=` deep-link calls into
// state that has not been declared yet — `let` is not hoisted, so
// `?tab=inventory` died on `Cannot access '_invPage' before
// initialization`. Hoisting each offending variable would be whack-a-mole;
// running boot last removes the whole class.

