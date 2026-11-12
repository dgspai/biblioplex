// Minimalist Biblioplex UI — single-screen client for the search /
// detail / similar endpoints. Same-origin via nginx /api/ proxy.

const $ = (sel) => document.querySelector(sel);
const resultsEl = $("#results");
const detailEl = $("#detail");
const bannerEl = $("#banner");
// Thin wrappers over helpers.js display formatters (kept as functions so call
// sites read plainly and the "every helper is defined" test pins can see them).
function formatInt(n) { return window.bibliohelpers.formatInt(n); }
function formatPct(n) { return window.bibliohelpers.formatPct(n); }
function formatUsd(n) { return window.bibliohelpers.formatUsd(n); }
function pagerRange(page, size, total) { return window.bibliohelpers.pagerRange(page, size, total); }
function plural(n, one, many) { return window.bibliohelpers.plural(n, one, many); }
function bracketLabel(b, total) { return window.bibliohelpers.bracketLabel(b, total); }
// Inline SVG icons (currentColor, so they follow the theme).
const _svg = (body) => `<svg class="icon" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true" focusable="false">${body}</svg>`;
const ICON_BOX = _svg('<path d="M2 5l6-3 6 3v6l-6 3-6-3z"/><path d="M2 5l6 3 6-3M8 8v6"/>');
const ICON_STAR = _svg('<path d="M8 1.8l1.9 4.1 4.4.5-3.3 3 .9 4.4L8 11.6l-3.9 2.2.9-4.4-3.3-3 4.4-.5z"/>');
const form = $("#searchForm");
const qInput = $("#q");
const orderSel = $("#order");
const dirSel = $("#dir");
const themeBtn = $("#themeToggle");
const refreshBtn = $("#refreshBtn");
const splitterEl = $("#splitter");
const cardsSplitEl = $("#cardsSplit");
const searchPaneEl = $("#searchPane");
const tabSearchEl = $("#tabSearch");
const tabCompareEl = $("#tabCompare");
const tabCombosEl = $("#tabCombos");
const comparePaneEl = $("#comparePane");
const combosPaneEl = $("#combosPane");
const combosFormEl = $("#combosForm");
const combosCardsEl = $("#combosCards");
const combosCiEl = $("#combosCi");
const combosCardChipsEl = $("#combosCardChips");
const combosCardListEl = $("#combosCardList");
const combosResultInputEl = $("#combosResultQ");
const combosQEl = $("#combosQ");
const combosSortEl = $("#combosSort");
const combosResetBtn = $("#combosResetBtn");
const combosResultsEl = $("#combosResults");
const combosDetailEl = $("#combosDetail");
const combosSplitEl = $("#combosSplit");
const combosSplitterEl = $("#combosSplitter");
const combosPagerEl = $("#combosPager");
const combosPageInfoEl = $("#combosPageInfo");
const combosPrevBtn = $("#combosPrev");
const combosNextBtn = $("#combosNext");
const tabInventoryEl = $("#tabInventory");
const inventoryPaneEl = $("#inventoryPane");
const invSyncFormEl = $("#inventorySyncForm");
const invDeckboxUrlEl = $("#invDeckboxUrl");
const invModeEl = $("#invMode");
const invSyncBtn = $("#invSyncBtn");
const invCsvFileEl = $("#invCsvFile");
const invCsvTextEl = $("#invCsvText");
const invSyncStatusEl = $("#invSyncStatus");
const invSummaryEl = $("#invSummary");
const invSearchFormEl = $("#inventorySearchForm");
const invQEl = $("#invQ");
const invClearBtn = $("#invClearBtn");
const invResultsEl = $("#invResults");
const invPagerEl = $("#invPager");
const invPageInfoEl = $("#invPageInfo");
const invPrevBtn = $("#invPrev");
const invNextBtn = $("#invNext");
const tabWishlistEl = $("#tabWishlist");
const wishlistPaneEl = $("#wishlistPane");
const wlSyncFormEl = $("#wishlistSyncForm");
const wlCsvFileEl = $("#wlCsvFile");
const wlCsvTextEl = $("#wlCsvText");
const wlModeEl = $("#wlMode");
const wlSyncBtn = $("#wlSyncBtn");
const wlSyncStatusEl = $("#wlSyncStatus");
const wlSummaryEl = $("#wlSummary");
const wlSearchFormEl = $("#wishlistSearchForm");
const wlQEl = $("#wlQ");
const wlClearBtn = $("#wlClearBtn");
const wlResultsEl = $("#wlResults");
const wlPagerEl = $("#wlPager");
const wlPageInfoEl = $("#wlPageInfo");
const wlPrevBtn = $("#wlPrev");
const wlNextBtn = $("#wlNext");
const invDetailEl = $("#invDetail");
const wlDetailEl = $("#wlDetail");
const invSplitEl = $("#invSplit");
const wlSplitEl = $("#wlSplit");
const invSplitterEl = $("#invSplitter");
const wlSplitterEl = $("#wlSplitter");
const tabDecksEl = $("#tabDecks");
const decksPaneEl = $("#decksPane");
const deckImportFormEl = $("#deckImportForm");
const deckFileEl = $("#deckFile");
const deckNameEl = $("#deckName");
const deckPasteTextEl = $("#deckPasteText");
const deckImportDeckboxEl = $("#deckImportDeckbox");
const deckImportMoxfieldEl = $("#deckImportMoxfield");
const deckImportArchidektEl = $("#deckImportArchidekt");
const deckImportNotesEl = $("#deckImportNotes");
const deckImportBranchEl = $("#deckImportBranch");
const deckImportMessageEl = $("#deckImportMessage");
const deckImportStatusEl = $("#deckImportStatus");
const deckImportDiffEl = $("#deckImportDiff");
const deckSearchFormEl = $("#deckSearchForm");
const deckQEl = $("#deckQ");
const deckLibStatusEl = $("#deckLibStatus");
const decksListEl = $("#decksList");
const deckFoldersEl = $("#deckFolders");
const deckSortEl = $("#deckSort");
const deckCompletionEl = $("#deckCompletion");
const deckPlanFormEl = $("#deckPlanForm");
const deckPlanBudgetEl = $("#deckPlanBudget");
const deckPlanMaxCardsEl = $("#deckPlanMaxCards");
const deckPlanStatusEl = $("#deckPlanStatus");
const deckPlanResultEl = $("#deckPlanResult");
const deckSortDirEl = $("#deckSortDir");
const deckDetailEl = $("#deckDetail");
const tabSettingsEl = $("#tabSettings");
const settingsPaneEl = $("#settingsPane");
const tabBuildEl = $("#tabBuild");
const buildPaneEl = $("#buildPane");
const uploadsListEl = $("#uploadsList");
const cardsPagerEl = $("#cardsPager");
const cardsPageInfoEl = $("#cardsPageInfo");
const cardsPrevBtn = $("#cardsPrev");
const cardsNextBtn = $("#cardsNext");
const SEARCH_PAGE_SIZE = 175;
const compareForm = $("#compareForm");
const compareAEl = $("#compareA");
const compareBEl = $("#compareB");
const compareResultEl = $("#compareResult");
const acDropdownEl = $("#acDropdown");
const historyDropdownEl = $("#historyDropdown");

let _currentTab = "search";

// Lean projection keeps the result-list payload small. The detail pane
// fetches the full card via /cards/:id when the user clicks a row.
// Base columns shared by every list row; D9.4 extends it on demand based
// on the active `order=` via `_sortField()` so we only pay for the extra
// JSON when the user actually sorts by edhrec/usd/released/power/etc.
const LIST_PROJECTION_BASE =
  "id,oracle_id,name,type_line,mana_cost,cmc,rarity,predicted_labels,power,toughness";

