// -------- Combos tab --------
// Wire the combo browser: form → GET /api/combos/search → dual pane
// (list left / detail right). List rows are compact summaries; clicking
// a row renders the full combo (chips, prereqs, steps, results, Spellbook
// link) into #combosDetail. URL still mirrors form state so links stay
// shareable (`?tab=combos&cards=Isochron+Scepter&ci=U`). Module state
// (`_combosPage`, `_combosLoaded`, `_combosCurrent`, `_combosPreselectId`,
// `COMBOS_PAGE_SIZE`) is hoisted to the top of the file — _syncUrl reads it
// during boot before this section executes.

function _renderComboRow(combo, idx) {
  const names = Array.isArray(combo.cards) ? combo.cards : [];
  const results = Array.isArray(combo.results) ? combo.results : [];
  const title = names.length
    ? escapeHtml(names.join(" + "))
    : `Combo ${escapeHtml(combo.id || "?")}`;
  const tail = results.length
    ? `<div class="meta">${escapeHtml(results.join(", "))}</div>`
    : "";
  return `<li data-combo-idx="${idx}"><div class="name">${title}</div>${tail}</li>`;
}

// Cache small/normal image URLs per card id — one fetch per card,
// shared across every combo that references it. Cleared only on reload.
const _comboThumbCache = new Map();

async function _fetchComboThumb(id) {
  if (_comboThumbCache.has(id)) return _comboThumbCache.get(id);
  try {
    const card = await api(`/cards/${id}?projection=image_uris,card_faces`);
    const uris = card.image_uris || card.card_faces?.[0]?.image_uris || {};
    const out = {
      small: uris.small || uris.normal || null,
      normal: uris.normal || uris.large || uris.small || null,
    };
    _comboThumbCache.set(id, out);
    return out;
  } catch {
    _comboThumbCache.set(id, { small: null, normal: null });
    return _comboThumbCache.get(id);
  }
}

