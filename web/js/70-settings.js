// ---------------------------------------------------------------------------
// D18.18 — Settings tab. The CSV import forms and the refresh controls moved
// here from the Inventory / Wishlist tabs and the header menu; those tabs are
// now purely for browsing. The import-history list is new.
// ---------------------------------------------------------------------------

function _fmtBytes(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function _renderUploads(list) {
  const rows = (list.data || []).map((u) => {
    const when = new Date(u.uploaded_at).toLocaleString();
    const unmatched = u.unmatched_count
      ? `<span class="upload-warn">${u.unmatched_count} unmatched</span>`
      : "";
    return `<li class="upload-row">
      <div class="upload-main">
        <span class="upload-kind upload-kind-${escapeHtml(u.kind)}">${escapeHtml(u.kind)}</span>
        <span class="upload-name">${escapeHtml(u.filename)}</span>
        <span class="help">${escapeHtml(when)}</span>
      </div>
      <div class="upload-meta help">
        ${plural(u.total_cards, "card")} · ${u.distinct_cards} unique · ${escapeHtml(_fmtBytes(u.size_bytes))}
        · mode <code>${escapeHtml(u.mode)}</code> ${unmatched}
      </div>
      <div class="upload-actions">
        <a class="ext-link" href="/api/uploads/${encodeURIComponent(u.id)}/download">Download</a>
        <button type="button" class="btn btn--danger btn--sm upload-del" data-upload-id="${escapeHtml(u.id)}">Delete</button>
      </div>
    </li>`;
  }).join("");
  uploadsListEl.innerHTML = rows
    ? `<ul class="upload-list">${rows}</ul>`
    : `<p class="empty">No imports yet.</p>`;
}

async function refreshUploadsList() {
  if (!uploadsListEl) return;
  try {
    _renderUploads(await api("/uploads?limit=100"));
  } catch (err) {
    uploadsListEl.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
  }
}

if (uploadsListEl) {
  uploadsListEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button.upload-del[data-upload-id]");
    if (!btn) return;
    if (!window.confirm("Delete this file from the import history? The cards it imported stay.")) return;
    try {
      const resp = await fetch(`/api/uploads/${encodeURIComponent(btn.dataset.uploadId)}`, { method: "DELETE" });
      if (!resp.ok) throw new Error("delete failed");
      await refreshUploadsList();
    } catch (err) {
      uploadsListEl.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
    }
  });
}