// Stable display order for predicted labels — matches LABELS in
// src/biblioplex/classify/rules.py. Used for both the slot-counts bar
// and the per-card chip strip so the visual order doesn't drift with
// whatever order the server happens to return.
const PRED_LABELS = [
  "ramp", "card_advantage", "targeted_disrupt", "mass_disrupt", "plan",
];
const SIMILAR_PROJECTION =
  "id,name,type_line,similarity,image_uris.small,image_uris.normal,"
  + "card_faces.0.image_uris.small,card_faces.0.image_uris.normal";
const SIMILAR_LIMIT = 12;
const COMBOS_INITIAL_LIMIT = 25;

// Injected at container start by `docker-entrypoint.d/10-render-app-js.sh`
// from the `DEFAULT_Q` env var via envsubst.
const DEFAULT_Q = "${DEFAULT_Q}";

let selectedId = null;
let currentPage = 1;

// Combos-tab state — hoisted here (rather than in the // -------- Combos tab
// -------- section further down) because _syncUrl → _combosUrlSuffix →
// _combosFormState touches _combosPage during the initial _setTab() the
// boot path fires before that section executes; `let` in TDZ would throw.
const COMBOS_PAGE_SIZE = 25;
let _combosPage = 1;
let _combosLoaded = false;
let _combosCards = [];
// D19.13: declared up here, not beside their panes. `_setTab` reads them
// and `_bootFromUrl()` runs partway down this file — `let` is not hoisted,
// so a `?tab=inventory` deep-link would die on a TDZ ReferenceError.
let _invLoaded = false;
let _wlLoaded = false;
let _combosCurrent = [];
let _combosPreselectId = null;

// Serialize the current form/state into `location` via replaceState so the URL
// bar mirrors what the user sees. `urlStateToSearch` (D9.7) drops keys that
// match the default, so a bare load renders as `/` not `/?q=&order=name&…`.
// The compare/combos tabs carry only `?tab=…`; their state is too large to
// round-trip via URL and isn't worth bookmarking.
function _syncUrl() {
  let search;
  if (_currentTab === "compare") {
    search = "?tab=compare";
  } else if (_currentTab === "combos") {
    search = "?tab=combos" + _combosUrlSuffix();
  } else if (_currentTab !== "search") {
    search = "?tab=" + _currentTab;
  } else {
    search = window.bibliohelpers.urlStateToSearch({
      q: qInput.value.trim(),
      order: orderSel.value,
      dir: dirSel.value,
      page: currentPage,
      id: selectedId,
    });
  }
  const target = location.pathname + search + location.hash;
  if (target !== location.pathname + location.search + location.hash) {
    history.replaceState(null, "", target);
  }
}

// Tab switch: show one pane, mark its tab and section active. URL is
// mirrored via _syncUrl so back/forward and reload remember the tab.
// D19.13 — the tab registry comes from the MARKUP, so adding a tab to
// index.html makes it deep-linkable without editing this file. The literal
// is only a fallback for a DOM that somehow has no tabs.
const _TAB_NAMES = (() => {
  const found = Array.from(document.querySelectorAll("[data-tab]"))
    .map((el) => el.getAttribute("data-tab"))
    .filter(Boolean);
  return found.length ? found : ["search"];
})();

// D19.13 — a tab's data load belongs to the TAB, not to its click handler.
// `refreshDecksList()` used to hang off the nav click only, so `_setTab`
// alone rendered an empty Decks pane; a deep-link (or any internal caller)
// got a blank tab. Dispatching here makes click and `?tab=` identical.
// Arrow bodies defer the lookup, so hoisting order does not matter.
const _TAB_LOADERS = {
  combos: () => { if (!_combosLoaded) runCombosSearch(); },
  inventory: () => { if (!_invLoaded) { _invLoaded = true; refreshInventoryView(); } },
  wishlist: () => { if (!_wlLoaded) { _wlLoaded = true; refreshWishlistView(); } },
  decks: () => { refreshDecksList(); _refreshDeckEvalIfShown(); },
  build: () => _renderBuild(),
  settings: () => refreshUploadsList(),
};

const _TAB_PANES = {
  search: searchPaneEl, compare: comparePaneEl, combos: combosPaneEl,
  inventory: inventoryPaneEl, wishlist: wishlistPaneEl, decks: decksPaneEl,
  build: buildPaneEl, settings: settingsPaneEl,
};

// A tab belongs to the section of its closest [data-section] (the sidebar
// item itself, or the .segments bar it sits in). The section lights its
// sidebar item and reveals its segments bar, if it has one.
function _setTab(name) {
  if (!_TAB_NAMES.includes(name)) name = "search";
  _currentTab = name;
  for (const [tab, pane] of Object.entries(_TAB_PANES)) if (pane) pane.hidden = tab !== name;
  for (const b of document.querySelectorAll("[data-tab]")) {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  }
  const section = document.querySelector(`[data-tab="${name}"]`).closest("[data-section]").dataset.section;
  for (const el of document.querySelectorAll(".nav-item[data-section]")) {
    el.classList.toggle("active", el.dataset.section === section);
  }
  for (const el of document.querySelectorAll(".segments")) el.hidden = el.dataset.section !== section;
  _syncUrl();
  const load = _TAB_LOADERS[name];
  if (load) load();
}

// Scryfall symbology — one shot at startup, build {symbol -> {svg, english}}
// map used to render `{W}`, `{2/U}`, `{T}` etc. as inline SVG <img>.
// ponytail: in-memory only; fine for a session. Add localStorage cache if
// the ~80-entry payload ever becomes a perf concern.
let symbolMap = {};
const symbolsReady = fetch("https://api.scryfall.com/symbology")
  .then((r) => r.ok ? r.json() : { data: [] })
  .then((j) => {
    for (const s of j.data || []) {
      if (s.svg_uri) symbolMap[s.symbol] = { svg: s.svg_uri, alt: s.english || s.symbol };
    }
  })
  .catch(() => { /* offline / blocked — fall back to plaintext */ });

function renderSymbols(text) {
  if (!text) return "";
  return String(text).replace(/(\{[^}]+\})|([^{]+)/g, (_, sym, plain) => {
    if (sym) {
      const hit = symbolMap[sym];
      if (hit) return `<img class="mana-sym" src="${hit.svg}" alt="${escapeHtml(hit.alt)}" title="${escapeHtml(hit.alt)}">`;
      return escapeHtml(sym);
    }
    return escapeHtml(plain);
  });
}

// tone "info" for status (refresh running, empty DB); default is error.
function showBanner(html, tone) {
  bannerEl.innerHTML = html;
  bannerEl.dataset.tone = tone || "error";
  bannerEl.hidden = false;
}
function hideBanner() {
  bannerEl.hidden = true;
  bannerEl.innerHTML = "";
}