// Split newline-delimited prose (Spellbook stores prereqs/steps as
// `\n`-separated lines) into a semantic list. Each line runs through
// renderSymbols so `{U}` etc. render as inline SVG like the card detail.
function _proseToItems(text) {
  return String(text || "")
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function _renderComboDetail(combo) {
  const names = Array.isArray(combo.cards) ? combo.cards : [];
  const ids = Array.isArray(combo.card_ids) ? combo.card_ids : [];
  // Card gallery: <figure> per card mirrors #detail .faces so MDFC-style
  // captions read the same on both surfaces. Placeholders keyed by
  // data-card-thumb; async fill swaps in the small Scryfall URL. The whole
  // figure carries data-related-id so click/hover-preview hit anywhere.
  const cards = ids.map((cid, i) => {
    if (!cid) return "";
    const name = names[i] || "";
    return `<button type="button" class="combo-card face" data-related-id="${escapeHtml(cid)}" data-card-name="${escapeHtml(name)}" title="${escapeHtml(name)}">
      <img class="combo-thumb" data-card-thumb="${escapeHtml(cid)}" alt="${escapeHtml(name)}" loading="lazy">
      <figcaption>${escapeHtml(name)}</figcaption>
    </button>`;
  }).join("");
  const results = Array.isArray(combo.results) ? combo.results : [];
  const resultsBlock = results.length
    ? `<div class="combo-section"><h3>Produces</h3><ul class="combo-prose">${
        results.map((r) => `<li>${renderSymbols(r)}</li>`).join("")
      }</ul></div>`
    : "";
  const prereqItems = _proseToItems(combo.prereqs);
  const prereqs = prereqItems.length
    ? `<div class="combo-section"><h3>Prereqs</h3><ul class="combo-prose">${
        prereqItems.map((p) => `<li>${renderSymbols(p)}</li>`).join("")
      }</ul></div>`
    : "";
  const stepItems = _proseToItems(combo.steps);
  const steps = stepItems.length
    ? `<div class="combo-section"><h3>Steps</h3><ol class="combo-prose">${
        stepItems.map((s) => `<li>${renderSymbols(s)}</li>`).join("")
      }</ol></div>`
    : "";
  const title = names.length
    ? escapeHtml(names.join(" + "))
    : `Combo ${escapeHtml(combo.id || "?")}`;
  const link = combo.id
    ? `<a class="ext-link" href="https://commanderspellbook.com/combo/${encodeURIComponent(combo.id)}/" target="_blank" rel="noopener">Spellbook</a>`
    : "";
  return `<div class="detail-header"><h2>${title}</h2><div class="detail-actions">${link}</div></div>
    <div class="combo-cards">${cards}</div>
    ${resultsBlock}
    ${prereqs}
    ${steps}`;
}

// After _renderComboDetail writes placeholders, fill in image srcs in the
// background. Runs in parallel per card; a stale render (user clicked a
// different combo) is harmless because the target <img> is already gone.
async function _fillComboThumbs(container) {
  const imgs = container.querySelectorAll("img[data-card-thumb]");
  await Promise.all(Array.from(imgs).map(async (img) => {
    const id = img.dataset.cardThumb;
    const uris = await _fetchComboThumb(id);
    if (!img.isConnected) return;
    if (uris.small) img.src = uris.small;
    else img.remove();
  }));
}

// Picked cards live in `_combosCards` (declared in 00-core.js with the other
// boot-read combos state) and render as chips. Names are never split on
// commas: `Kenrith, the Returned King` is one card.

function _renderCombosCardChips() {
  combosCardChipsEl.innerHTML = _combosCards
    .map((n) => `<button type="button" class="chip" data-remove-card="${escapeHtml(n)}" aria-label="Remove ${escapeHtml(n)}">${escapeHtml(n)} ×</button>`)
    .join("");
}

// Returns true when the name was new (caller re-runs the search).
function _addCombosCard(name) {
  const n = name.trim();
  if (!n || _combosCards.some((c) => c.toLowerCase() === n.toLowerCase())) return false;
  _combosCards.push(n);
  _renderCombosCardChips();
  return true;
}

function _combosCiBtns() { return Array.from(combosCiEl.querySelectorAll("[data-ci]")); }

function _setCombosCi(ci) {
  for (const b of _combosCiBtns()) b.setAttribute("aria-pressed", String(ci.includes(b.dataset.ci)));
}

function _combosFormState() {
  return {
    cards: [..._combosCards],
    // Button order is WUBRG then C, so the string is already canonical.
    ci: _combosCiBtns().filter((b) => b.getAttribute("aria-pressed") === "true").map((b) => b.dataset.ci).join(""),
    result: combosResultInputEl.value.trim(),
    q: combosQEl.value.trim(),
    sort: combosSortEl.value,
    page: _combosPage,
  };
}

function _combosUrlSuffix() {
  const s = _combosFormState();
  const p = new URLSearchParams();
  for (const c of s.cards) p.append("card", c);
  if (s.ci) p.set("ci", s.ci);
  if (s.result) p.set("result", s.result);
  if (s.q) p.set("q", s.q);
  if (s.sort && s.sort !== "id") p.set("sort", s.sort);
  if (s.page && s.page > 1) p.set("page", String(s.page));
  const str = p.toString();
  return str ? "&" + str : "";
}

function _combosStateFromUrl(params) {
  _combosCards = [];
  for (const c of params.getAll("card")) _addCombosCard(c);
  _renderCombosCardChips();
  combosCardsEl.value = "";
  _setCombosCi((params.get("ci") || "").toUpperCase());
  combosResultInputEl.value = params.get("result") || "";
  combosQEl.value = params.get("q") || "";
  combosSortEl.value = params.get("sort") === "cards" ? "cards" : "id";
  const p = parseInt(params.get("page") || "1", 10);
  _combosPage = Number.isFinite(p) && p > 0 ? p : 1;
  _combosPreselectId = params.get("combo") || null;
}

async function runCombosSearch() {
  _combosLoaded = true;
  const s = _combosFormState();
  const p = new URLSearchParams();
  for (const c of s.cards) p.append("card", c);
  if (s.ci) p.set("ci", s.ci);
  if (s.result) p.set("result", s.result);
  if (s.q) p.set("q", s.q);
  if (s.sort) p.set("sort", s.sort);
  p.set("page", String(s.page));
  p.set("page_size", String(COMBOS_PAGE_SIZE));
  combosResultsEl.innerHTML = `<p class="empty">Loading combos…</p>`;
  combosDetailEl.innerHTML = `<p class="empty">Select a combo to see details.</p>`;
  combosPagerEl.hidden = true;
  try {
    const body = await api(`/combos/search?${p.toString()}`);
    const list = body.data || [];
    _combosCurrent = list;
    if (list.length === 0) {
      combosResultsEl.innerHTML = emptyStateHtml("No combos found", "No combos match that search. Try fewer or broader filters.");
    } else {
      const items = list.map((c, i) => _renderComboRow(c, i)).join("");
      combosResultsEl.innerHTML = `<ul class="combos-cards">${items}</ul>`;
    }
    const total = body.total || 0;
    if (total > COMBOS_PAGE_SIZE || s.page > 1) {
      combosPagerEl.hidden = false;
      combosPrevBtn.disabled = s.page <= 1;
      combosNextBtn.disabled = !body.has_more;
      combosPageInfoEl.textContent = pagerRange(s.page, COMBOS_PAGE_SIZE, total);
    }
    if (_combosPreselectId) await _consumeCombosPreselect();
    _syncUrl();
  } catch (err) {
    // The API answers "no match" with a 404; that is an empty result, not a failure.
    combosResultsEl.innerHTML = err.status === 404
      ? emptyStateHtml("No combos found", "No combos match that search. Try fewer or broader filters.")
      : `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
  }
}

// One-shot: fetch (or find in the current page) the preselected combo,
// render it into the detail pane, highlight the row if visible, then
// clear the flag so filter changes don't re-fire.
async function _consumeCombosPreselect() {
  const id = _combosPreselectId;
  _combosPreselectId = null;
  const idx = _combosCurrent.findIndex((c) => c && c.id === id);
  let combo;
  if (idx >= 0) {
    combo = _combosCurrent[idx];
    const row = combosResultsEl.querySelector(`li[data-combo-idx="${idx}"]`);
    if (row) {
      combosResultsEl.querySelectorAll("li.selected").forEach((el) => el.classList.remove("selected"));
      row.classList.add("selected");
      row.scrollIntoView({ block: "nearest" });
    }
  } else {
    try { combo = await api(`/combos/${encodeURIComponent(id)}`); }
    catch { combo = null; }
  }
  if (!combo) return;
  combosDetailEl.innerHTML = _renderComboDetail(combo);
  _fillComboThumbs(combosDetailEl);
}

if (combosFormEl) {
  combosFormEl.addEventListener("submit", (e) => {
    e.preventDefault();
    // Enter with a typed (not picked) name still adds it as a chip.
    _addCombosCard(combosCardsEl.value);
    combosCardsEl.value = "";
    _combosPage = 1;
    runCombosSearch();
  });
  // Card autocomplete: refill the native <datalist> from /cards/autocomplete
  // (debounced); picking a suggestion makes the value an exact option match,
  // which adds the chip and searches at once.
  let _combosAcTimer = null;
  combosCardsEl.addEventListener("input", () => {
    const v = combosCardsEl.value.trim();
    const picked = Array.from(combosCardListEl.options).some((o) => o.value === v);
    if (picked) {
      combosCardsEl.value = "";
      combosCardListEl.innerHTML = "";
      if (_addCombosCard(v)) { _combosPage = 1; runCombosSearch(); }
      return;
    }
    if (_combosAcTimer) clearTimeout(_combosAcTimer);
    if (v.length < 2) return;
    _combosAcTimer = setTimeout(async () => {
      try {
        const res = await api(`/cards/autocomplete?q=${encodeURIComponent(v)}`);
        combosCardListEl.innerHTML = (res.data || []).slice(0, 10)
          .map((n) => `<option value="${escapeHtml(n)}"></option>`).join("");
      } catch (_e) { /* no suggestions */ }
    }, 200);
  });
  combosCardChipsEl.addEventListener("click", (e) => {
    const chip = e.target.closest?.("[data-remove-card]");
    if (!chip) return;
    _combosCards = _combosCards.filter((c) => c !== chip.dataset.removeCard);
    _renderCombosCardChips();
    _combosPage = 1;
    runCombosSearch();
  });
  combosCiEl.addEventListener("click", (e) => {
    const b = e.target.closest?.("[data-ci]");
    if (!b) return;
    const on = b.getAttribute("aria-pressed") !== "true";
    // Colorless is exclusive: any color set already includes colorless combos.
    if (on) {
      for (const o of _combosCiBtns()) {
        if ((b.dataset.ci === "C") !== (o.dataset.ci === "C")) o.setAttribute("aria-pressed", "false");
      }
    }
    b.setAttribute("aria-pressed", String(on));
    _combosPage = 1;
    runCombosSearch();
  });
  combosResetBtn.addEventListener("click", () => {
    _combosCards = [];
    _renderCombosCardChips();
    combosCardsEl.value = "";
    _setCombosCi("");
    combosResultInputEl.value = "";
    combosQEl.value = "";
    combosSortEl.value = "id";
    _combosPage = 1;
    runCombosSearch();
  });
  combosPrevBtn.addEventListener("click", () => {
    if (_combosPage > 1) { _combosPage--; runCombosSearch(); }
  });
  combosNextBtn.addEventListener("click", () => {
    _combosPage++;
    runCombosSearch();
  });
  // List rows: click → render detail on right, highlight row.
  combosResultsEl.addEventListener("click", (e) => {
    const li = e.target.closest?.("li[data-combo-idx]");
    if (!li) return;
    const idx = parseInt(li.dataset.comboIdx, 10);
    const combo = _combosCurrent[idx];
    if (!combo) return;
    combosResultsEl.querySelectorAll("li.selected").forEach((el) => el.classList.remove("selected"));
    li.classList.add("selected");
    combosDetailEl.innerHTML = _renderComboDetail(combo);
    _fillComboThumbs(combosDetailEl);
  });
  // Cards inside the combo detail route back into the Cards tab: build a
  // DSL query that OR-matches every card in the combo (`!"A" or !"B"`) so
  // the result list is scoped to just this combo, then open the clicked
  // card in the detail pane. Skip the requery when the click came from
  // the card gallery of a full combo (data-card-name present); the
  // Prereqs/Steps blocks that may also carry data-related-id fall back
  // to plain selectCard.
  combosDetailEl.addEventListener("click", (e) => {
    const btn = e.target.closest?.("[data-related-id]");
    if (!btn) return;
    _setTab("search");
    const gallery = btn.closest?.(".combo-cards");
    if (gallery) {
      const names = Array.from(gallery.querySelectorAll("[data-card-name]"))
        .map((el) => el.dataset.cardName)
        .filter(Boolean);
      if (names.length) {
        const dsl = names.map((n) => `!"${n.replace(/"/g, "")}"`).join(" or ");
        qInput.value = dsl;
        selectedId = btn.dataset.relatedId;
        form.dispatchEvent(new SubmitEvent("submit", { cancelable: true }));
        selectCard(btn.dataset.relatedId);
        return;
      }
    }
    selectCard(btn.dataset.relatedId);
  });
}

