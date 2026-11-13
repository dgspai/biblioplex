// Pure helpers shared by app.js. Attached to window.bibliohelpers so the
// classic-script app.js can pick them up without a bundler. No DOM access,
// no side effects — keep this file trivially testable via test_helpers.html.
//
// Exports (all attached to window.bibliohelpers):
//   - appendPredicate(q, pred): token-aware AND-append with dedup.
//   - splitTypeLine(line): em-dash/`--` split → supertype/type/subtype tokens.
//   - bannerStateFromHealth(health): pure projection of a /health response onto
//     the next oracle-tags banner state (D10.5 — makes "should the timer run?"
//     a function of the response shape, no DOM, no timer).
//   - refreshProgress(refresh, order, nowMs): /health.refresh → per-stage rows
//     plus core-stage counts (coreDone/coreTotal/coreComplete).
//   - shouldRerunSearch(prevKind, nextKind), emptyResultsCopy(healthKind): first-run UX.
//   - urlStateFromSearch(search): parse `location.search` → {q, order, dir,
//     page, id} with field-level defaults (D9.7).
//   - urlStateToSearch(state): serialize state → `?q=…` string, omitting
//     defaults so a fresh load renders as a bare URL (D9.7).
//   - clampSplit(percent, min, max): clamp a pane-split percentage into the
//     legible range so neither pane fully collapses on drag (D9.9).
//   - cardFaceImages(card): list of {url, name} for every distinct face image
//     on a card — single-face cards return one entry, DFC/transform/MDFC two
//     (D9.10).
//   - relatedParts(card): `card.all_parts` filtered to entries whose `id` is
//     not the card itself — feeds the related-cards chip strip (D9.10).
//   - mergeFaceData(card, faceIdx): top-level card with `card_faces[faceIdx]`
//     fields overlaid — drives the click-a-face-to-see-its-text flow (D9.11).
//   - historyPush(list, q, max): dedup + LRU insert of `q` at the head of the
//     search-history list, capped at `max` (D13.8).
//   - toggleRole(labels, role): flip one role; plan is exclusive + the fallback.
//   - landRecommendation(landCount): {status, message} for the deck-view land
//     count vs the EDH baseline of ~37 (D13.9).