// Shared empty-state block: title, one sentence, optional call to action that
// jumps to a tab (and focuses an element there). Wired by the delegated
// [data-goto-tab] handler below.
function emptyStateHtml(title, text, cta) {
  const btn = cta
    ? `<button type="button" class="btn btn--primary" data-goto-tab="${escapeHtml(cta.tab)}"`
      + `${cta.focus ? ` data-goto-focus="${escapeHtml(cta.focus)}"` : ""}>${escapeHtml(cta.label)}</button>`
    : "";
  return `<div class="state-empty"><h3>${escapeHtml(title)}</h3><p>${escapeHtml(text)}</p>${btn}</div>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-goto-tab]");
  if (!b) return;
  _setTab(b.dataset.gotoTab);
  const el = b.dataset.gotoFocus && document.querySelector(b.dataset.gotoFocus);
  if (el) { el.scrollIntoView({ block: "center" }); el.focus(); }
});
// Placeholder text in the card detail pane; also what a failed search resets it to.
const DETAIL_PLACEHOLDER = `<p class="empty">Select a card to see details.</p>`;

async function api(path) {
  const resp = await fetch("/api" + path);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(body.details || resp.statusText);
    err.status = resp.status;
    err.body = body;
    throw err;
  }
  return body;
}

// Mirror of `_AUTO_DIR` in `src/biblioplex/routers/cards.py`. Lets us
// resolve `dir=auto` on the client so the sort-info arrow on each row
// reflects the same direction the backend actually applied.
const _AUTO_DIR = {
  name: "asc", cmc: "asc", power: "asc", toughness: "asc",
  color: "asc", rarity: "asc", edhrec: "asc",
  released: "desc", usd: "desc", relevance: "desc",
};

// Map `order=` → extra projection field(s) needed to render the sort-info
// line. `null` means "already in the base projection — skip the line".
const _sortField = (order) => ({
  name: null,
  cmc: null,
  rarity: null,
  power: "power,toughness",
  toughness: "power,toughness",
  released: "released_at",
  color: "colors",
  edhrec: "edhrec_rank",
  usd: "prices.usd",
}[order] ?? null);

function _renderSortInfo(card, order, dir) {
  // `relevance` has no per-row badge — the card name itself is the signal.
  if (order === "name" || order === "cmc" || order === "rarity" || order === "relevance") return "";
  const arrow = dir === "desc" ? "↓" : "↑";
  if (order === "edhrec") {
    const rank = card.edhrec_rank;
    if (rank == null) return "";
    return `<div class="sort-info">${arrow} #${escapeHtml(String(rank))}</div>`;
  }
  if (order === "usd") {
    const usd = card.prices?.usd;
    if (!usd) return "";
    return `<div class="sort-info">${arrow} ${escapeHtml(formatUsd(usd))}</div>`;
  }
  if (order === "released") {
    const r = card.released_at;
    if (!r) return "";
    return `<div class="sort-info">${arrow} ${escapeHtml(String(r))}</div>`;
  }
  if (order === "power" || order === "toughness") {
    const p = card.power, t = card.toughness;
    if (p == null && t == null) return "";
    return `<div class="sort-info">${arrow} P/T ${escapeHtml(String(p ?? "?"))}/${escapeHtml(String(t ?? "?"))}</div>`;
  }
  if (order === "color") {
    const colors = card.colors;
    if (!colors || colors.length === 0) return "";
    return `<div class="sort-info">${arrow} ${escapeHtml(colors.join(""))}</div>`;
  }
  return "";
}

// Footer pager sits below the results/detail split so it stays put while
// #results scrolls. Hidden entirely when the current query returns nothing
// past page 1's window — a tiny quiet state beats a lonely disabled bar.
function _updateCardsPager(page, hasMore, total) {
  if (!cardsPagerEl) return;
  // One page: the "N results" line above the list already says it all.
  if (total <= SEARCH_PAGE_SIZE && page <= 1) { cardsPagerEl.hidden = true; return; }
  cardsPagerEl.hidden = false;
  cardsPrevBtn.disabled = page <= 1;
  cardsNextBtn.disabled = !hasMore;
  cardsPageInfoEl.textContent = pagerRange(page, SEARCH_PAGE_SIZE, total);
}

// Tally per-label counts across the current page. Each card can be in
// multiple labels (multilabel classifier), so the sum can exceed the
// page size — that's accurate to the slot-count semantics, not a bug.
function _renderSlotCounts(list) {
  if (!list.data || list.data.length === 0) return "";
  const counts = Object.fromEntries(PRED_LABELS.map((l) => [l, 0]));
  let scored = 0;
  for (const c of list.data) {
    const labs = c.predicted_labels;
    if (!Array.isArray(labs)) continue;
    scored++;
    for (const lab of labs) if (counts[lab] != null) counts[lab]++;
  }
  if (scored === 0) return "";
  const cells = PRED_LABELS.map((lab) => {
    const pred = `pred:${lab}`;
    return `<button type="button" class="slot pred-${lab}"`
      + ` data-pred="${escapeHtml(pred)}"`
      + ` title="filter by ${escapeHtml(pred)}">`
      + `<span class="slot-label">${escapeHtml(lab.replace(/_/g, " "))}</span>`
      + ` <span class="slot-count">${counts[lab]}</span>`
      + `</button>`;
  }).join("");
  return `<div class="slot-counts" aria-label="Predicted label counts on this page">${cells}</div>`;
}

function renderResults(list, order, dir, page) {
  _dismissPreview();  // the row under the cursor is about to be replaced
  if (!list.data || list.data.length === 0) {
    const copy = window.bibliohelpers.emptyResultsCopy(_healthKind, qInput.value);
    resultsEl.innerHTML = emptyStateHtml(copy.title, copy.body, copy.cta);
    _updateCardsPager(page, false, 0);
    detailEl.innerHTML = DETAIL_PLACEHOLDER;
    return;
  }
  // A stale error from an earlier failed search or random click must not
  // linger beside fresh results.
  if (detailEl.querySelector(".state-error")) detailEl.innerHTML = DETAIL_PLACEHOLDER;
  const items = list.data.map((c) => {
    const meta = [];
    if (c.type_line) meta.push(`<span>${escapeHtml(c.type_line)}</span>`);
    if (c.mana_cost) meta.push(`<span class="cost">${renderSymbols(c.mana_cost)}</span>`);
    else if (c.cmc != null) meta.push(`<span>cmc ${c.cmc}</span>`);
    if (c.power != null || c.toughness != null) {
      meta.push(`<span class="pt">${escapeHtml(c.power ?? "")}/${escapeHtml(c.toughness ?? "")}</span>`);
    }
    if (c.rarity) meta.push(`<span>${escapeHtml(c.rarity)}</span>`);
    const sel = c.id === selectedId ? " selected" : "";
    const sortInfo = _renderSortInfo(c, order, dir);
    return `<li data-id="${c.id}" data-oracle="${escapeHtml(c.oracle_id || "")}" class="${sel.trim()}" tabindex="0">
      <div class="name">${escapeHtml(c.name || "?")}${_ownedBadge(c.oracle_id)}</div>
      <div class="meta">${meta.join("")}</div>
      ${sortInfo}
    </li>`;
  }).join("");
  const total = list.total_cards ?? list.data.length;
  const slots = _renderSlotCounts(list);
  resultsEl.innerHTML = `
    <p class="meta">${plural(total, "result")}</p>
    ${slots}
    <ul class="cards">${items}</ul>`;
  _updateCardsPager(page, !!list.has_more, total);
}

// One badge per format. `status` is the raw Scryfall value
// (`legal`/`banned`/`restricted`/`not_legal`) or `unknown` when the
// legalities map is present but the format key is missing.
const _renderLegality = (fmt, status) => {
  const label = status.replace(/_/g, " ");
  return `<li class="legality legality-${status}"><span class="fmt">${fmt}</span><span class="status">${label}</span></li>`;
};

// One pill button. The DSL predicate rides in `data-pred`; the click
// handler on `detailEl` reads it back and appends to the search box.
// Both attributes pass through `escapeHtml` for safety — the entity-
// encoded `kw:"Flying"` round-trips cleanly via `dataset.pred`.
const _chip = (pred, label) =>
  `<button type="button" class="chip" data-pred="${escapeHtml(pred)}">${escapeHtml(label)}</button>`;

// Combined color string for `c:`. Empty → "C" (colorless). Otherwise
// sort by WUBRG index so order is canonical regardless of input order.
const _COLOR_ORDER = ["W", "U", "B", "R", "G"];
const _combinedColors = (colors) => {
  if (!Array.isArray(colors)) return null;
  if (colors.length === 0) return "C";
  return [...colors]
    .filter((c) => _COLOR_ORDER.includes(c))
    .sort((a, b) => _COLOR_ORDER.indexOf(a) - _COLOR_ORDER.indexOf(b))
    .join("");
};

function _renderKeywordsSection(card) {
  const kws = card.keywords;
  if (!Array.isArray(kws) || kws.length === 0) return "";
  const chips = kws.map((kw) => _chip(`kw:"${kw}"`, kw)).join("");
  return `<section class="chips-section" aria-label="Keywords">
    <h3>Keywords</h3>
    <div class="chips">${chips}</div>
  </section>`;
}

