// ---------------------------------------------------------------------------
// D18.6 — Inventory tab, owned badges, deck coverage bar.
// ---------------------------------------------------------------------------

// Owned-oracle lookup. One `GET /inventory/oracle-ids` builds the whole set,
// so badging a 60-tile page of search results costs zero extra requests.
// ponytail: in-memory Set, invalidated on sync/clear. A self-hosted
// collection is thousands of ids, not millions — swap for a bloom filter or
// a per-page lookup if that ever stops being true.
let _detailTarget = null;
let _ownedOracleIds = null;
let _wantedOracleIds = null;
let _wlPage = 1;
let _wlQ = "";
let _invPage = 1;
let _invQ = "";

async function loadOwnedOracleIds() {
  try {
    const body = await api("/inventory/oracle-ids");
    _ownedOracleIds = new Set(body.data || []);
  } catch {
    _ownedOracleIds = new Set();
  }
}

async function loadWantedOracleIds() {
  try {
    const body = await api("/wishlist/oracle-ids");
    _wantedOracleIds = new Set(body.data || []);
  } catch {
    _wantedOracleIds = new Set();
  }
}

// Collection markers. Owned and wanted are INDEPENDENT — owning a copy while
// still wanting another is a normal state — so both icons can show at once,
// unlike the earlier either/or text badge.
function _ownedBadge(oracleId) {
  if (!oracleId) return "";
  let out = "";
  if (_ownedOracleIds && _ownedOracleIds.has(oracleId)) {
    out += ` <span class="coll-icon owned-icon" role="img"`
      + ` title="In your inventory" aria-label="In your inventory">${ICON_BOX}</span>`;
  }
  if (_wantedOracleIds && _wantedOracleIds.has(oracleId)) {
    out += ` <span class="coll-icon wanted-icon" role="img"`
      + ` title="On your wishlist" aria-label="On your wishlist">${ICON_STAR}</span>`;
  }
  return out;
}

// Repaint tiles in place after the owned set changes. Reads `data-oracle` off
// the existing DOM rather than re-issuing the search — a sync should not cost
// the user their scroll position or another round-trip.
function _repaintOwnedBadges() {
  document.querySelectorAll("ul.cards li").forEach((li) => {
    const nameEl = li.querySelector(".name");
    if (!nameEl) return;
    nameEl.querySelectorAll(".owned-badge").forEach((b) => b.remove());
    const badge = _ownedBadge(li.dataset.oracle);
    if (badge) nameEl.insertAdjacentHTML("beforeend", badge);
  });
}

// --- inventory tab ----------------------------------------------------------

const INV_PAGE_SIZE = 50;

function _renderInvSummary(sum) {
  if (!sum) { invSummaryEl.innerHTML = ""; return; }
  const parts = [
    `${formatInt(sum.total_cards)} cards`,
    `${formatInt(sum.distinct_cards)} unique`,
    escapeHtml(formatUsd(sum.total_value_usd)),
  ];
  const src = sum.deckbox_url
    ? ` · <a class="ext-link" href="${escapeHtml(sum.deckbox_url)}" target="_blank" rel="noopener">Deckbox</a>`
    : "";
  invSummaryEl.innerHTML = parts.filter(Boolean).join(" · ") + src;
}

function _renderInvList(list) {
  const items = (list.data || []).map((r) => {
    // card_raw is the full Scryfall payload the row joined to; its `id` is
    // what the detail pane loads.
    const cardId = (r.card_raw && r.card_raw.id) || "";
    const meta = [];
    if (r.set_name) meta.push(`<span>${escapeHtml(r.set_name)}${r.collector_number ? ` #${escapeHtml(r.collector_number)}` : ""}</span>`);
    if (r.type_line) meta.push(`<span>${escapeHtml(r.type_line)}</span>`);
    if (r.price_usd != null) meta.push(`<span>${escapeHtml(formatUsd(r.price_usd))}</span>`);
    if (r.foil_quantity) meta.push(`<span class="foil">${r.foil_quantity} foil</span>`);
    return `<li data-id="${escapeHtml(cardId)}" data-oracle="${escapeHtml(r.oracle_id || "")}" tabindex="0">
      <div class="name"><span class="inv-qty-badge">${r.quantity}×</span>${escapeHtml(r.card_name || "?")}${_ownedBadge(r.oracle_id)}</div>
      <div class="meta">${meta.join("")}</div>
    </li>`;
  }).join("");
  invResultsEl.innerHTML = items
    ? `<ul class="cards">${items}</ul>`
    : (_invQ
      ? emptyStateHtml("No matches", "No owned card matches that filter.")
      : emptyStateHtml("No cards in your inventory yet",
          "Import a CSV export from Deckbox to see which cards you own.",
          { label: "Import inventory", tab: "settings", focus: "#invCsvFile" }));
  const total = list.total_cards ?? 0;
  invPagerEl.hidden = total <= INV_PAGE_SIZE;
  invPageInfoEl.textContent = pagerRange(_invPage, INV_PAGE_SIZE, total);
  invPrevBtn.disabled = _invPage <= 1;
  invNextBtn.disabled = !list.has_more;
}

async function refreshInventoryView() {
  invResultsEl.innerHTML = `<p class="loading">Loading…</p>`;
  const params = new URLSearchParams({ page: String(_invPage), limit: String(INV_PAGE_SIZE) });
  if (_invQ) params.set("q", _invQ);
  try {
    const [sum, list] = await Promise.all([
      api("/inventory/summary"),
      api(`/inventory?${params}`),
    ]);
    _renderInvSummary(sum);
    _renderInvList(list);
  } catch (err) {
    invResultsEl.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
  }
}

async function _postInventorySync(payload) {
  return _runInventorySync(() => fetch("/api/inventory/sync/deckbox", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }));
}

