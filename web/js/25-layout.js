// D9.9 — pane splitter. Pointer drag updates `--split` on <main> as a percent
// of the main element's width; the value is clamped to [15, 85] (helpers.js
// `clampSplit`) so neither pane fully collapses and persisted to localStorage
// so the user's choice survives reloads. The splitter is a grid track between
// #results and #detail (see style.css grid-template-columns).
const _SPLIT_KEY = "biblioplex.split";
// Every split pane shares the one persisted `--split` percentage, so the
// cards / inventory / wishlist tabs stay visually consistent and dragging
// any of them moves all three.
const _splitGrids = [cardsSplitEl, invSplitEl, wlSplitEl].filter(Boolean);

function _applySplit(percent) {
  const clamped = window.bibliohelpers.clampSplit(percent);
  for (const grid of _splitGrids) grid.style.setProperty("--split", `${clamped}%`);
  return clamped;
}
const _savedSplit = localStorage.getItem(_SPLIT_KEY);
if (_savedSplit !== null) _applySplit(_savedSplit);

// `apply`/`key` default to the shared cards/inventory/wishlist split; Combos
// passes its own so its wider list keeps a separate width.
function wireSplitter(splitter, grid, apply = _applySplit, key = _SPLIT_KEY) {
  if (!splitter || !grid) return;
  let dragging = false;
  splitter.addEventListener("pointerdown", (e) => {
    dragging = true;
    splitter.setPointerCapture(e.pointerId);
    document.body.style.cursor = "col-resize";
  });
  splitter.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const rect = grid.getBoundingClientRect();
    apply(((e.clientX - rect.left) / rect.width) * 100);
  });
  splitter.addEventListener("pointerup", (e) => {
    if (!dragging) return;
    dragging = false;
    splitter.releasePointerCapture(e.pointerId);
    document.body.style.cursor = "";
    // Re-read the applied value so what we persist matches what's on screen.
    const current = grid.style.getPropertyValue("--split").replace("%", "").trim();
    if (current) localStorage.setItem(key, current);
  });
}
wireSplitter(splitterEl, cardsSplitEl);
wireSplitter(invSplitterEl, invSplitEl);
wireSplitter(wlSplitterEl, wlSplitEl);

// Back/forward should re-paint to match the URL the browser navigated to.
window.addEventListener("popstate", () => {
  const params = new URLSearchParams(location.search);
  const tabParam = params.get("tab");
  const tab = tabParam === "compare" ? "compare"
    : tabParam === "combos" ? "combos"
    : "search";
  _setTab(tab);
  if (tab === "combos") {
    _combosStateFromUrl(params);
    runCombosSearch();
    return;
  }
  if (tab === "compare") return;
  const state = window.bibliohelpers.urlStateFromSearch(location.search);
  qInput.value = state.q || "";
  orderSel.value = state.order;
  dirSel.value = state.dir;
  const prevId = selectedId;
  currentPage = state.page;
  selectedId = state.id;
  if (state.q) {
    search(state.q, state.order, state.dir, state.page).then(() => {
      if (selectedId && selectedId !== prevId) selectCard(selectedId);
    });
  } else {
    resultsEl.innerHTML = `<p class="empty">Enter a query to search.</p>`;
    detailEl.innerHTML = `<p class="empty">Select a card to see details.</p>`;
  }
});


// Collapsible sidebar. Remembered per browser; storage may throw (private
// mode, blocked site data), which only costs the memory, never the toggle.
const _SIDEBAR_KEY = "biblioplex.sidebar";
const sidebarToggleEl = $("#sidebarToggle");

function _setSidebarCollapsed(collapsed) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  sidebarToggleEl.setAttribute("aria-expanded", String(!collapsed));
  const label = collapsed ? "Expand sidebar" : "Collapse sidebar";
  sidebarToggleEl.setAttribute("aria-label", label);
  sidebarToggleEl.title = `${label}  [`;
  try { localStorage.setItem(_SIDEBAR_KEY, collapsed ? "collapsed" : "open"); } catch { /* ignore */ }
}

function _toggleSidebar() {
  _setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"));
}

try {
  if (localStorage.getItem(_SIDEBAR_KEY) === "collapsed") _setSidebarCollapsed(true);
} catch { /* ignore */ }
sidebarToggleEl.addEventListener("click", _toggleSidebar);
document.addEventListener("keydown", (e) => {
  if (e.key === "[" && !e.ctrlKey && !e.metaKey && !e.altKey && !_inEditable(document.activeElement)) {
    e.preventDefault();
    _toggleSidebar();
  }
});

// Native <dialog>: [data-open-dialog="<id>"] opens it modally,
// [data-close-dialog] (inside it) closes it, as do Esc and a backdrop click.
document.addEventListener("click", (e) => {
  const opener = e.target.closest("[data-open-dialog]");
  if (opener) {
    document.getElementById(opener.dataset.openDialog)?.showModal();
    return;
  }
  if (e.target.closest("[data-close-dialog]")) {
    e.target.closest("dialog")?.close();
    return;
  }
  // Backdrop clicks target the <dialog> itself, but so do clicks on its
  // padding, so close only when the point is outside the dialog's box.
  const d = e.target;
  if (d instanceof HTMLDialogElement && d.open) {
    const r = d.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) d.close();
  }
});

// Phones show list or detail, not both (style.css): a fresh render of a
// detail pane opens it, Back returns to the list.
for (const pane of document.querySelectorAll(".cards-split > .detail-pane, #deckDetail")) {
  const split = pane.closest(".cards-split, .decks-split");
  const back = Object.assign(document.createElement("button"), { type: "button", className: "split-back", textContent: "← Back" });
  back.addEventListener("click", () => split.classList.remove("detail-open"));
  split.append(back);
  new MutationObserver(() => {
    split.classList.toggle("detail-open", !pane.querySelector(":scope > .empty:only-child"));
  }).observe(pane, { childList: true });
}