// Step 5 — rules-layer predicted labels. Chip per label, click filters
// the result list by `pred:<label>` (parallel to `otag:`/`kw:`). Score
// (when present) rides as a tooltip so the chip stays compact.
function _renderPredictedLabelsSection(card) {
  const labels = Array.isArray(card.predicted_labels) ? card.predicted_labels : [];
  const scores = card.label_scores || {};
  const ordered = PRED_LABELS.filter((l) => labels.includes(l));
  const chips = ordered.length
    ? ordered.map((lab) => {
        const score = scores[lab];
        const title = typeof score === "number"
          ? `${lab.replace(/_/g, " ")} — score ${score.toFixed(2)}`
          : lab.replace(/_/g, " ");
        return `<button type="button" class="chip chip-pred pred-${escapeHtml(lab)}"`
          + ` data-pred="${escapeHtml(`pred:${lab}`)}"`
          + ` data-label="${escapeHtml(lab)}"`
          + ` title="${escapeHtml(title)}">`
          + escapeHtml(lab.replace(/_/g, " "))
          + `</button>`;
      }).join("")
    : `<span class="help">(no role labels)</span>`;
  // A manual correction (labels_overridden) beats the rules classifier; the
  // editor lets you fix a miscategorised card and it sticks across refreshes.
  const edited = card.labels_overridden
    ? ` <span class="label-edited" title="Manually corrected — overrides the classifier">✎ edited</span>`
    : "";
  const boxes = PRED_LABELS.map((lab) =>
    `<label class="label-opt"><input type="checkbox" value="${escapeHtml(lab)}"`
    + `${labels.includes(lab) ? " checked" : ""}> ${escapeHtml(lab.replace(/_/g, " "))}</label>`
  ).join("");
  return `<section class="chips-section chips-predicted" aria-label="Predicted labels">
    <h3>Predicted labels${edited}
      <button type="button" class="label-edit-btn" data-label-edit title="Correct these labels">edit</button>
    </h3>
    <div class="chips">${chips}</div>
    <div class="label-editor" hidden>
      <div class="label-opts">${boxes}</div>
      <div class="label-editor-actions">
        <button type="button" data-label-save>Save</button>
        <button type="button" data-label-reset title="Discard the override, use the classifier's verdict">Reset to auto</button>
        <button type="button" class="btn btn--ghost btn--sm" data-label-cancel>Cancel</button>
      </div>
    </div>
  </section>`;
}

// Persist a manual label correction, then repaint the detail from the
// response so predicted_labels + the "edited" marker reflect the new state.
// `labels === null` clears the override (revert to the rules verdict).
async function _saveCardLabels(pane, card, labels) {
  const path = `/api/cards/${card.id}/labels`;
  try {
    const resp = labels === null
      ? await fetch(path, { method: "DELETE" })
      : await fetch(path, {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ labels }),
        });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).details || resp.status);
    const updated = await resp.json();
    // The GET carries an epoch ETag that a label edit doesn't bump, so repaint
    // from the write response rather than re-fetching (which could 304-stale).
    renderDetail(updated, 0, pane);
  } catch (err) {
    showBanner(`Label update failed: ${err.message || "network"}`);
  }
}

function _renderOracleTagsSection(tags) {
  if (!Array.isArray(tags) || tags.length === 0) return "";
  const chips = tags.map((t) => _chip(`otag:${t}`, t)).join("");
  return `<section class="chips-section" aria-label="Oracle tags">
    <h3>Oracle tags</h3>
    <div class="chips">${chips}</div>
  </section>`;
}

function _renderTagsSection(card) {
  const chips = [];
  for (const tok of window.bibliohelpers.splitTypeLine(card.type_line || "")) {
    chips.push(_chip(`t:${tok}`, tok));
  }
  if (card.rarity) chips.push(_chip(`r:${card.rarity}`, card.rarity));
  const combined = _combinedColors(card.colors);
  if (combined != null) chips.push(_chip(`c:${combined}`, combined));
  if (card.legalities?.commander === "legal") chips.push(_chip("f:commander", "commander"));
  if (card.legalities?.modern === "legal") chips.push(_chip("f:modern", "modern"));
  if (chips.length === 0) return "";
  return `<section class="chips-section" aria-label="Tags">
    <h3>Tags</h3>
    <div class="chips">${chips.join("")}</div>
  </section>`;
}

// One <li> per related part (token, meld piece, combo piece, …). The chip
// rides a `data-related-id` so the delegated click handler on detailEl can
// route into `selectCard()`; uses the same `.chip` styling as the DSL chips
// for visual continuity. Component label (Scryfall: `token`, `meld_part`,
// `meld_result`, `combo_piece`) is rendered muted-and-small before the name.
const _relatedChip = (p) =>
  `<button type="button" class="chip related-chip" data-related-id="${escapeHtml(p.id)}">`
  + `<span class="component">${escapeHtml((p.component || "").replace(/_/g, " "))}</span>`
  + escapeHtml(p.name || "?")
  + `</button>`;

function _renderRelatedSection(card) {
  const parts = window.bibliohelpers.relatedParts(card);
  if (parts.length === 0) return "";
  const chips = parts.map(_relatedChip).join("");
  // Chips render sync from `card.all_parts` so the paint is instant, but the
  // ids in that JSON are Scryfall printing ids that may not match our
  // collapsed rows. `loadRelatedInto` (D14.8) fetches /cards/:id/related and
  // swaps in canonical `data-related-id` values so clicks land.
  return `<section class="chips-section chips-related" aria-label="Related cards">
    <h3>Related</h3>
    <div class="chips">${chips}</div>
  </section>`;
}

// Stack every distinct face image. Single-face cards render one <img>;
// transform/MDFC/meld render front + back stacked (D9.10). When the card has
// multiple faces (D9.11), each <figure> is a real <button> so click/Enter/Space
// swap the active face — the detail body (mana, oracle, P/T, type-line) then
// re-renders from `mergeFaceData(card, idx)`. The active face gets `.active`
// for a visible cue (accent ring + caption weight).
function _renderFaceImages(card, faceIdx) {
  const faces = window.bibliohelpers.cardFaceImages(card);
  if (faces.length === 0) return "";
  if (faces.length === 1) {
    const f = faces[0];
    return `<img class="art" src="${f.url}" alt="${escapeHtml(f.name)} card art">`;
  }
  return `<div class="faces">${faces.map((f, i) => {
    const active = i === faceIdx ? " active" : "";
    return `<button type="button" class="face${active}" data-face-idx="${i}" aria-pressed="${i === faceIdx}">
      <img class="art" src="${f.url}" alt="${escapeHtml(f.name)} card art">
      <figcaption>${escapeHtml(f.name)}</figcaption>
    </button>`;
  }).join("")}</div>`;
}

// Cache the last-rendered card so the face-click handler can re-render
// without re-fetching `/cards/:id`. Cleared in selectCard() whenever a new
// card is loaded.
let _detailCard = null;
let _detailFaceIdx = 0;

