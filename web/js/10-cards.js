// Event wiring.
form.addEventListener("submit", (e) => {
  e.preventDefault();
  hideAutocomplete();
  // A new query resets pagination — the page= in the URL refers to the
  // previous query and is meaningless against a fresh result set.
  search(qInput.value.trim(), orderSel.value, dirSel.value, 1);
});

// Autocomplete — fires when the query looks like a bare name (no DSL
// operator chars). Debounced 200ms. Arrow keys navigate; Enter picks the
// highlighted name (or falls through to normal submit); Esc closes.
// ponytail: prefix-only heuristic for "is this DSL?" — one char in {:<>=()} kills AC. Widen if operator surface grows.
const _AC_DEBOUNCE_MS = 200;
let _acTimer = null;
let _acAbort = null;
let _acItems = [];
let _acActive = -1;

function _looksLikeDsl(q) {
  return /[:<>=()"]/.test(q);
}

function hideAutocomplete() {
  acDropdownEl.hidden = true;
  acDropdownEl.innerHTML = "";
  _acItems = [];
  _acActive = -1;
  qInput.setAttribute("aria-expanded", "false");
  qInput.removeAttribute("aria-activedescendant");
}

function _renderAcList() {
  if (_acItems.length === 0) { hideAutocomplete(); return; }
  acDropdownEl.innerHTML = _acItems.map((name, i) => {
    const active = i === _acActive ? " active" : "";
    const id = `acOpt${i}`;
    return `<li id="${id}" role="option" class="ac-item${active}" data-name="${escapeHtml(name)}" aria-selected="${i === _acActive}">${escapeHtml(name)}</li>`;
  }).join("");
  acDropdownEl.hidden = false;
  qInput.setAttribute("aria-expanded", "true");
  if (_acActive >= 0) qInput.setAttribute("aria-activedescendant", `acOpt${_acActive}`);
  else qInput.removeAttribute("aria-activedescendant");
}

async function _fetchAutocomplete(q) {
  if (_acAbort) _acAbort.abort();
  _acAbort = new AbortController();
  try {
    const resp = await fetch(`/api/cards/autocomplete?q=${encodeURIComponent(q)}`, { signal: _acAbort.signal });
    if (!resp.ok) { hideAutocomplete(); return; }
    const body = await resp.json();
    _acItems = Array.isArray(body.data) ? body.data.slice(0, 20) : [];
    _acActive = -1;
    _renderAcList();
  } catch (e) {
    if (e.name !== "AbortError") hideAutocomplete();
  }
}

qInput.addEventListener("input", () => {
  const q = qInput.value.trim();
  if (_acTimer) clearTimeout(_acTimer);
  // D13.8 — hide history the moment the user starts typing; the AC panel
  // takes over from here.
  if (q !== "") hideHistory();
  if (q.length < 2 || _looksLikeDsl(q)) { hideAutocomplete(); return; }
  _acTimer = setTimeout(() => _fetchAutocomplete(q), _AC_DEBOUNCE_MS);
});

qInput.addEventListener("keydown", (e) => {
  if (acDropdownEl.hidden || _acItems.length === 0) return;
  if (e.key === "ArrowDown") {
    e.preventDefault();
    _acActive = (_acActive + 1) % _acItems.length;
    _renderAcList();
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    _acActive = (_acActive - 1 + _acItems.length) % _acItems.length;
    _renderAcList();
  } else if (e.key === "Enter" && _acActive >= 0) {
    e.preventDefault();
    qInput.value = _acItems[_acActive];
    hideAutocomplete();
    form.dispatchEvent(new SubmitEvent("submit", { cancelable: true }));
  } else if (e.key === "Escape") {
    // Close the dropdown only: a native type=search clears itself on Esc, and
    // the document-level handler below clears the query once nothing is open.
    e.preventDefault();
    e.stopPropagation();
    hideAutocomplete();
  } else if (e.key === "Tab" && _acActive >= 0) {
    e.preventDefault();
    qInput.value = _acItems[_acActive];
    hideAutocomplete();
  }
});

qInput.addEventListener("blur", () => {
  // Delay so a mousedown on a dropdown item can still resolve before hide.
  setTimeout(hideAutocomplete, 120);
});

acDropdownEl.addEventListener("mousedown", (e) => {
  const li = e.target.closest("li.ac-item[data-name]");
  if (!li) return;
  e.preventDefault();
  qInput.value = li.dataset.name;
  hideAutocomplete();
  form.dispatchEvent(new SubmitEvent("submit", { cancelable: true }));
});

// `target` is the detail pane this list feeds. Omitted for the cards tab,
// which keeps the historical default of #detail.
function wireListNavigation(root, target) {
  const pick = (li) => selectCard(li.dataset.id, target, li.dataset.printing);
  root.addEventListener("click", (e) => {
    const li = e.target.closest("li[data-id]");
    if (li) pick(li);
  });
  root.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const li = e.target.closest("li[data-id]");
    if (li) { e.preventDefault(); pick(li); }
  });
}
wireListNavigation(resultsEl);
wireListNavigation(detailEl);