(function () {
  // Tokenize respecting double-quoted values: `kw:"Living Weapon"` is one token.
  const TOKEN_RE = /"[^"]*"|\S+/g;

  function appendPredicate(q, pred) {
    const base = (q || "").trim();
    const needle = (pred || "").trim();
    if (!needle) return base;
    const tokens = base.match(TOKEN_RE) || [];
    if (tokens.includes(needle)) return base;
    return base ? `${base} ${needle}` : needle;
  }

  function splitTypeLine(line) {
    if (!line) return [];
    const normalized = String(line).replace(/--/g, "—");
    const [left, right] = normalized.split("—");
    const parts = [];
    for (const side of [left, right]) {
      if (!side) continue;
      for (const tok of side.trim().split(/\s+/)) {
        if (tok) parts.push(tok);
      }
    }
    return parts;
  }

  // State machine (hidden / ingesting / missing / empty) over the /health response shape (§6.6):
  //   - "hidden"    → tags loaded (count > 0), OR table absent (count === null,
  //                   per-README pre-migration deployment marker), OR degenerate
  //                   body. Caller must clear any running timer + hide banner.
  //   - "ingesting" → refresh in flight and tags not yet visible. Caller starts
  //                   the poll timer if not already running and shows the
  //                   "ingesting" banner. Carries `jobId` when /health supplied it.
  //   - "background"→ refresh in flight but every core stage of the run is done;
  //                   only best-effort stages remain. Hide the banner, keep polling.
  //   - "empty"     → cards table has no rows and nothing is refreshing. Caller
  //                   shows the "Download card data" onboarding banner.
  //   - "missing"   → refresh NOT in flight and tags === 0 (refresh ended without
  //                   producing tags, OR no refresh was ever kicked). Caller MUST
  //                   clear the timer (the D10.5 bug-fix case) and show the
  //                   "no oracle tags loaded" banner.
  //
  // `null` count is treated as "hidden" rather than "missing" because the README
  // documents that as the pre-tag-migration deployment marker — telling the
  // operator to "trigger ingest" won't help until the schema is rebuilt.
  function bannerStateFromHealth(health) {
    const h = health || {};
    const tags = h.oracle_tags_count;
    const refresh = h.refresh || {};
    const refreshing = !!refresh.running;
    // F2: an empty cards table wins over the tags checks. `null` counts as
    // empty (the server probes past the pre-ANALYZE estimate); a missing key
    // is a degenerate body and stays "hidden".
    if (h.cards_count === 0 || h.cards_count === null) {
      if (!refreshing) return {kind: "empty"};
      const state = {kind: "ingesting"};
      if (refresh.job_id) state.jobId = refresh.job_id;
      return state;
    }
    if (refreshing) {
      // Core stages done (or none selected): cards are searchable and only
      // best-effort stages (tags, combos, ...) remain. No banner, but the
      // caller keeps polling so Settings can show their progress.
      const kind = refreshProgress(refresh, [], 0).coreComplete ? "background" : "ingesting";
      const state = {kind};
      if (refresh.job_id) state.jobId = refresh.job_id;
      return state;
    }
    if (typeof tags === "number" && tags > 0) return {kind: "hidden"};
    if (tags === 0) return {kind: "missing"};
    return {kind: "hidden"};
  }

  // F3 — shape /health.refresh into per-stage rows for the Settings block and
  // the banner. `order` is the stage list to show (pending = not started yet);
  // `nowMs` is injected so the function stays pure. seconds is null for a
  // pending stage, finish-start when finished, now-start while running.
  // Stages that must finish before search is useful. The rest (tags, combos,
  // deck_cards) are best-effort and keep running after cards are searchable.
  const CORE_STAGES = ["ingest", "embed", "hnsw", "classify"];

  function refreshProgress(refresh, order, nowMs) {
    const r = refresh || {};
    // The run's own stage list beats the Settings checkboxes (`order`), which
    // can differ from what is actually running (auto-refresh, another tab).
    const planned = r.planned_stages || [];
    if (planned.length) order = planned;
    const byName = new Map((r.stages || []).map((s) => [s.name, s]));
    const secs = (s) => {
      const end = s.finished_at ? Date.parse(s.finished_at) : nowMs;
      return Math.max(0, Math.round((end - Date.parse(s.started_at)) / 1000));
    };
    // Idle with no history → nothing to show (not a wall of "pending").
    const show = r.running || (r.stages || []).length > 0;
    const rows = (show ? order || [] : []).map((name) => {
      const s = byName.get(name);
      if (!s) return {name, status: "pending", rows: null, seconds: null, error: null};
      const status = s.error ? "error" : s.finished_at ? "done" : "running";
      return {name, status, rows: s.rows ?? null, seconds: secs(s), error: s.error || null};
    });
    const starts = (r.stages || []).map((s) => Date.parse(s.started_at));
    const ends = (r.stages || []).map((s) => (s.finished_at ? Date.parse(s.finished_at) : nowMs));
    const core = rows.filter((x) => CORE_STAGES.includes(x.name));
    return {
      rows,
      coreTotal: core.length,
      coreDone: core.filter((x) => x.status === "done").length,
      // Only judged when the server sent the plan; otherwise unknown = false.
      coreComplete: planned.length > 0
        && core.every((x) => x.status === "done" || x.status === "error"),
      done: rows.filter((x) => x.status === "done").length,
      running: rows.filter((x) => x.status === "running").map((x) => x.name),
      totalSeconds: starts.length
        ? Math.max(0, Math.round((Math.max(...ends) - Math.min(...starts)) / 1000))
        : null,
    };
  }

  // Re-run the visible search once cards become searchable: the previous probe
  // saw an empty/ingesting DB, so the screen may still say "No cards found".
  function shouldRerunSearch(prevKind, nextKind) {
    const loading = (k) => k === "ingesting" || k === "empty";
    return loading(prevKind) && !loading(nextKind);
  }

  // Which imported dataset a query's operators depend on: inventory, wishlist,
  // or null. Inventory wins when both appear (its import comes first in Settings).
  const _INV_RE = /(?:^|[\s(-])(?:owned[:<>=!]|in:inventory\b|is:(?:owned|unowned)\b)/i;
  const _WL_RE = /(?:^|[\s(-])(?:wanted[:<>=!]|in:wishlist\b|is:(?:wanted|unwanted)\b)/i;
  function ownershipScope(q) {
    if (_INV_RE.test(q || "")) return "inventory";
    if (_WL_RE.test(q || "")) return "wishlist";
    return null;
  }

  // Zero-results copy. `healthKind` is the last bannerStateFromHealth kind.
  function emptyResultsCopy(healthKind, q) {
    if (healthKind === "ingesting" || healthKind === "empty") {
      return {
        title: "Card data is still downloading",
        body: "Results appear here automatically once the first import finishes (progress is under Settings).",
      };
    }
    const scope = ownershipScope(q);
    if (scope) {
      return {
        title: "No matches",
        body: `This filter uses your ${scope}; import one in Settings.`,
        cta: scope === "inventory"
          ? {label: "Import inventory", tab: "settings", focus: "#invCsvFile"}
          : {label: "Import wishlist", tab: "settings", focus: "#wlCsvFile"},
      };
    }
    return {
      title: "No cards found",
      body: "Nothing matches that query. Loosen a filter, or check the search syntax.",
    };
  }

  // F16 — display formatting. `locale` is for tests; the app passes none so
  // the browser's own locale decides grouping.
  function formatInt(n, locale) {
    const v = Number(n);
    return new Intl.NumberFormat(locale, {maximumFractionDigits: 0}).format(Number.isFinite(v) ? v : 0);
  }
  function formatPct(n, locale) {
    if (n === null || n === undefined || !Number.isFinite(Number(n))) return "";
    return `${formatInt(n, locale)}%`;
  }
  // "$0.10", "$4,133.77"; null/non-numeric → "" (unknown is not $0.00).
  function formatUsd(n, locale) {
    if (n === null || n === undefined || n === "" || !Number.isFinite(Number(n))) return "";
    return "$" + new Intl.NumberFormat(locale, {minimumFractionDigits: 2, maximumFractionDigits: 2}).format(Number(n));
  }
  // Footer pager text shared by every list: "101–200 of 2,832".
  function pagerRange(page, size, total, locale) {
    const end = Math.min(total, page * size);
    return `${formatInt((page - 1) * size + 1, locale)}–${formatInt(end, locale)} of ${formatInt(total, locale)}`;
  }
  // plural(1, "card") → "1 card"; plural(2, "copy", "copies") → "2 copies".
  function plural(n, one, many, locale) {
    return `${formatInt(n, locale)} ${Number(n) === 1 ? one : (many || one + "s")}`;
  }
  // The bracket is a rough heuristic over the list; below ~20 cards it says
  // nothing, so show nothing. "≈" because "~4" reads as "-4".
  function bracketLabel(bracket, totalCards) {
    if (!bracket || !(Number(totalCards) >= 20)) return "";
    return `Bracket ≈${bracket} (est.)`;
  }

  // URL state contract (D9.7). Defaults match the form/router defaults so a
  // bare URL (no query string) parses to the same state the page boots into
  // before any user interaction. Serializer drops keys that match the default
  // so the URL bar only ever carries what the user actually changed.
  const _URL_DEFAULTS = { q: "", order: "name", dir: "auto", page: 1, id: null };

  function urlStateFromSearch(search) {
    const p = new URLSearchParams(search || "");
    const pageRaw = parseInt(p.get("page") || "", 10);
    return {
      q: p.get("q") || _URL_DEFAULTS.q,
      order: p.get("order") || _URL_DEFAULTS.order,
      dir: p.get("dir") || _URL_DEFAULTS.dir,
      page: Number.isFinite(pageRaw) && pageRaw >= 1 ? pageRaw : _URL_DEFAULTS.page,
      id: p.get("id") || _URL_DEFAULTS.id,
    };
  }

  function urlStateToSearch(state) {
    const s = state || {};
    const p = new URLSearchParams();
    if (s.q && s.q !== _URL_DEFAULTS.q) p.set("q", s.q);
    if (s.order && s.order !== _URL_DEFAULTS.order) p.set("order", s.order);
    if (s.dir && s.dir !== _URL_DEFAULTS.dir) p.set("dir", s.dir);
    if (typeof s.page === "number" && s.page > _URL_DEFAULTS.page) p.set("page", String(s.page));
    if (s.id) p.set("id", s.id);
    const str = p.toString();
    return str ? `?${str}` : "";
  }

  // Bound the split so a runaway drag (or a garbage localStorage value) can
  // never fully collapse a pane and strand the user with no way back. Garbage
  // input (NaN, non-finite, missing) → midpoint of [min, max]. Defaults: 15–85.
  function clampSplit(percent, min, max) {
    const lo = typeof min === "number" ? min : 15;
    const hi = typeof max === "number" ? max : 85;
    const p = Number(percent);
    if (!Number.isFinite(p)) return (lo + hi) / 2;
    return Math.max(lo, Math.min(hi, p));
  }

  // Walk `card_faces` for distinct image URLs, falling back to the top-level
  // `image_uris` for single-face cards. Per-face `image_uris` is the
  // transform/MDFC signature (Scryfall layouts: `transform`, `modal_dfc`,
  // `meld`, `reversible_card`). Split/flip/adventure share one art and
  // populate top-level `image_uris` only — those return one entry. Dedup
  // preserves order so the front face is always first.
  function cardFaceImages(card) {
    const c = card || {};
    const faces = Array.isArray(c.card_faces) ? c.card_faces : [];
    const out = [];
    const seen = new Set();
    for (const face of faces) {
      const url = face?.image_uris?.normal || face?.image_uris?.large || null;
      if (!url || seen.has(url)) continue;
      seen.add(url);
      out.push({ url, name: face.name || c.name || "" });
    }
    if (out.length === 0) {
      const url = c.image_uris?.normal || c.image_uris?.large || null;
      if (url) out.push({ url, name: c.name || "" });
    }
    return out;
  }

  function relatedParts(card) {
    const c = card || {};
    const parts = Array.isArray(c.all_parts) ? c.all_parts : [];
    return parts.filter((p) => p && p.id && p.id !== c.id);
  }

  // Overlay one face's per-face fields onto a copy of the top-level card so
  // the detail renderer can read "the active face" through the same keys it
  // reads single-face cards. Only the fields Scryfall populates per face are
  // overlaid (name/mana_cost/type_line/oracle_text/power/toughness/loyalty/
  // colors); top-level concerns (rarity, set, legalities, all_parts, prices,
  // edhrec_rank, …) stay on the merged object untouched. Out-of-range or
  // missing faceIdx returns the card unchanged so the renderer can call this
  // unconditionally on every card without a guard.
  const _FACE_FIELDS = [
    "name", "mana_cost", "type_line", "oracle_text",
    "power", "toughness", "loyalty", "colors",
  ];
  function mergeFaceData(card, faceIdx) {
    if (!card) return card;
    const faces = Array.isArray(card.card_faces) ? card.card_faces : [];
    const face = faces[faceIdx];
    if (!face) return card;
    const merged = Object.assign({}, card);
    for (const k of _FACE_FIELDS) {
      if (face[k] !== undefined && face[k] !== null && face[k] !== "") {
        merged[k] = face[k];
      }
    }
    return merged;
  }

  // D13.8 — search history dropdown. Pure LRU + dedup + cap. `list` is the
  // current history (head = most recent); returns a NEW array (immutable).
  // Empty/whitespace-only `q` is dropped (no history noise from Esc-clears).
  function historyPush(list, q, max) {
    const n = max == null ? NaN : Number(max);
    const cap = Math.max(1, Number.isFinite(n) ? n : 20);
    const query = String(q || "").trim();
    const src = Array.isArray(list) ? list : [];
    if (!query) return src.slice(0, cap);
    // Dedup case-insensitively: repeated searches for the same query, even
    // with different casing, should promote the single entry — not stack up.
    const lower = query.toLowerCase();
    const filtered = src.filter(
      (item) => typeof item === "string" && item.toLowerCase() !== lower
    );
    return [query, ...filtered].slice(0, cap);
  }

  // D15.4 — hover-preview state machine. Pure: takes a hover target and
  // returns the card `{id}` to preview, or `null` if the target isn't a
  // card-carrying element. Supports both `data-id` (result rows, suggest
  // rows) and `data-related-id` (related chips). If the target exposes
  // `closest()` (real DOM element), we walk up to find the nearest
  // ancestor with either attribute so `<span>` children of a `<li>` still
  // resolve. `data-no-hover` on any ancestor opts out (used for the
  // detail-pane's own header — no self-preview).
  function hoverPreviewShouldShow(target) {
    if (!target) return null;
    let el = target;
    if (typeof el.closest === "function") {
      el = el.closest(
        "[data-no-hover],[data-id],[data-related-id]"
      );
    }
    if (!el) return null;
    const ds = el.dataset || {};
    if ("noHover" in ds) return null;
    const id = ds.id || ds.relatedId;
    if (!id) return null;
    return { id: String(id) };
  }

  // D13.9 — recommended EDH land count. Commander deckbuilding consensus is
  // ~37 lands; below 33 or above 41 is worth flagging. `status` is one of
  // "ok" | "low" | "high" so the caller can tint accordingly.
  function landRecommendation(landCount) {
    const n = Number(landCount);
    if (!Number.isFinite(n) || n < 0) {
      return { status: "ok", message: "Lands: 0 / ~37 recommended" };
    }
    let status = "ok";
    if (n < 33) status = "low";
    else if (n > 41) status = "high";
    return { status, message: `Lands: ${n} / ~37 recommended` };
  }

  // Flip one role on a card's label set. `plan` is the classifier's residue
  // role: exclusive with the functional ones, and what a card falls back to.
  function toggleRole(labels, role) {
    const cur = labels || [];
    if (cur.includes(role)) {
      const rest = cur.filter((l) => l !== role);
      return rest.length ? rest : ["plan"];
    }
    return role === "plan" ? ["plan"] : [...cur.filter((l) => l !== "plan"), role];
  }

  window.bibliohelpers = {
    appendPredicate, splitTypeLine, bannerStateFromHealth,
    urlStateFromSearch, urlStateToSearch, clampSplit,
    cardFaceImages, relatedParts, mergeFaceData,
    historyPush, landRecommendation, hoverPreviewShouldShow,
    refreshProgress, formatInt, formatPct, formatUsd, pagerRange, plural, bracketLabel,
    shouldRerunSearch, emptyResultsCopy, toggleRole,
  };
})();