function renderDetail(card, faceIdx, target) {
  const pane = target || _detailTarget || detailEl;
  _detailTarget = pane;
  _detailCard = card;
  _detailFaceIdx = Number.isFinite(faceIdx) && faceIdx >= 0 ? faceIdx : 0;
  const view = window.bibliohelpers.mergeFaceData(card, _detailFaceIdx);
  const sim = card.similarity != null
    ? `<dt>similarity</dt><dd>${card.similarity.toFixed(3)}</dd>` : "";
  const setLine = card.set_name
    ? `<dt>set</dt><dd data-field="set">${escapeHtml(card.set_name)}${card.collector_number ? ` #${escapeHtml(card.collector_number)}` : ""}</dd>`
    : "";
  const legalities = card.legalities
    ? `<section class="legalities" aria-label="Format legality">
        <h3>Legality</h3>
        <ul class="legality-list">
          ${["commander", "modern"].map((fmt) => _renderLegality(fmt, card.legalities[fmt] || "unknown")).join("")}
        </ul>
      </section>`
    : "";
  const scryUrl = card.scryfall_uri
    || (card.set && card.collector_number
        ? `https://scryfall.com/card/${encodeURIComponent(card.set)}/${encodeURIComponent(card.collector_number)}`
        : null);
  const scryLink = scryUrl
    ? `<a class="ext-link" href="${scryUrl}" target="_blank" rel="noopener" title="Open on Scryfall">Scryfall</a>`
    : "";
  pane.innerHTML = `
    <div class="detail-header">
      <h2>${escapeHtml(view.name || "?")}${_ownedBadge(card.oracle_id)}</h2>
      <div class="detail-actions">
        <button type="button" class="copy-id" data-copy="${escapeHtml(card.id || "")}" title="Copy card id">copy id</button>
        ${scryLink}
      </div>
    </div>
    <p class="typeline">${escapeHtml(view.type_line || "")}</p>
    ${_renderFaceImages(card, _detailFaceIdx)}
    <section class="printings" aria-label="Printings" hidden>
      <h3>Printings</h3>
      <div class="printings-strip"></div>
    </section>
    <p class="oracle">${renderSymbols(view.oracle_text || "")}</p>
    <dl>
      ${view.mana_cost ? `<dt>mana</dt><dd>${renderSymbols(view.mana_cost)}</dd>` : ""}
      ${view.power ? `<dt>p/t</dt><dd>${escapeHtml(view.power)}/${escapeHtml(view.toughness || "")}</dd>` : ""}
      ${view.loyalty ? `<dt>loy</dt><dd>${escapeHtml(view.loyalty)}</dd>` : ""}
      ${card.rarity ? `<dt>rarity</dt><dd>${escapeHtml(card.rarity)}</dd>` : ""}
      ${card.released_at ? `<dt>released</dt><dd>${escapeHtml(card.released_at)}</dd>` : ""}
      ${setLine}
      ${card.artist ? `<dt>artist</dt><dd>${escapeHtml(card.artist)}</dd>` : ""}
      ${card.edhrec_rank ? `<dt>edhrec</dt><dd>#${escapeHtml(String(card.edhrec_rank))}</dd>` : ""}
      ${card.prices?.usd ? `<dt>usd</dt><dd data-field="usd">${escapeHtml(formatUsd(card.prices.usd))}</dd>` : ""}
      ${sim}
    </dl>
    ${_renderKeywordsSection(card)}
    ${_renderTagsSection(card)}
    ${_renderPredictedLabelsSection(card)}
    ${_renderRelatedSection(card)}
    <section class="oracle-tags" aria-label="Oracle tags">
      <div class="oracle-tags-body"></div>
    </section>
    ${legalities}
    <section class="similar" aria-label="Similar cards">
      <h3>Similar cards</h3>
      <div class="similar-body"><p class="loading">Loading similar…</p></div>
    </section>
    <section class="combos" aria-label="Commander Spellbook combos">
      <h3>Combos</h3>
      <div class="combos-body"><p class="loading">Loading combos…</p></div>
    </section>`;
  loadSimilarInto(card.id, pane.querySelector(".similar-body"));
  loadOracleTagsInto(card.id, pane.querySelector(".oracle-tags-body"));
  loadExplainInto(card.id, pane.querySelector(".chips-predicted"));
  loadCombosInto(card.id, pane.querySelector(".combos-body"));
  loadRelatedInto(card.id, pane.querySelector(".chips-related .chips"));
  loadPrintingsInto(card.id, pane);
}

function _printingThumb(p, selected) {
  const o = p.owned;
  const label = `${p.set_name || p.set} #${p.collector_number}`
    + (o ? `, ${o.quantity} owned${o.foil_quantity ? ` (${o.foil_quantity} foil)` : ""}` : "");
  return `<button type="button" class="printing" aria-pressed="${selected}" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}">
    <img src="${escapeHtml(p.image_uris.small || "")}" alt="" loading="lazy">
    <span>${escapeHtml((p.set || "").toUpperCase())}${o ? ` <b>×${o.quantity}${o.foil_quantity ? "★" : ""}</b>` : ""}</span>
  </button>`;
}

// ponytail: swaps the front image only; a DFC's back face keeps the canonical printing's art.
async function loadPrintingsInto(id, pane) {
  const section = pane.querySelector(".printings");
  try {
    const list = (await api(`/cards/${id}/printings`)).data || [];
    if (!section.isConnected || list.length < 2) return;
    const strip = section.querySelector(".printings-strip");
    strip.innerHTML = list.map((p) => _printingThumb(p, false)).join("");
    const show = (btn) => {
      const p = list[[...strip.children].indexOf(btn)];
      pane.dataset.printing = p.id;  // survives a face-flip re-render
      for (const b of strip.children) b.setAttribute("aria-pressed", String(b === btn));
      const art = pane.querySelector(".art");
      if (art && p.image_uris.normal) art.src = p.image_uris.normal;
      const set = pane.querySelector('[data-field="set"]');
      if (set) set.textContent = `${p.set_name || p.set} #${p.collector_number}`;
      const usd = pane.querySelector('[data-field="usd"]');
      if (usd) usd.textContent = p.prices.usd ? formatUsd(p.prices.usd) : "—";
    };
    strip.addEventListener("click", (e) => {
      const btn = e.target.closest(".printing");
      if (btn) show(btn);
    });
    const want = list.findIndex((p) => p.id === (pane.dataset.printing || id));
    if (want >= 0 && pane.dataset.printing) show(strip.children[want]);
    else if (want >= 0) strip.children[want].setAttribute("aria-pressed", "true");
    section.hidden = false;
  } catch (_err) {
    // 404 stale id / 5xx — the strip stays hidden.
  }
}

// D14.8 — fetch /cards/:id/related and swap the sync-rendered chips (which
// carry Scryfall printing ids from raw.all_parts) with canonical ids from
// our DB. Chips whose name doesn't resolve get `data-related-id` cleared
// and an `disabled` attribute so clicks don't fire on unhydrated entries.
async function loadRelatedInto(id, container) {
  if (!container) return;
  try {
    const list = await api(`/cards/${id}/related`);
    if (!container.isConnected) return;
    const items = Array.isArray(list.data) ? list.data : [];
    if (items.length === 0) return;  // sync-rendered chips stay
    const chips = items.map((p) => {
      const canonicalId = p.card?.id || "";
      const attrs = canonicalId
        ? `data-related-id="${escapeHtml(canonicalId)}"`
        : `disabled title="Not in local corpus"`;
      return `<button type="button" class="chip related-chip" ${attrs}>`
        + `<span class="component">${escapeHtml((p.component || "").replace(/_/g, " "))}</span>`
        + escapeHtml(p.name || "?")
        + `</button>`;
    }).join("");
    container.innerHTML = chips;
  } catch (_err) {
    // 404 stale id / 5xx — keep the sync-rendered chips as fallback.
  }
}

// D15.3 — fetch /explain and hydrate predicted-label chip tooltips with
// the rule slugs that fired. Best-effort: any error leaves the base
// score-only tooltip in place.
async function loadExplainInto(id, container) {
  if (!container) return;
  try {
    const payload = await api(`/cards/${id}/explain`);
    if (!container.isConnected) return;
    const labels = payload.labels || {};
    for (const chip of container.querySelectorAll("[data-label]")) {
      const lab = chip.dataset.label;
      const entry = labels[lab];
      if (!entry || !Array.isArray(entry.rules) || entry.rules.length === 0) continue;
      const rules = entry.rules.map((r) => r.id).join(", ");
      const base = chip.getAttribute("title") || lab.replace(/_/g, " ");
      chip.setAttribute("title", `${base} — ${rules}`);
    }
  } catch (_err) {
    // 404 (stale id) or 5xx — silent; base tooltip stays.
  }
}