// Slot-counts buttons live above the result list and reuse the chip-filter
// pattern (data-pred → applyChipFilter → submits search).
resultsEl.addEventListener("click", (e) => {
  const slot = e.target.closest(".slot[data-pred]");
  if (slot) applyChipFilter(slot.dataset.pred);
});

// Footer pager. Selected id cleared because the previously-shown card is
// unlikely to live on the new page and a stale highlight would mislead.
function _goCardsPage(next) {
  if (!Number.isFinite(next) || next < 1) return;
  selectedId = null;
  search(qInput.value.trim(), orderSel.value, dirSel.value, next);
  resultsEl.scrollTop = 0;
}
cardsPrevBtn.addEventListener("click", () => _goCardsPage(currentPage - 1));
cardsNextBtn.addEventListener("click", () => _goCardsPage(currentPage + 1));

// Chip → DSL predicate. `appendPredicate` (D9.3) handles token-aware
// dedup so re-clicking the same chip is a no-op. Native <button>
// elements fire `click` on Enter/Space too, so one listener covers
// both mouse and keyboard.
function applyChipFilter(pred) {
  const next = window.bibliohelpers.appendPredicate(qInput.value, pred);
  if (next === qInput.value) return;
  qInput.value = next;
  form.dispatchEvent(new SubmitEvent("submit", { cancelable: true }));
}
function wireDetailClicks(pane) {
  pane.addEventListener("click", (e) => {
  const face = e.target.closest("button.face[data-face-idx]");
  if (face && _detailCard) {
    const idx = parseInt(face.dataset.faceIdx, 10);
    if (Number.isFinite(idx) && idx !== _detailFaceIdx) renderDetail(_detailCard, idx, pane);
    return;
  }
  // Manual label override editor.
  if (e.target.closest("[data-label-edit]")) {
    const ed = pane.querySelector(".label-editor");
    if (ed) ed.hidden = !ed.hidden;
    return;
  }
  if (e.target.closest("[data-label-cancel]")) {
    const ed = pane.querySelector(".label-editor");
    if (ed) ed.hidden = true;
    return;
  }
  if (e.target.closest("[data-label-save]") && _detailCard) {
    const picked = Array.from(pane.querySelectorAll(".label-editor input:checked"))
      .map((i) => i.value);
    _saveCardLabels(pane, _detailCard, picked);
    return;
  }
  if (e.target.closest("[data-label-reset]") && _detailCard) {
    _saveCardLabels(pane, _detailCard, null);
    return;
  }
  const copyBtn = e.target.closest("button.copy-id[data-copy]");
  if (copyBtn) {
    const id = copyBtn.dataset.copy;
    if (!id) return;
    const restore = copyBtn.textContent;
    navigator.clipboard?.writeText(id).then(() => {
      copyBtn.textContent = "copied";
      setTimeout(() => { copyBtn.textContent = restore; }, 1000);
    }).catch(() => { copyBtn.textContent = "copy failed"; setTimeout(() => { copyBtn.textContent = restore; }, 1200); });
    return;
  }
  const btn = e.target.closest("button.chip");
  if (!btn) return;
  // Related-card chips (D9.10) navigate within the same pane; DSL chips
  // append to the query, which only makes sense on the cards tab.
  if (btn.dataset.relatedId) selectCard(btn.dataset.relatedId, pane);
  else if (btn.dataset.pred && pane === detailEl) applyChipFilter(btn.dataset.pred);
  });
}
wireDetailClicks(detailEl);

