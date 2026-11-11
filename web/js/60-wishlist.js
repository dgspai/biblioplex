// ---------------------------------------------------------------------------
// D18.11 — Wishlist tab. Mirrors the inventory tab; the wanted-vs-owned diff
// is the point (`owned_quantity` on each row lets us strike through cards the
// user has since acquired).
// ---------------------------------------------------------------------------

const WL_PAGE_SIZE = 50;

function _renderWlSummary(sum) {
  if (!sum) { wlSummaryEl.innerHTML = ""; return; }
  const parts = [
    `${formatInt(sum.total_cards)} wanted`,
    `${formatInt(sum.distinct_cards)} unique`,
    escapeHtml(formatUsd(sum.total_value_usd)),
  ];
  if (sum.owned_distinct_cards) parts.push(`${formatInt(sum.owned_distinct_cards)} owned`);
  wlSummaryEl.innerHTML = parts.filter(Boolean).join(" · ");
}

function _renderWlList(list) {
  const items = (list.data || []).map((r) => {
    const cardId = (r.card_raw && r.card_raw.id) || "";
    const have = r.owned_quantity > 0;
    const meta = [];
    if (r.type_line) meta.push(`<span>${escapeHtml(r.type_line)}</span>`);
    if (r.price_usd != null) meta.push(`<span>${escapeHtml(formatUsd(r.price_usd))}</span>`);
    if (have) meta.push(`<span class="wl-owned">already own ${r.owned_quantity}</span>`);
    return `<li class="${have ? "wl-have" : ""}" data-id="${escapeHtml(cardId)}" data-oracle="${escapeHtml(r.oracle_id || "")}" tabindex="0">
      <div class="name"><span class="inv-qty-badge">${r.quantity}×</span>${escapeHtml(r.card_name || "?")}${_ownedBadge(r.oracle_id)}</div>
      <div class="meta">${meta.join("")}</div>
    </li>`;
  }).join("");
  wlResultsEl.innerHTML = items
    ? `<ul class="cards">${items}</ul>`
    : (_wlQ
      ? emptyStateHtml("No matches", "No wishlist card matches that filter.")
      : emptyStateHtml("Your wishlist is empty",
          "Import a CSV export from Deckbox to track the cards you want.",
          { label: "Import wishlist", tab: "settings", focus: "#wlCsvFile" }));
  const total = list.total_cards ?? 0;
  wlPagerEl.hidden = total <= WL_PAGE_SIZE;
  wlPageInfoEl.textContent = pagerRange(_wlPage, WL_PAGE_SIZE, total);
  wlPrevBtn.disabled = _wlPage <= 1;
  wlNextBtn.disabled = !list.has_more;
}

async function refreshWishlistView() {
  wlResultsEl.innerHTML = `<p class="loading">Loading…</p>`;
  const params = new URLSearchParams({ page: String(_wlPage), limit: String(WL_PAGE_SIZE) });
  if (_wlQ) params.set("q", _wlQ);
  try {
    const [sum, list] = await Promise.all([
      api("/wishlist/summary"),
      api(`/wishlist?${params}`),
    ]);
    _renderWlSummary(sum);
    _renderWlList(list);
  } catch (err) {
    wlResultsEl.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
  }
}

async function _runWishlistSync(doFetch) {
  wlSyncBtn.disabled = true;
  wlSyncStatusEl.innerHTML = `<p class="loading">Syncing…</p>`;
  try {
    const resp = await doFetch();
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      wlSyncStatusEl.innerHTML = `<p class="state-error" role="alert">Sync failed: ${escapeHtml(body.details || resp.statusText)}</p>`;
      return;
    }
    const unmatched = body.unmatched || [];
    const notes = unmatched.length
      ? `<details class="inv-unmatched"><summary>${unmatched.length} name${unmatched.length === 1 ? "" : "s"} not found in the card database</summary><ul>${
          unmatched.map((n) => `<li>${escapeHtml(n)}</li>`).join("")
        }</ul></details>`
      : "";
    wlSyncStatusEl.innerHTML =
      `<p class="inv-ok">Synced ${body.total_cards} wanted card${body.total_cards === 1 ? "" : "s"} (${body.distinct_cards} unique), mode <code>${escapeHtml(body.mode)}</code>.</p>`
      + notes;
    _wlPage = 1;
    await loadWantedOracleIds();
    await refreshWishlistView();
    _repaintOwnedBadges();
    refreshUploadsList();
  } catch (err) {
    wlSyncStatusEl.innerHTML = `<p class="state-error" role="alert">Sync failed: ${escapeHtml(err.message || "network")}</p>`;
  } finally {
    wlSyncBtn.disabled = false;
  }
}

if (wlSyncFormEl) {
  wlSyncFormEl.addEventListener("submit", async (e) => {
    e.preventDefault();
    const mode = wlModeEl.value;
    const file = wlCsvFileEl.files && wlCsvFileEl.files[0];
    if (file) {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("mode", mode);
      await _runWishlistSync(() => fetch("/api/wishlist/sync/upload", { method: "POST", body: fd }));
      return;
    }
    const pasted = wlCsvTextEl ? wlCsvTextEl.value.trim() : "";
    if (!pasted) {
      wlSyncStatusEl.innerHTML = `<p class="empty">Pick a CSV file or paste CSV text first.</p>`;
      return;
    }
    await _runWishlistSync(() => fetch("/api/wishlist/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csv_text: pasted, mode }),
    }));
  });
}

if (wlSearchFormEl) {
  wlSearchFormEl.addEventListener("submit", (e) => {
    e.preventDefault();
    _wlQ = wlQEl.value.trim();
    _wlPage = 1;
    refreshWishlistView();
  });
}

if (wlPrevBtn) {
  wlPrevBtn.addEventListener("click", () => {
    if (_wlPage <= 1) return;
    _wlPage -= 1;
    refreshWishlistView();
  });
}

if (wlNextBtn) {
  wlNextBtn.addEventListener("click", () => {
    _wlPage += 1;
    refreshWishlistView();
  });
}

if (wlClearBtn) {
  wlClearBtn.addEventListener("click", async () => {
    if (!window.confirm("Delete every row on your wishlist? This cannot be undone.")) return;
    try {
      const resp = await fetch("/api/wishlist", { method: "DELETE" });
      if (!resp.ok) throw new Error("clear failed");
      wlSyncStatusEl.innerHTML = `<p class="inv-ok">Wishlist cleared.</p>`;
      _wlPage = 1;
      await loadWantedOracleIds();
      await refreshWishlistView();
      _repaintOwnedBadges();
    } catch (err) {
      wlSyncStatusEl.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
    }
  });
}