async function loadOracleTagsInto(id, container) {
  try {
    const list = await api(`/cards/${id}/tags`);
    if (!container.isConnected) return;
    container.innerHTML = _renderOracleTagsSection(list.data);
  } catch (err) {
    if (!container.isConnected) return;
    // 404 = stale id; silently drop. Other errors get a one-line note.
    if (err.status !== 404) {
      container.innerHTML = `<p class="state-error" role="alert">Tags error: ${escapeHtml(err.message)}</p>`;
    }
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function search(q, order, dir, page) {
  hideBanner();
  resultsEl.innerHTML = `<p class="loading">Searching…</p>`;
  const effOrder = order || "name";
  const effDir = dir || "auto";
  const effPage = Number.isFinite(page) && page >= 1 ? page : 1;
  currentPage = effPage;
  const extra = _sortField(effOrder);
  const projection = extra ? `${LIST_PROJECTION_BASE},${extra}` : LIST_PROJECTION_BASE;
  const resolvedDir = effDir === "auto" ? (_AUTO_DIR[effOrder] || "asc") : effDir;
  const params = new URLSearchParams({
    q, order: effOrder, dir: effDir, page: String(effPage), projection,
  });
  _syncUrl();
  try {
    const [list] = await Promise.all([api("/cards/search?" + params), symbolsReady]);
    renderResults(list, effOrder, resolvedDir, effPage);
    // D13.8 — only push to history on a successful search, on page 1 (so
    // pagination clicks don't clutter). Cap at 20 entries, dedup case-
    // insensitively.
    if (effPage === 1) _pushHistory(q);
  } catch (err) {
    if (err.status === 404 && err.body?.code === "not_found") {
      await pollOracleTagsStatus();  // refreshes _healthKind for the empty-state copy
      renderResults({ data: [] }, effOrder, resolvedDir, effPage);
      return;
    }
    // D17.6 wire — on 422, preflight /cards/search/validate to surface
    // `did_you_mean` chips. Click a chip to swap the offending key and
    // re-run. Fire-and-forget: any /validate failure falls through to
    // the plain-text error below.
    if (err.status === 422) {
      _renderSearchError(err, q, effOrder, effDir, effPage);
      return;
    }
    resultsEl.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message)}</p>`;
    _updateCardsPager(effPage, false, 0);
    detailEl.innerHTML = DETAIL_PLACEHOLDER;
  }
}

// D17.6 — render a 422 with an inline "did you mean" chip strip. Fetches
// the preflight (POST /cards/search/validate) with the same q so agents
// get the typo-corrected key list, then wires each chip to replace the
// offending token in the search input and rerun.
async function _renderSearchError(err, q, order, dir, page) {
  const details = err.body?.details || err.message || "invalid query";
  let hints = err.body?.did_you_mean;
  // /cards/search doesn't surface did_you_mean on its own — call validate.
  if (!Array.isArray(hints)) {
    try {
      const resp = await fetch("/api/cards/search/validate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ q }),
      });
      const body = await resp.json().catch(() => ({}));
      hints = Array.isArray(body?.did_you_mean) ? body.did_you_mean : null;
    } catch { hints = null; }
  }
  const badKey = _extractOffendingKey(details);
  const chips = (hints || []).map((h) => {
    const dsl = escapeHtml(h);
    return `<button type="button" class="chip search-hint-chip" `
      + `data-key="${escapeHtml(badKey || "")}" data-hint="${dsl}">${dsl}:</button>`;
  }).join("");
  const chipStrip = chips
    ? `<p class="empty">Did you mean: <span class="chips">${chips}</span></p>`
    : "";
  resultsEl.innerHTML =
    `<p class="state-error" role="alert">Error: ${escapeHtml(details)}</p>${chipStrip}`;
  _updateCardsPager(page, false, 0);
  detailEl.innerHTML = DETAIL_PLACEHOLDER;
  // Wire chip clicks: swap the offending key in the query and rerun.
  resultsEl.querySelectorAll(".search-hint-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.dataset.key;
      const hint = btn.dataset.hint;
      const next = _swapKey(q, key, hint);
      qInput.value = next;
      search(next, order, dir, 1);
    });
  });
}

// Pull the offending key out of the compiler's error message. Shapes:
//   "unsupported operator: <key>"
//   "unknown predicted label: <label>"
function _extractOffendingKey(details) {
  if (!details) return "";
  const m = details.match(/(?:unsupported operator|unknown predicted label):\s*([^\s:=<>!]+)/);
  return m ? m[1] : "";
}

// Replace `<badKey>` with `<hint>` in the q string, preserving the rest.
// Case-insensitive; word-boundary anchored so `t` in a name doesn't get
// hit. Falls back to appending `hint:` when no match found.
function _swapKey(q, badKey, hint) {
  if (!badKey) return `${q} ${hint}:`;
  const escaped = badKey.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&");
  const re = new RegExp(`\\b${escaped}(?=[:=<>!])`, "i");
  return re.test(q) ? q.replace(re, hint) : `${q} ${hint}:`;
}

// D13.8 — search history dropdown. Stored in localStorage keyed by
// "biblioplex.history", capped at 20. Rendered on focus when the query
// input is empty; each entry is a click-to-rerun row. Delete via
// Backspace on a keyboard-focused/hovered entry (no confirm — cheap
// operation, cap is small).
const _HISTORY_KEY = "biblioplex.history";
const _HISTORY_MAX = 20;

function _loadHistory() {
  try {
    const raw = localStorage.getItem(_HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch { return []; }
}

function _saveHistory(list) {
  try { localStorage.setItem(_HISTORY_KEY, JSON.stringify(list)); }
  catch { /* localStorage disabled — no-op */ }
}

function _pushHistory(q) {
  const next = window.bibliohelpers.historyPush(_loadHistory(), q, _HISTORY_MAX);
  _saveHistory(next);
}

function _deleteHistory(entry) {
  const list = _loadHistory();
  const next = list.filter((x) => x !== entry);
  _saveHistory(next);
  return next;
}

let _historyActive = -1;

function hideHistory() {
  historyDropdownEl.hidden = true;
  historyDropdownEl.innerHTML = "";
  _historyActive = -1;
}

function _renderHistory() {
  const list = _loadHistory();
  if (list.length === 0) { hideHistory(); return; }
  historyDropdownEl.innerHTML = list.map((entry, i) => {
    const active = i === _historyActive ? " active" : "";
    const id = `histOpt${i}`;
    return `<li id="${id}" role="option" class="ac-item${active}" data-history="${escapeHtml(entry)}" aria-selected="${i === _historyActive}">${escapeHtml(entry)}</li>`;
  }).join("");
  historyDropdownEl.hidden = false;
}

function showHistory() {
  // Only surface when the query field is empty and the autocomplete
  // panel isn't already active — avoids two popovers stacking.
  if (qInput.value.trim() !== "") return;
  if (!acDropdownEl.hidden) return;
  _renderHistory();
}

qInput.addEventListener("focus", showHistory);

historyDropdownEl.addEventListener("mousedown", (e) => {
  const li = e.target.closest("li[data-history]");
  if (!li) return;
  e.preventDefault();
  qInput.value = li.dataset.history;
  hideHistory();
  form.dispatchEvent(new SubmitEvent("submit", { cancelable: true }));
});

qInput.addEventListener("keydown", (e) => {
  if (historyDropdownEl.hidden) return;
  const list = _loadHistory();
  if (e.key === "ArrowDown") {
    e.preventDefault();
    _historyActive = (_historyActive + 1) % list.length;
    _renderHistory();
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    _historyActive = (_historyActive - 1 + list.length) % list.length;
    _renderHistory();
  } else if (e.key === "Enter" && _historyActive >= 0) {
    e.preventDefault();
    qInput.value = list[_historyActive];
    hideHistory();
    form.dispatchEvent(new SubmitEvent("submit", { cancelable: true }));
  } else if (e.key === "Escape") {
    e.preventDefault();  // close the panel only; don't let Esc clear the query
    e.stopPropagation();
    hideHistory();
  } else if (e.key === "Backspace" && _historyActive >= 0) {
    // Delete the highlighted entry from history in place; keep the panel open.
    e.preventDefault();
    const next = _deleteHistory(list[_historyActive]);
    _historyActive = Math.min(_historyActive, next.length - 1);
    _renderHistory();
  }
});

qInput.addEventListener("blur", () => {
  // Same 150ms defer as autocomplete so a click on a history entry lands
  // before the panel hides.
  setTimeout(hideHistory, 150);
});

async function selectCard(id, target, printingId) {
  const pane = target || detailEl;
  _detailTarget = pane;
  pane.dataset.printing = printingId || "";
  // Only the cards tab drives `selectedId` / the URL — the inventory and
  // wishlist panes are their own browsing context and must not rewrite the
  // card-search deep link.
  if (pane === detailEl) {
    selectedId = id;
    _syncUrl();
  }
  _detailCard = null;
  _detailFaceIdx = 0;
  // Scope the selected-row highlight to the list that owns this pane.
  const list = pane.closest(".cards-split") || document;
  list.querySelectorAll("ul.cards li").forEach((li) => {
    li.classList.toggle("selected", li.dataset.id === id
      && (!printingId || li.dataset.printing === printingId));
  });
  pane.innerHTML = `<p class="loading">Loading…</p>`;
  try {
    const [card] = await Promise.all([api(`/cards/${id}`), symbolsReady]);
    renderDetail(card, 0, pane);
  } catch (err) {
    pane.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message)}</p>`;
  }
}

