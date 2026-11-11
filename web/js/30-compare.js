// D15.5 — deck compare tab. POSTs both textareas to /decks/compare and
// renders the diff (added / removed / qty deltas + slot deltas).
function _renderCompareCardList(title, list) {
  if (!list || list.length === 0) return "";
  const rows = list.map((c) => {
    const labs = Array.isArray(c.predicted_labels) && c.predicted_labels.length
      ? ` <span class="muted">[${escapeHtml(c.predicted_labels.join(", "))}]</span>`
      : "";
    return `<li data-id="${escapeHtml(c.id)}">${c.qty}× ${escapeHtml(c.name)}${labs}</li>`;
  }).join("");
  return `<section class="compare-section"><h3>${escapeHtml(title)} (${list.length})</h3><ul>${rows}</ul></section>`;
}

function _renderCompareChangedQty(list) {
  if (!list || list.length === 0) return "";
  const rows = list.map((c) => {
    const cls = c.delta > 0 ? "delta-pos" : c.delta < 0 ? "delta-neg" : "";
    const sign = c.delta > 0 ? `+${c.delta}` : String(c.delta);
    return `<li data-id="${escapeHtml(c.id)}">${escapeHtml(c.name)}: ${c.qty_a} → ${c.qty_b} <span class="${cls}">(${sign})</span></li>`;
  }).join("");
  return `<section class="compare-section"><h3>Qty changed (${list.length})</h3><ul>${rows}</ul></section>`;
}

function _renderCompareDeltas(title, deltas) {
  const entries = Object.entries(deltas).filter(([, v]) => v !== 0);
  if (entries.length === 0) return `<section class="compare-section"><h3>${escapeHtml(title)}</h3><p class="muted">No change.</p></section>`;
  const items = entries.map(([k, v]) => {
    const sign = v > 0 ? `+${v}` : String(v);
    const cls = v > 0 ? "delta-pos" : "delta-neg";
    return `<li>${escapeHtml(k.replace(/_/g, " "))}: <span class="${cls}">${sign}</span></li>`;
  }).join("");
  return `<section class="compare-section"><h3>${escapeHtml(title)}</h3><ul class="compare-deltas">${items}</ul></section>`;
}

function renderCompare(payload) {
  if (!payload || !payload.diff) {
    compareResultEl.innerHTML = `<p class="empty">Unexpected response.</p>`;
    return;
  }
  const d = payload.diff;
  compareResultEl.innerHTML = ""
    + _renderCompareCardList("Added (in B, not A)", d.added)
    + _renderCompareCardList("Removed (in A, not B)", d.removed)
    + _renderCompareChangedQty(d.changed_qty)
    + _renderCompareDeltas("Slot counts delta (B − A)", d.counts_delta)
    + _renderCompareDeltas("Curve delta", d.curve_delta)
    + _renderCompareDeltas("Pip delta", d.pips_delta);
}

if (compareForm) {
  compareForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const a = (compareAEl.value || "").trim();
    const b = (compareBEl.value || "").trim();
    if (!a || !b) {
      compareResultEl.innerHTML = `<p class="empty">Paste both decklists first.</p>`;
      return;
    }
    compareResultEl.innerHTML = `<p class="loading">Comparing…</p>`;
    try {
      const resp = await fetch("/api/decks/compare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ a, b }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        compareResultEl.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(body.details || resp.statusText)}</p>`;
        return;
      }
      renderCompare(body);
    } catch (err) {
      compareResultEl.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
    }
  });
}

// Surface tag-ingest state on first paint. If tags=0 mid-refresh, the
// banner pollers self-cancel when oracle_tags_count crosses zero.
pollOracleTagsStatus();