// Multipart upload — the CSV never becomes a JSON string, so a large export
// does not pay for JSON escaping, and the browser streams it.
async function _postInventoryUpload(file, mode) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("mode", mode);
  return _runInventorySync(() => fetch("/api/inventory/sync/upload", {
    method: "POST",
    body: fd,
  }));
}

async function _runInventorySync(doFetch) {
  invSyncBtn.disabled = true;
  invSyncStatusEl.innerHTML = `<p class="loading">Syncing…</p>`;
  try {
    const resp = await doFetch();
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      invSyncStatusEl.innerHTML = `<p class="state-error" role="alert">Sync failed: ${escapeHtml(body.details || resp.statusText)}</p>`;
      return;
    }
    const unmatched = body.unmatched || [];
    const warnings = body.warnings || [];
    const notes = [];
    if (unmatched.length) {
      notes.push(`<details class="inv-unmatched"><summary>${unmatched.length} name${unmatched.length === 1 ? "" : "s"} not found in the card database</summary><ul>${
        unmatched.map((n) => `<li>${escapeHtml(n)}</li>`).join("")
      }</ul></details>`);
    }
    if (warnings.length) {
      notes.push(`<details class="inv-unmatched"><summary>${warnings.length} CSV warning${warnings.length === 1 ? "" : "s"}</summary><ul>${
        warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")
      }</ul></details>`);
    }
    invSyncStatusEl.innerHTML =
      `<p class="inv-ok">Synced ${body.total_cards} card${body.total_cards === 1 ? "" : "s"} (${body.distinct_cards} unique), mode <code>${escapeHtml(body.mode)}</code>.</p>`
      + notes.join("");
    _invPage = 1;
    await loadOwnedOracleIds();
    await refreshInventoryView();
    _repaintOwnedBadges();
    refreshUploadsList();
  } catch (err) {
    invSyncStatusEl.innerHTML = `<p class="state-error" role="alert">Sync failed: ${escapeHtml(err.message || "network")}</p>`;
  } finally {
    invSyncBtn.disabled = false;
  }
}

if (invSyncFormEl) {
  invSyncFormEl.addEventListener("submit", async (e) => {
    e.preventDefault();
    const mode = invModeEl.value;
    const file = invCsvFileEl.files && invCsvFileEl.files[0];
    if (file) {
      await _postInventoryUpload(file, mode);
      return;
    }
    const pasted = invCsvTextEl ? invCsvTextEl.value.trim() : "";
    if (pasted) {
      await _postInventorySync({ csv_text: pasted, mode });
      return;
    }
    // The URL input is hidden until Bug.32 is fixed, so it is normally
    // absent. Read it defensively so unhiding the block is the only change
    // needed to bring the path back.
    const url = invDeckboxUrlEl ? invDeckboxUrlEl.value.trim() : "";
    if (!url) {
      invSyncStatusEl.innerHTML = `<p class="empty">Pick a CSV file or paste CSV text first.</p>`;
      return;
    }
    await _postInventorySync({ url, mode });
  });
}

if (invSearchFormEl) {
  invSearchFormEl.addEventListener("submit", (e) => {
    e.preventDefault();
    _invQ = invQEl.value.trim();
    _invPage = 1;
    refreshInventoryView();
  });
}

if (invPrevBtn) {
  invPrevBtn.addEventListener("click", () => {
    if (_invPage <= 1) return;
    _invPage -= 1;
    refreshInventoryView();
  });
}

if (invNextBtn) {
  invNextBtn.addEventListener("click", () => {
    _invPage += 1;
    refreshInventoryView();
  });
}

if (invClearBtn) {
  invClearBtn.addEventListener("click", async () => {
    if (!window.confirm("Delete every row in your inventory? This cannot be undone.")) return;
    try {
      const resp = await fetch("/api/inventory", { method: "DELETE" });
      if (!resp.ok) throw new Error("clear failed");
      invSyncStatusEl.innerHTML = `<p class="inv-ok">Inventory cleared.</p>`;
      _invPage = 1;
      await loadOwnedOracleIds();
      await refreshInventoryView();
      _repaintOwnedBadges();
    } catch (err) {
      invSyncStatusEl.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
    }
  });
}

if (invResultsEl && invDetailEl) {
  wireListNavigation(invResultsEl, invDetailEl);
  wireListNavigation(invDetailEl, invDetailEl);
  wireDetailClicks(invDetailEl);
}
if (wlResultsEl && wlDetailEl) {
  wireListNavigation(wlResultsEl, wlDetailEl);
  wireListNavigation(wlDetailEl, wlDetailEl);
  wireDetailClicks(wlDetailEl);
}

// Warm both badge sets at startup so the first search already carries badges.
loadOwnedOracleIds();
loadWantedOracleIds();