async function loadSimilarInto(id, container) {
  const params = new URLSearchParams({
    limit: String(SIMILAR_LIMIT),
    projection: SIMILAR_PROJECTION,
  });
  try {
    const list = await api(`/cards/${id}/similar?${params}`);
    // selectCard may have moved on; only paint if the container is still in the DOM.
    if (!container.isConnected) return;
    if (!list.data || list.data.length === 0) {
      container.innerHTML = `<p class="empty">No similar cards.</p>`;
      return;
    }
    const items = list.data.map((c) => {
      const sim = c.similarity != null ? c.similarity.toFixed(3) : "";
      const meta = [];
      if (c.type_line) meta.push(`<span>${escapeHtml(c.type_line)}</span>`);
      if (sim) meta.push(`<span>${sim}</span>`);
      const img = c.image_uris?.small
        || c.card_faces?.[0]?.image_uris?.small
        || null;
      const big = c.image_uris?.normal
        || c.card_faces?.[0]?.image_uris?.normal
        || img;
      const thumb = img
        ? `<img class="thumb" src="${img}" alt="" loading="lazy">`
        : `<span class="thumb thumb-empty" aria-hidden="true"></span>`;
      const previewAttr = big ? ` data-preview="${big}"` : "";
      return `<li data-id="${c.id}"${previewAttr} tabindex="0">
        ${thumb}
        <div class="body">
          <div class="name">${escapeHtml(c.name || "?")}</div>
          <div class="meta">${meta.join("")}</div>
        </div>
      </li>`;
    }).join("");
    container.innerHTML = `<ul class="cards similar-list">${items}</ul>`;
  } catch (err) {
    if (!container.isConnected) return;
    const msg = err.status === 404
      ? "Embeddings not ready for this card."
      : `Error: ${escapeHtml(err.message)}`;
    container.innerHTML = `<p class="empty">${msg}</p>`;
  }
}

// Render one combo as a collapsed <details> with clickable card chips,
// prereqs/steps/results prose, and a link to Commander Spellbook. Chip
// carries `data-related-id` so the existing detailEl click handler
// (10-cards.js) routes into selectCard(). Cards missing a
// `card_ids` slot (older ingests, unresolved oracle_id) render as plain
// non-clickable spans.
function _renderComboEntry(combo) {
  const names = Array.isArray(combo.cards) ? combo.cards : [];
  const ids = Array.isArray(combo.card_ids) ? combo.card_ids : [];
  const chips = names.map((n, i) => {
    const cid = ids[i];
    if (cid) {
      return `<button type="button" class="chip related-chip" data-related-id="${escapeHtml(cid)}">${escapeHtml(n)}</button>`;
    }
    return `<span class="chip chip-static">${escapeHtml(n)}</span>`;
  }).join("");
  const results = Array.isArray(combo.results) ? combo.results : [];
  const resultsLine = results.length
    ? `<p class="combo-results"><strong>Produces:</strong> ${escapeHtml(results.join(", "))}</p>`
    : "";
  const prereqs = combo.prereqs
    ? `<p class="combo-prereqs"><strong>Prereqs:</strong> ${escapeHtml(combo.prereqs)}</p>`
    : "";
  const steps = combo.steps
    ? `<p class="combo-steps"><strong>Steps:</strong> ${escapeHtml(combo.steps)}</p>`
    : "";
  const localLink = combo.id
    ? `<a class="ext-link" href="?tab=combos&combo=${encodeURIComponent(combo.id)}">Combo details</a>`
    : "";
  const spellbookUrl = combo.id
    ? `https://commanderspellbook.com/combo/${encodeURIComponent(combo.id)}/`
    : null;
  const link = spellbookUrl
    ? `<a class="ext-link" href="${spellbookUrl}" target="_blank" rel="noopener">Spellbook</a>`
    : "";
  const links = [localLink, link].filter(Boolean).join(" · ");
  const summary = names.length
    ? escapeHtml(names.join(" + "))
    : `Combo ${escapeHtml(combo.id || "?")}`;
  return `<details class="combo-entry">
    <summary>${summary}${resultsLine ? ` <span class="combo-summary-tail">→ ${escapeHtml(results.join(", "))}</span>` : ""}</summary>
    <div class="combo-body">
      <div class="chips">${chips}</div>
      ${prereqs}
      ${steps}
      ${resultsLine}
      ${links}
    </div>
  </details>`;
}

async function loadCombosInto(id, container) {
  try {
    const list = await api(`/cards/${id}/combos`);
    if (!container.isConnected) return;
    const combos = list.data || [];
    if (combos.length === 0) {
      container.innerHTML = `<p class="empty">No combos.</p>`;
      return;
    }
    const initial = combos.slice(0, COMBOS_INITIAL_LIMIT).map(_renderComboEntry).join("");
    const overflow = combos.length - COMBOS_INITIAL_LIMIT;
    const more = overflow > 0
      ? `<button type="button" class="combos-load-all">Show ${overflow} more combos</button>`
      : "";
    container.innerHTML = `<div class="combos-list">${initial}</div>${more}`;
    if (overflow > 0) {
      container.querySelector(".combos-load-all").addEventListener("click", (e) => {
        e.currentTarget.remove();
        const rest = combos.slice(COMBOS_INITIAL_LIMIT).map(_renderComboEntry).join("");
        container.querySelector(".combos-list").insertAdjacentHTML("beforeend", rest);
      }, { once: true });
    }
  } catch (err) {
    if (!container.isConnected) return;
    if (err.status === 404) {
      // stale id after navigate — silently drop
      return;
    }
    container.innerHTML = `<p class="state-error" role="alert">Combos error: ${escapeHtml(err.message)}</p>`;
  }
}