// Hover preview: one fixed-position img reused across the whole app.
// Delegated at document level — any element carrying `data-id` or
// `data-related-id` (via `hoverPreviewShouldShow`) is eligible, including
// result rows, suggest-fill rows, and related chips. Rows that already
// carry `data-preview` (search results) skip the fetch; other targets
// lazy-fetch `/cards/:id?projection=image_uris,card_faces` and cache the
// resolved URL for the session. Debounced 200ms so a fast mouse-cross
// doesn't spam requests. MDFC / transform surface the front face only.
const previewEl = document.createElement("img");
previewEl.id = "cardPreview";
previewEl.alt = "";
previewEl.hidden = true;
document.body.appendChild(previewEl);

const _previewCache = new Map();  // card id → resolved image URL (or null)
let _previewTimer = null;
let _previewCurrentId = null;

function positionPreview(x, y) {
  const pad = 16;
  const w = previewEl.offsetWidth || 244;
  const h = previewEl.offsetHeight || 340;
  let left = x + pad;
  let top = y + pad;
  if (left + w > window.innerWidth - pad) left = x - w - pad;
  if (top + h > window.innerHeight - pad) top = window.innerHeight - h - pad;
  if (top < pad) top = pad;
  previewEl.style.left = `${Math.max(pad, left)}px`;
  previewEl.style.top = `${top}px`;
}

function _dismissPreview() {
  clearTimeout(_previewTimer);
  _previewTimer = null;
  _previewCurrentId = null;
  previewEl.hidden = true;
  previewEl.removeAttribute("src");
}

async function _showPreview(id, precachedUrl) {
  if (_previewCurrentId !== id) return;  // moved away before 200ms
  let url = precachedUrl || _previewCache.get(id);
  if (url === undefined || url === null || url === "") {
    try {
      const card = await api(`/cards/${id}?projection=image_uris,card_faces`);
      url = card.image_uris?.normal
        || card.image_uris?.large
        || card.card_faces?.[0]?.image_uris?.normal
        || card.card_faces?.[0]?.image_uris?.large
        || null;
      _previewCache.set(id, url);
    } catch (_err) {
      return;
    }
  }
  if (!url || _previewCurrentId !== id) return;
  previewEl.src = url;
  previewEl.hidden = false;
}

document.addEventListener("mouseover", (e) => {
  const hit = window.bibliohelpers.hoverPreviewShouldShow(e.target);
  if (!hit) return;
  if (hit.id === selectedId) return;  // already shown large in the detail pane
  const el = e.target.closest?.("[data-preview]");
  const precached = el ? el.dataset.preview : null;
  clearTimeout(_previewTimer);
  _previewCurrentId = hit.id;
  positionPreview(e.clientX, e.clientY);
  _previewTimer = setTimeout(() => _showPreview(hit.id, precached), 200);
});

document.addEventListener("mousemove", (e) => {
  if (previewEl.hidden && _previewTimer === null) return;
  positionPreview(e.clientX, e.clientY);
});

document.addEventListener("mouseout", (e) => {
  const hit = window.bibliohelpers.hoverPreviewShouldShow(e.target);
  if (!hit) return;
  // Don't dismiss when moving to another child of the same target.
  const from = e.target.closest?.("[data-id],[data-related-id]");
  const to = e.relatedTarget?.closest?.("[data-id],[data-related-id]");
  if (from && to && from === to) return;
  _dismissPreview();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !previewEl.hidden) _dismissPreview();
});
// F18: a re-render under a stationary cursor never fires mouseout, so the
// preview also dies on click, scroll (any scroller, via capture) and when the
// pointer leaves the results list.
document.addEventListener("click", _dismissPreview, true);
document.addEventListener("scroll", _dismissPreview, true);
resultsEl.addEventListener("pointerleave", _dismissPreview);

// F29: a failed image load must not show raw alt text. Card art becomes a
// name placeholder; thumbnails vanish; a failed preview just closes.
document.addEventListener("error", (e) => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement)) return;
  if (img === previewEl) { _dismissPreview(); return; }
  if (img.classList.contains("art")) {
    const name = (img.alt || "").replace(/ card art$/, "");
    const ph = document.createElement("div");
    ph.className = "art art-missing";
    ph.innerHTML = `<strong>${escapeHtml(name)}</strong><span>Image unavailable</span>`;
    img.replaceWith(ph);
  } else if (img.classList.contains("thumb") || img.classList.contains("combo-thumb")) {
    img.style.visibility = "hidden";
  }
}, true);