// Combos keeps its own persisted width (default 40%, see #combosSplit).
const _COMBOS_SPLIT_KEY = "biblioplex.combos.split";
function _applyCombosSplit(percent) {
  const clamped = window.bibliohelpers.clampSplit(percent);
  combosSplitEl.style.setProperty("--split", `${clamped}%`);
  return clamped;
}
const _savedCombosSplit = localStorage.getItem(_COMBOS_SPLIT_KEY);
if (_savedCombosSplit !== null) _applyCombosSplit(_savedCombosSplit);
wireSplitter(combosSplitterEl, combosSplitEl, _applyCombosSplit, _COMBOS_SPLIT_KEY);

// D17.3 — Random card. Respects the current DSL in the search box; empty
// box picks from the whole corpus. Result loads into the detail pane
// without touching the search results list (rolling doesn't invalidate
// the user's current search context).
const randomBtn = $("#randomBtn");
if (randomBtn) {
  randomBtn.addEventListener("click", async () => {
    const q = qInput.value.trim();
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    const suffix = params.toString() ? "?" + params.toString() : "";
    randomBtn.disabled = true;
    detailEl.innerHTML = `<p class="loading">Rolling…</p>`;
    try {
      const card = await api(`/cards/random${suffix}`);
      selectedId = card.id;
      _detailCard = null;
      _detailFaceIdx = 0;
      _syncUrl();
      renderDetail(card);
    } catch (err) {
      const msg = err.status === 404
        ? `<p class="empty">No cards matched the current filter.</p>`
        : `<p class="state-error" role="alert">Error: ${escapeHtml(err.message)}</p>`;
      detailEl.innerHTML = msg;
    } finally {
      randomBtn.disabled = false;
    }
  });
}