// Poll /health: one probe at page load, then every 5s while a refresh is
// running. State decision is delegated to `bannerStateFromHealth` (pure, in
// helpers.js) so "should the timer be running?" is a function of the /health
// response — never strands the poll on a refresh that ended. Each probe also
// redraws the Settings refresh block and the Run-refresh button state.
let _tagsPollTimer = null;
// Last bannerStateFromHealth kind; drives the zero-results copy and the
// "re-run the search once cards are searchable" transition.
let _healthKind = null;
function _clearTagsPollTimer() {
  if (_tagsPollTimer) { clearInterval(_tagsPollTimer); _tagsPollTimer = null; }
}
const STAGE_LABELS = {
  ingest: "Card data", embed: "Search embeddings", hnsw: "Embedding index",
  classify: "Card role labels", tags: "Oracle tags", combos: "Combos", deck_cards: "Deck card index",
};
const _fmtSecs = (n) => (n == null ? "" : n >= 60 ? `${Math.floor(n / 60)}m ${n % 60}s` : `${n}s`);
function renderRefreshStatus(refresh, combosCount) {
  const order = refreshStageInputs().map((el) => el.value);
  const p = window.bibliohelpers.refreshProgress(refresh, order, Date.now());
  const box = $("#refreshStatus");
  if (box) {
    const mark = { pending: "·", running: "…", done: "✓", error: "✗" };
    box.innerHTML = p.rows.length === 0 ? "" : `<ul class="refresh-progress">${p.rows.map((r) =>
      `<li class="rp-${r.status}"><span class="rp-mark">${mark[r.status]}</span> ${escapeHtml(STAGE_LABELS[r.name] || r.name)}` +
      `<span class="rp-meta">${r.status === "error" ? escapeHtml(r.error) :
        [r.rows != null ? `${r.rows.toLocaleString()} rows`
          : r.name === "combos" && r.status === "running" && combosCount != null ? `${combosCount.toLocaleString()} imported so far` : "",
          _fmtSecs(r.seconds)].filter(Boolean).join(" · ")}</span></li>`
    ).join("")}</ul>` + (!refresh.running && p.totalSeconds != null
      ? `<p class="help">Last run took ${_fmtSecs(p.totalSeconds)}.</p>` : "");
  }
  refreshBtn.disabled = !!refresh.running;
  return p;
}
// Settings About line: running version, linked to its release notes.
function _showVersion(v) {
  const el = document.getElementById("appVersion");
  if (!el || !v || el.dataset.v === v) return;
  el.dataset.v = v;
  const tag = encodeURIComponent("v" + v);
  el.innerHTML = `<a href="https://github.com/dgspai/biblioplex/releases/tag/${tag}" target="_blank" rel="noopener" title="Release notes">v${escapeHtml(v)}</a>`;
}
async function pollOracleTagsStatus() {
  let h;
  try { h = await api("/health"); }
  catch { return; /* /health down — search error path will surface it. */ }
  _showVersion(h.version);
  const p = renderRefreshStatus(h.refresh || {}, h.combos_count);
  const state = window.bibliohelpers.bannerStateFromHealth(h);
  const prevKind = _healthKind;
  _healthKind = state.kind;
  if (state.kind === "hidden") {
    _clearTagsPollTimer();
    hideBanner();
  } else if (state.kind === "background") {
    // Cards are searchable; only combos & co. remain (progress in Settings).
    hideBanner();
    if (!_tagsPollTimer) _tagsPollTimer = setInterval(pollOracleTagsStatus, 5_000);
  } else if (state.kind === "ingesting") {
    const steps = p.coreTotal > 0 ? `${p.coreDone} of ${p.coreTotal} steps` : `${p.done} step${p.done === 1 ? "" : "s"}`;
    showBanner(`Downloading and indexing card data… ${steps} done. ` +
      `Search fills in as it finishes; progress is under Settings.`, "info");
    if (!_tagsPollTimer) _tagsPollTimer = setInterval(pollOracleTagsStatus, 5_000);
  } else if (state.kind === "empty") {
    _clearTagsPollTimer();
    showBanner(`No card data yet.
      <button type="button" class="btn btn--primary btn--sm banner-action" data-banner-refresh>Download card data</button>`, "info");
  } else if (state.kind === "missing") {
    _clearTagsPollTimer();
    showBanner(`No card tags loaded.
      <button type="button" class="btn btn--primary btn--sm banner-action" data-banner-refresh>Download card data</button>`, "info");
  }
  if (window.bibliohelpers.shouldRerunSearch(prevKind, state.kind)) {
    search(qInput.value.trim(), orderSel.value, dirSel.value, currentPage);
  }
}
bannerEl.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-banner-refresh]");
  if (!btn) return;
  btn.disabled = true;
  startRefresh([]);
});

// Dark-mode toggle. `data-theme` attribute overrides the OS preference;
// stored in localStorage so the choice persists across reloads.
function applyTheme(theme) {
  if (theme === "light" || theme === "dark") {
    document.documentElement.setAttribute("data-theme", theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
}
function currentTheme() {
  return document.documentElement.getAttribute("data-theme")
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
// Trigger POST /admin/refresh, optionally scoped to a stage subset.
// Endpoint is idempotent — a duplicate click while a run is in flight
// returns 202 `already_running` with the existing job_id, so no client-
// side lock needed. Kicks pollOracleTagsStatus so the banner and the
// Settings progress block surface it.
const refreshStageInputs = () =>
  Array.from(document.querySelectorAll(".refresh-stage"));
const refreshSelectedStages = () =>
  refreshStageInputs().filter((el) => el.checked).map((el) => el.value);
document.getElementById("refreshAllBtn")?.addEventListener("click", () => {
  refreshStageInputs().forEach((el) => { el.checked = true; });
});
document.getElementById("refreshNoneBtn")?.addEventListener("click", () => {
  refreshStageInputs().forEach((el) => { el.checked = false; });
});
// DAG-driven cascade (mirrors README "Refresh stage dependencies"):
// checking a stage transitively re-checks its prerequisites (upstream);
// unchecking a stage transitively unchecks anything that depends on it
// (downstream). tags is fully independent; hnsw is the only stage with
// a non-ingest prereq. Keep in sync with RefreshRunner.ALL_STAGES + the
// dep table documented in the README.
const STAGE_DEPS = {
  ingest: [],
  embed: ["ingest"],
  hnsw: ["embed"],
  classify: ["ingest"],
  tags: [],
  combos: ["ingest"],
  deck_cards: ["ingest"],
};
const STAGE_DEPENDENTS = Object.fromEntries(
  Object.keys(STAGE_DEPS).map((s) => [s, []]),
);
for (const [s, deps] of Object.entries(STAGE_DEPS)) {
  for (const d of deps) STAGE_DEPENDENTS[d].push(s);
}
const setStageChecked = (name, checked) => {
  const el = refreshStageInputs().find((i) => i.value === name);
  if (el) el.checked = checked;
};
refreshStageInputs().forEach((el) => {
  el.addEventListener("change", () => {
    const stack = [el.value];
    const seen = new Set();
    while (stack.length) {
      const cur = stack.pop();
      if (seen.has(cur)) continue;
      seen.add(cur);
      const next = el.checked ? STAGE_DEPS[cur] : STAGE_DEPENDENTS[cur];
      for (const n of next || []) {
        setStageChecked(n, el.checked);
        stack.push(n);
      }
    }
  });
});
// POST /admin/refresh for `stages` (empty = full pipeline) and kick the
// banner poller. Shared by Settings › Run refresh and the "Download card
// data" banner button. Returns true when the server accepted the job.
async function startRefresh(stages) {
  const total = refreshStageInputs().length;
  // Subset → ?stages=a,b,c; full pipeline → no param (server default).
  const qs = stages.length === 0 || stages.length === total
    ? "" : `?stages=${encodeURIComponent(stages.join(","))}`;
  try {
    const resp = await fetch("/api/admin/refresh" + qs, { method: "POST" });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      showBanner(`Refresh trigger failed: ${escapeHtml(body.detail || resp.statusText)}`);
      return false;
    }
    // The poll draws the progress banner; /health already reports running=true
    // because the server marks the job started before it replies.
    await pollOracleTagsStatus();
    return true;
  } catch (err) {
    showBanner(`Refresh trigger failed: ${escapeHtml(err.message || String(err))}`);
    return false;
  }
}
refreshBtn.addEventListener("click", async () => {
  const stages = refreshSelectedStages();
  if (stages.length === 0) {
    showBanner("Refresh: select at least one stage.");
    return;
  }
  refreshBtn.disabled = true;
  // On success the poll leaves the button disabled while the job runs.
  if (!(await startRefresh(stages))) refreshBtn.disabled = false;
});

themeBtn.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  applyTheme(next);
  localStorage.setItem("theme", next);
});
applyTheme(localStorage.getItem("theme"));