// Keyboard shortcuts. `/` focuses search from anywhere. `Esc` in search
// clears the input (and closes AC). `j`/`k` step through result rows
// when focus isn't in a text input. All shortcuts are no-ops on the deck
// tab except `/` (which still moves focus to the search box after tab-
// switch — expected UX). ponytail: keydown at document, one listener.
function _inEditable(el) {
  return el && (el.matches?.("input, textarea, select, [contenteditable=true]"));
}

function _stepResultSelection(delta) {
  const rows = resultsEl.querySelectorAll("ul.cards > li[data-id]");
  if (rows.length === 0) return;
  let idx = -1;
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].dataset.id === selectedId) { idx = i; break; }
  }
  const next = idx < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, idx + delta));
  const li = rows[next];
  if (!li) return;
  li.scrollIntoView({ block: "nearest" });
  selectCard(li.dataset.id);
}

document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !_inEditable(document.activeElement)) {
    e.preventDefault();
    qInput.focus();
    qInput.select();
    return;
  }
  if (e.key === "Escape" && document.activeElement === qInput) {
    if (!acDropdownEl.hidden) { hideAutocomplete(); return; }
    if (qInput.value) {
      e.preventDefault();
      qInput.value = "";
      return;
    }
  }
  if ((e.key === "j" || e.key === "k") && !_inEditable(document.activeElement)) {
    if (_currentTab !== "search") return;
    e.preventDefault();
    _stepResultSelection(e.key === "j" ? 1 : -1);
  }
});

// D9.7 — hydrate from `?q=&order=&dir=&page=&id=` first; fall back to
// DEFAULT_Q when the URL has no query. URL state always wins (explicit user
// intent — shared link, refresh, back/fwd).
function _bootFromUrl() {
  const params = new URLSearchParams(location.search);
  // Read before _setTab: its _syncUrl() rewrites the URL from the empty box.
  const state = window.bibliohelpers.urlStateFromSearch(location.search);
  const tabParam = params.get("tab");
  // Any tab present in the markup is deep-linkable, so a browser refresh on
  // Decks / Inventory / Wishlist / Settings stays where the user was.
  const tab = _TAB_NAMES.includes(tabParam) ? tabParam : "search";
  // Hydrate the combos form BEFORE _setTab: its loader runs the one search
  // from the form's current contents (a second call would race it).
  if (tab === "combos") _combosStateFromUrl(params);
  _setTab(tab);
  if (tab === "combos") {
    resultsEl.innerHTML = `<p class="empty">Switch back to search to load results.</p>`;
    return;
  }
  const effQ = state.q || DEFAULT_Q;
  qInput.value = effQ;
  orderSel.value = state.order;
  dirSel.value = state.dir;
  currentPage = state.page;
  selectedId = state.id;
  // Skip the auto-search when booting directly into another tab — no point
  // hitting the API for results that live behind a hidden pane.
  if (tab === "compare") {
    resultsEl.innerHTML = `<p class="empty">Switch back to search to load results.</p>`;
    return;
  }
  if (effQ) {
    search(effQ, state.order, state.dir, state.page).then(() => {
      if (selectedId) selectCard(selectedId);
    });
  } else {
    resultsEl.innerHTML = `<p class="empty">Enter a query to search.</p>`;
  }
}

// Tab nav clicks.
tabSearchEl.addEventListener("click", () => _setTab("search"));
if (tabCompareEl) tabCompareEl.addEventListener("click", () => _setTab("compare"));
if (tabSettingsEl) tabSettingsEl.addEventListener("click", () => _setTab("settings"));
if (tabWishlistEl) tabWishlistEl.addEventListener("click", () => _setTab("wishlist"));
if (tabInventoryEl) tabInventoryEl.addEventListener("click", () => _setTab("inventory"));
if (tabCombosEl) tabCombosEl.addEventListener("click", () => _setTab("combos"));
if (tabBuildEl) tabBuildEl.addEventListener("click", () => _setTab("build"));

