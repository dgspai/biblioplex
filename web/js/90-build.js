// ======================= Build wizard ===================================
//
// A guided, human-driven walk of docs/edh-guide.md's 7-step framework over
// a mutable decklist held in localStorage. Every step calls an EXISTING
// endpoint — no new server surface. Both entry paths (start empty / paste a
// list) feed one deck state. Step 7 reuses the evaluate_deck grading via
// POST-shaped analyze; the tips engine itself lives server-side.

const BUILD_KEY = "bx.build.v1";
const BUILD_STEPS = [
  { title: "Commander", blurb: "The one known variable in a 100-card singleton library. Pick a leader (or paste a list to tune) — its text box dictates the 99." },
  { title: "Keyword analysis", blurb: "Break the commander's text into search terms and solve the 99 for each. Click a tag to pull cards that serve it." },
  { title: "Card advantage", blurb: "12 NET-POSITIVE draw pieces — cantrips that replace themselves don't count. Velocity finds your lands and answers." },
  { title: "Ramp", blurb: "10-12 pieces, 2-mana priority, to deploy multiple spells per turn cycle." },
  { title: "Interaction", blurb: "12 targeted + 6 mass. Disrupt opponents before they reach their win conditions." },
  { title: "Lands & cutting", blurb: "Secure the 38-land mandate, shape the curve toward an MV2 peak, and cut to exactly 100." },
  { title: "Review", blurb: "The whole build at a glance: goldfish the turn-7 test, check the colour balance, resolve anything the tally couldn't, then save. Click a slot to split the decklist into what fills it and what doesn't." },
];
// The final step doubles as the turn-7 goldfish test and the read-only review.
const _REVIEW_STEP = BUILD_STEPS.length;
const BUILD_STEP_SLOTS = { 3: ["card_advantage"], 4: ["ramp"], 5: ["targeted_disrupt", "mass_disrupt"] };
// Search projection for wizard card rows — base cols plus usd price so
// rows can show cmc + $ (suggest/manabase already return full cards).
const _BUILD_LIST_PROJ = LIST_PROJECTION_BASE + ",prices.usd,oracle_text";

// A land counts as ramp only if it makes extra mana or puts more lands on
// the battlefield — plain fixing/utility lands (Command Tower, Reliquary
// Tower) do not, even though the classifier tags them "ramp".
function _isLandType(typeLine) {
  return /\bland\b/i.test(typeLine || "");
}
function _landRamps(c) {
  const o = (c.oracle_text || "").toLowerCase();
  if (!o) return false;
  if (o.includes("onto the battlefield") && o.includes("land")) return true; // fetches/plays extra lands
  if (/\{[wubrgc]\}\{[wubrgc]\}/.test(o)) return true;                        // adds 2 adjacent mana (Ancient Tomb)
  if (/add (two|three|four|five)\b/.test(o)) return true;                     // "add two mana"
  if (/add \{[wubrgc]\} for each/.test(o)) return true;                       // mana per permanent (Cabal Coffers)
  return false;
}
// Deck focus: _buildOtagSel is the PRIORITISED themes (persist across
// steps, bias every search); _buildFocusPool is the candidate chip pool —
// the commander's tags plus tags of cards added in step 1; _buildFocusNew
// flags freshly-surfaced tags (from a just-added card) so they stand out
// as suggestions. _buildCmdTags/_For cache the commander's own tags.
let _buildOtagSel = new Set();
let _buildFocusPool = new Set();
let _buildFocusNew = new Set();
let _buildCmdTags = [];
let _buildCmdTagsFor = null;
// {tag: count of deck cards carrying it} — shown on each theme chip.
let _buildTagCounts = {};
let _buildBracket = null;  // WOTC 1-5 power bracket estimate
let _buildActiveSlot = null;  // step-8 review: slot whose cards are lit in the decklist
// nameLower -> {type_line, cmc, mana_cost, id}; feeds the decklist sidebar.
const _cardMetaCache = {};

let _build = _loadBuild();
let _buildAnalysis = null;
let _buildCmdCard = null;     // resolved commander card (id + color_identity)
let _buildAnalyzeTimer = null;

function _loadBuild() {
  try {
    const s = JSON.parse(localStorage.getItem(BUILD_KEY));
    if (s && typeof s === "object" && s.deck) return s;
  } catch (_e) { /* ignore */ }
  return { step: 1, commander: "", deck: {} };
}
function _saveBuild() {
  try { localStorage.setItem(BUILD_KEY, JSON.stringify(_build)); } catch (_e) { /* ignore */ }
}
function _buildDecklist() {
  const lines = [];
  if (_build.commander) lines.push("Commander", "1 " + _build.commander, "");
  lines.push("Deck");
  for (const name of Object.keys(_build.deck)) lines.push(_build.deck[name] + " " + name);
  // Sideboard from a loaded saved deck rides along untouched (not editable here).
  const side = _build.sideboard || {};
  if (Object.keys(side).length) {
    lines.push("", "Sideboard");
    for (const name of Object.keys(side)) lines.push(side[name] + " " + name);
  }
  return lines.join("\n");
}
function _buildCount() {
  let n = _build.commander ? 1 : 0;
  for (const name of Object.keys(_build.deck)) n += _build.deck[name];
  return n;
}
function _buildHas(name) {
  return _build.commander === name || Object.prototype.hasOwnProperty.call(_build.deck, name);
}
function _buildAdd(name) {
  if (!name || name === _build.commander) return;
  _build.deck[name] = (_build.deck[name] || 0) + 1;
  _saveBuild();
  _scheduleBuildAnalyze();
}
function _buildRemove(name) {
  delete _build.deck[name];
  _saveBuild();
  _scheduleBuildAnalyze();
}
function _buildRemoveOne(name) {
  if (!_build.deck[name]) return;
  _build.deck[name] -= 1;
  if (_build.deck[name] <= 0) delete _build.deck[name];
  _saveBuild();
  _scheduleBuildAnalyze();
}

// Basic-land handling for step 6. `_pipBasicAllocation(n)` splits n basic
// slots across the deck's colored-pip distribution (from analyze `pips`),
// largest-remainder rounded so the parts sum to exactly n; falls back to
// the commander's color identity, then to nothing for a colorless deck.
const _BASIC_FOR = { W: "Plains", U: "Island", B: "Swamp", R: "Mountain", G: "Forest", C: "Wastes" };
const _BASIC_NAMES = new Set(Object.values(_BASIC_FOR));

function _pipBasicAllocation(n) {
  if (n <= 0) return {};
  const pips = (_buildAnalysis && _buildAnalysis.pips) || {};
  let weights = ["W", "U", "B", "R", "G"].map((c) => [c, pips[c] || 0]).filter(([, v]) => v > 0);
  let total = weights.reduce((s, [, v]) => s + v, 0);
  if (total === 0) {
    // No colored pips yet — split evenly over the commander's identity.
    const ci = (_buildCmdCard && _buildCmdCard.color_identity) || [];
    if (!ci.length) return {};
    weights = ci.map((c) => [c, 1]);
    total = ci.length;
  }
  const raw = weights.map(([c, v]) => [c, (n * v) / total]);
  const alloc = raw.map(([c, x]) => [c, Math.floor(x)]);
  let used = alloc.reduce((s, [, x]) => s + x, 0);
  // Hand out the remaining slots by largest fractional remainder.
  const order = raw
    .map(([, x], i) => [i, x - Math.floor(x)])
    .sort((a, b) => b[1] - a[1]);
  let k = 0;
  while (used < n) { alloc[order[k % order.length][0]][1] += 1; used += 1; k += 1; }
  const out = {};
  for (const [c, x] of alloc) if (x > 0) out[_BASIC_FOR[c]] = x;
  return out;
}

const _COLOR_OF_BASIC = { Plains: "W", Island: "U", Swamp: "B", Mountain: "R", Forest: "G", Wastes: "C" };

// Which basic to cut when swapping a nonbasic in. Two considerations:
//   1. Colours the incoming land provides (`produces`, WUBRG letters) — a
//      dual/tri replaces a source of a colour it actually taps for, so
//      only basics of those colours are candidates. A land that provides
//      none of the deck's basic colours (colourless, or a fetch with no
//      produced_mana) leaves every basic a candidate.
//   2. Among the candidates, the one most OVER its pip-proportional target
//      for the reduced basic count, so the basics stay in the deck's
//      colored-pip proportion rather than just shaving the biggest pile.
// Null when no basics are left.
function _pickBasicToRemove(produces) {
  const basics = Object.keys(_build.deck).filter((n) => _BASIC_NAMES.has(n));
  if (!basics.length) return null;
  const totalBasics = basics.reduce((s, n) => s + _build.deck[n], 0);
  const target = _pipBasicAllocation(Math.max(0, totalBasics - 1));
  let pool = basics;
  if (produces) {
    const covered = new Set(produces.split(""));
    const restricted = basics.filter((n) => covered.has(_COLOR_OF_BASIC[n]));
    if (restricted.length) pool = restricted;
  }
  let victim = null;
  let worst = -Infinity;
  for (const n of pool) {
    const over = _build.deck[n] - (target[n] || 0);
    if (over > worst) { worst = over; victim = n; }
  }
  return victim;
}

// Swap a nonbasic in and pull one basic (chosen by _pickBasicToRemove,
// respecting the land's produced colours) to hold the land count steady.
// Returns the basic removed (or null if none).
function _buildSwapIn(name, produces) {
  const victim = _pickBasicToRemove(produces);
  _build.deck[name] = (_build.deck[name] || 0) + 1;
  if (victim) {
    _build.deck[victim] -= 1;
    if (_build.deck[victim] <= 0) delete _build.deck[victim];
  }
  _saveBuild();
  _scheduleBuildAnalyze();
  return victim;
}

// Count the deck's mana SOURCES by colour (what it can produce), to sit
// beside the pip chart (what it needs). Basics are known locally; every
// other card's produced_mana is fetched once via /cards/collection — so
// rocks/dorks/duals all count, and a rainbow land counts for every colour
// it makes. Fetches (no produced_mana) contribute nothing, since they only
// find a basic indirectly.
async function _buildManaSources() {
  const counts = { W: 0, U: 0, B: 0, R: 0, G: 0, C: 0 };
  const qmap = {};
  for (const n of Object.keys(_build.deck)) qmap[n.toLowerCase()] = _build.deck[n];
  if (_build.commander) qmap[_build.commander.toLowerCase()] = (qmap[_build.commander.toLowerCase()] || 0) + 1;
  // Basics locally.
  for (const n of Object.keys(_build.deck)) {
    if (_BASIC_NAMES.has(n)) counts[_COLOR_OF_BASIC[n]] += _build.deck[n];
  }
  // Everything else via one collection lookup (chunked at 75).
  const nonBasic = Object.keys(_build.deck).filter((n) => !_BASIC_NAMES.has(n));
  if (_build.commander) nonBasic.push(_build.commander);
  for (let i = 0; i < nonBasic.length; i += 75) {
    const chunk = nonBasic.slice(i, i + 75);
    let body;
    try {
      body = await _buildPost("/cards/collection", { identifiers: chunk.map((n) => ({ name: n })) });
    } catch (_e) { continue; }
    for (const card of (body.data || [])) {
      const q = qmap[(card.name || "").toLowerCase()] || 0;
      let prod = card.produced_mana || [];
      if (!prod.length && Array.isArray(card.card_faces)) {
        prod = card.card_faces.flatMap((f) => f.produced_mana || []);
      }
      for (const c of prod) if (counts[c] !== undefined) counts[c] += q;
    }
  }
  return counts;
}

// One WUBRG radar overlaying colour NEED (pips) and HAVE (sources). Each
// series is normalised to its OWN max, so the two polygons compare by
// SHAPE — a colour where "have" bulges past "need" is over-sourced. The
// raw need/have counts sit at each axis tip so absolute values survive.
function _renderManaRadar(pips, sources) {
  const axes = ["W", "U", "B", "R", "G"];
  const need = axes.map((k) => pips[k] || 0);
  const have = axes.map((k) => sources[k] || 0);
  const needMax = Math.max(1, ...need);
  const haveMax = Math.max(1, ...have);
  const needSum = need.reduce((s, v) => s + v, 0);
  const haveSum = have.reduce((s, v) => s + v, 0);
  const pct = (v, sum) => (sum ? Math.round((v / sum) * 100) : 0);
  const cx = 120, cy = 120, R = 78, n = axes.length;
  const pt = (i, frac) => {
    const ang = -Math.PI / 2 + i * 2 * Math.PI / n;
    return [cx + R * frac * Math.cos(ang), cy + R * frac * Math.sin(ang)];
  };
  const poly = (vals, max, cls) => {
    const p = vals.map((v, i) => pt(i, v / max).map((z) => z.toFixed(1)).join(",")).join(" ");
    return `<polygon points="${p}" class="${cls}"/>`;
  };
  const rings = [0.5, 1].map((f) =>
    `<polygon points="${axes.map((_, i) => pt(i, f).map((z) => z.toFixed(1)).join(",")).join(" ")}" class="radar-grid"/>`
  ).join("");
  const spokes = axes.map((_, i) => {
    const [x, y] = pt(i, 1);
    return `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" class="radar-grid"/>`;
  }).join("");
  const labels = axes.map((k, i) => {
    const [x, y] = pt(i, 1.22);
    const sym = symbolMap["{" + k + "}"];
    const glyph = sym
      ? `<image href="${sym.svg}" x="${(x - 7).toFixed(1)}" y="${(y - 15).toFixed(1)}" width="14" height="14"/>`
      : `<text x="${x.toFixed(1)}" y="${(y - 3).toFixed(1)}" text-anchor="middle" class="radar-lbl">${k}</text>`;
    // Labels show each colour's SHARE of its series; hover gives the counts.
    const tip = `${k} — need ${need[i]} (${pct(need[i], needSum)}%), have ${have[i]} (${pct(have[i], haveSum)}%)`;
    return `<g><title>${escapeHtml(tip)}</title>${glyph}<text x="${x.toFixed(1)}" y="${(y + 9).toFixed(1)}" text-anchor="middle" class="radar-val">${pct(need[i], needSum)}%/${pct(have[i], haveSum)}%</text></g>`;
  }).join("");
  return `<div class="build-radar">
    <svg viewBox="0 0 240 240" width="220" height="220" role="img" aria-label="Colour need versus sources radar">
      ${rings}${spokes}
      ${poly(have, haveMax, "radar-have")}
      ${poly(need, needMax, "radar-need")}
      ${labels}
    </svg>
    <div class="radar-legend">
      <span class="radar-key radar-need-key">Need (pips)</span>
      <span class="radar-key radar-have-key">Have (sources)</span>
      <span class="help">% share per colour</span>
    </div>
  </div>`;
}

async function _buildPost(path, body) {
  const res = await fetch("/api" + path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = res.status + "";
    try { msg = (await res.json()).details || msg; } catch (_e) { /* ignore */ }
    throw new Error(msg);
  }
  return res.json();
}

function _scheduleBuildAnalyze() {
  if (_buildAnalyzeTimer) clearTimeout(_buildAnalyzeTimer);
  _buildAnalyzeTimer = setTimeout(_buildAnalyze, 250);
}
async function _buildAnalyze() {
  try {
    _buildAnalysis = await _buildPost("/decks/analyze", { decklist: _buildDecklist() });
  } catch (_e) { _buildAnalysis = null; }
  // Scoreboard only. Re-rendering the step body here would clobber
  // in-progress form input (commander name, paste box, save name) when
  // the debounce lands mid-edit; steps re-render themselves on add/remove.
  try {
    const b = await _buildPost("/decks/bracket", { decklist: _buildDecklist() });
    _buildBracket = b.bracket;
  } catch (_e) { _buildBracket = null; }
  _renderBuildScoreboard();
  _refreshSlotGoals();
  // Refresh only the review step's analysis-driven summary (colour balance,
  // unresolved, ownership) when a later analyze lands — cheap and it avoids
  // re-firing the step's heavy goldfish/combos fetch.
  if (_build.step === _REVIEW_STEP) _renderReviewSummary();
  await _refreshTagCounts();
  await _renderDeckPanel();
}

// How many deck cards carry each tag — one aggregate query — then refresh
// the theme chips so each shows its deck count.
async function _refreshTagCounts() {
  const names = Object.keys(_build.deck);
  if (_build.commander) names.push(_build.commander);
  if (!names.length) { _buildTagCounts = {}; }
  else {
    try {
      const res = await _buildPost("/cards/tag-counts", { names });
      _buildTagCounts = res.counts || {};
    } catch (_e) { _buildTagCounts = {}; }
  }
  await _renderThemeChips();
}

// Fetch type_line/cmc for any deck cards not yet cached (one collection
// call, chunked). Metadata is immutable per card, so the cache never
// invalidates.
async function _ensureCardMeta(names) {
  const need = [...new Set(names.map((n) => n.toLowerCase()))].filter((n) => !_cardMetaCache[n]);
  for (let i = 0; i < need.length; i += 75) {
    const chunk = need.slice(i, i + 75);
    try {
      const res = await _buildPost("/cards/collection", { identifiers: chunk.map((n) => ({ name: n })) });
      for (const c of (res.data || [])) {
        _cardMetaCache[(c.name || "").toLowerCase()] = {
          type_line: c.type_line, cmc: c.cmc, mana_cost: c.mana_cost, id: c.id,
          oracle_id: c.oracle_id,
        };
      }
    } catch (_e) { /* leave uncached */ }
  }
}

// Display order + assignment precedence for the decklist sidebar. Lands
// win over creature (Dryad Arbor is a land); artifact/enchantment creatures
// sit under Creatures.
const _DECK_TYPE_ORDER = [
  "Commander", "Creatures", "Planeswalkers", "Instants", "Sorceries",
  "Artifacts", "Enchantments", "Battles", "Lands", "Other",
];
function _deckBucket(name) {
  if (name === _build.commander) return "Commander";
  const tl = ((_cardMetaCache[name.toLowerCase()] || {}).type_line || "").toLowerCase();
  if (tl.includes("land")) return "Lands";
  if (tl.includes("creature")) return "Creatures";
  if (tl.includes("planeswalker")) return "Planeswalkers";
  if (tl.includes("instant")) return "Instants";
  if (tl.includes("sorcery")) return "Sorceries";
  if (tl.includes("artifact")) return "Artifacts";
  if (tl.includes("enchantment")) return "Enchantments";
  if (tl.includes("battle")) return "Battles";
  return "Other";
}

// One decklist row. `mark` is "hit"/"dim"/"" — the in-place emphasis used on
// the build steps (dim what the current slot doesn't touch); it's left ""
// inside the review step's relevant/not-relevant split, where the section
// itself already says which is which.
function _dlRow(e, mark) {
  const cost = e.meta.mana_cost ? renderSymbols(e.meta.mana_cost) : "";
  const rm = e.name === _build.commander ? ""
    : `<button type="button" class="build-dl-rm" data-build-basic-minus="${escapeHtml(e.name)}" title="Remove one">×</button>`;
  const cls = mark ? ` ${mark === "hit" ? "slot-hit" : "dim"}` : "";
  // On the review step a resolved row can be dragged onto a slot chip to
  // recategorise the card (drag = move to that slot; see _highlightDeckSlot's
  // neighbours). Non-review steps have no drop target, so no drag affordance.
  const drag = _build.step === _REVIEW_STEP && e.meta.id
    ? ` draggable="true" title="Drag onto a slot to recategorise"`
    : "";
  const dcls = drag ? " build-dl-draggable" : "";
  return `<div class="build-dl-row${cls}${dcls}" data-id="${escapeHtml(e.meta.id || "")}" data-name="${escapeHtml(e.name.toLowerCase())}" data-preview=""${drag}>
    <span class="build-dl-qty">${e.qty}</span>
    <span class="build-dl-name">${escapeHtml(e.name)}${_ownedBadge(e.meta.oracle_id)}</span>
    <span class="build-dl-cost">${cost}</span>${rm}</div>`;
}

// Type-grouped (Creatures / Lands / …) block for a set of entries. `markFn`
// returns the per-row emphasis, or null for none.
function _dlTypeGroups(entries, markFn) {
  const groups = {};
  for (const label of _DECK_TYPE_ORDER) groups[label] = [];
  for (const e of entries) groups[_deckBucket(e.name)].push(e);
  let html = "";
  for (const label of _DECK_TYPE_ORDER) {
    const list = groups[label];
    if (!list.length) continue;
    list.sort((a, b) => (a.meta.cmc ?? 0) - (b.meta.cmc ?? 0) || a.name.localeCompare(b.name));
    const n = list.reduce((s, e) => s + (e.qty || 1), 0);
    html += `<div class="build-dl-group"><div class="build-dl-title">${label} · ${n}</div>`
      + list.map((e) => _dlRow(e, markFn ? markFn(e) : null)).join("") + `</div>`;
  }
  return html;
}

async function _renderDeckPanel() {
  const el = $("#buildDeckList");
  if (!el) return;
  await symbolsReady;  // so mana costs render as glyphs, not {2}{R} text
  const names = Object.keys(_build.deck);
  if (_build.commander) names.push(_build.commander);
  if (!names.length) { el.innerHTML = `<div class="build-decklist-head">Deck · 0</div><p class="help">No cards yet.</p>`; return; }
  await _ensureCardMeta(names);
  const entries = names.map((name) => ({
    name,
    qty: name === _build.commander ? 1 : _build.deck[name],
    meta: _cardMetaCache[name.toLowerCase()] || {},
  }));
  const byName = {};
  for (const c of (_buildAnalysis && _buildAnalysis.cards) || []) byName[c.name.toLowerCase()] = c;
  const focus = _panelFocusSlots();
  const fills = (e) => _cardFillsSlots(e.name.toLowerCase(), focus, byName);

  let html = `<div class="build-decklist-head">Deck · ${_buildCount()}</div>`;
  if (_build.step === _REVIEW_STEP && _buildActiveSlot) {
    // Split: the slot's cards up top, the rest below — each still by type.
    const inSlot = entries.filter(fills);
    const rest = entries.filter((e) => !fills(e));
    const lbl = escapeHtml(_buildActiveSlot.replace(/_/g, " "));
    const qsum = (list) => list.reduce((s, e) => s + (e.qty || 1), 0);
    html += `<div class="build-dl-section"><div class="build-dl-section-title">${lbl} · ${qsum(inSlot)}</div>${_dlTypeGroups(inSlot, null)}</div>`
      + `<div class="build-dl-section build-dl-section-rest"><div class="build-dl-section-title">everything else · ${qsum(rest)}</div>${_dlTypeGroups(rest, null)}</div>`;
  } else {
    // Build steps: dim what the current step's slot doesn't touch.
    html += _dlTypeGroups(entries, focus ? (e) => (fills(e) ? "hit" : "dim") : null);
  }
  el.innerHTML = html;
}

function _renderBuildScoreboard() {
  const el = $("#buildScoreboard");
  if (!el) return;
  if (!_buildAnalysis) { el.innerHTML = ""; return; }
  const a = _buildAnalysis;
  const lands = (a.counts && a.counts.land) || 0;
  const bl = bracketLabel(_buildBracket, _buildCount());
  const bracket = bl ? ` · <span title="Rough estimate of the 1-5 power bracket">${bl}</span>` : "";
  el.innerHTML = `
    <p class="help">${plural(_buildCount(), "card")} (incl. commander) · ${lands} lands / 38${_build.commander ? " · " + escapeHtml(_build.commander) : ""}${bracket}</p>
    ${_renderDeckSlots(a, _build.step === _REVIEW_STEP)}
    ${_renderDeckCurve(a.curve, _CURVE_TARGET_MAP)}`;
}

// One suggestion/search row with an add (or remove) control.
function _buildCardRows(cards) {
  if (!cards || !cards.length) return `<p class="empty">Nothing to suggest.</p>`;
  return `<ul class="build-cards">${cards.map((c) => {
    const labs = (c.predicted_labels || []).map((l) => l.replace(/_/g, " ")).join(", ");
    const cost = c.mana_cost ? renderSymbols(c.mana_cost) : "";
    const usd = c.prices && c.prices.usd ? formatUsd(c.prices.usd) : "";
    // Cost is trusted symbol HTML (renderSymbols escapes its own text), so
    // keep it out of the escaped text join and prepend it.
    const textMeta = [c.type_line, labs, usd].filter(Boolean).map(escapeHtml).join(" · ");
    const meta = [cost, textMeta].filter(Boolean).join(" · ");
    const inDeck = _buildHas(c.name);
    const btn = inDeck
      ? `<button type="button" class="build-rm" data-build-remove="${escapeHtml(c.name)}">✓ in deck — remove</button>`
      : `<button type="button" class="build-add" data-build-add="${escapeHtml(c.name)}">+ add</button>`;
    return `<li data-id="${escapeHtml(c.id || "")}" data-preview="">
      <span class="build-card-name">${escapeHtml(c.name)}</span>
      <span class="help">${meta}</span>
      ${btn}</li>`;
  }).join("")}</ul>`;
}

// A collapsed "find your own card" panel embedded in a step. `slotPred` is
// the step's recommended filter (e.g. "pred:ramp", "t:land") — offered as a
// toggle so search complements the auto-suggestions. Results reuse the
// +add rows, so adding from here behaves like adding a suggestion.
function _buildSearchPanelHtml(slotPred, open) {
  const slotChk = slotPred
    ? `<label><input type="checkbox" id="bsSlot" checked> This slot only</label>`
    : "";
  const summary = open ? "Recommendations" : "Search cards to add";
  return `<details class="build-search"${open ? " open" : ""}><summary>${summary}</summary>
    <div class="build-search-body" data-slot="${escapeHtml(slotPred || "")}">
      <div id="buildThemes" class="build-themes"></div>
      <input type="text" id="bsQ" spellcheck="false" autocapitalize="off" autocomplete="off" placeholder="name, or DSL — e.g. o:&quot;draw a card&quot; cmc&lt;=2">
      <div class="build-search-opts">
        <label><input type="checkbox" id="bsLegal" checked> Legal for commander</label>
        ${slotChk}
        <label><input type="checkbox" id="bsHide" checked> Hide cards in deck</label>
        <label><input type="checkbox" id="bsOwned"> Owned only</label>
        <label>Sort
          <select id="bsSort">
            <option value="edhrec">EDHREC rank</option>
            <option value="name">name</option>
            <option value="usd">price</option>
            <option value="released">newest</option>
            <option value="relevance">best match</option>
          </select>
        </label>
        <button type="button" id="bsGo" class="btn btn--primary">Search</button>
      </div>
      <div id="bsQueryEcho" class="help build-query-echo"></div>
      <div id="bsResults"></div>
    </div>
  </details>`;
}

async function _runBuildSearch() {
  const body = $(".build-search-body");
  const out = $("#bsResults");
  if (!body || !out) return;
  const slotPred = body.dataset.slot || "";
  const parts = [];
  const q = ($("#bsQ").value || "").trim();
  if (q) parts.push(q);
  if ($("#bsLegal") && $("#bsLegal").checked) {
    parts.push("f:commander");
    if (_build.commander && (!_buildCmdCard || _buildCmdCard.name !== _build.commander)) {
      _buildCmdCard = await api(`/cards/named?exact=${encodeURIComponent(_build.commander)}`).catch(() => null);
    }
    const ci = (_buildCmdCard && _buildCmdCard.color_identity || []).join("").toLowerCase();
    // Empty identity = colourless commander: `id<=c` (cardinality 0), not "no filter".
    if (_buildCmdCard) parts.push(ci ? `id<=${ci}` : "id<=c");
  }
  const slotChk = $("#bsSlot");
  if (slotPred && (!slotChk || slotChk.checked)) parts.push(slotPred);
  // Selected commander themes (union) bias the search toward on-theme cards.
  if (_buildOtagSel.size) {
    const tags = Array.from(_buildOtagSel).map((t) => `otag:"${t}"`);
    parts.push(tags.length > 1 ? `(${tags.join(" or ")})` : tags[0]);
  }
  if ($("#bsOwned") && $("#bsOwned").checked) parts.push("is:owned");
  if (!parts.length) { out.innerHTML = `<p class="empty">Type a search or enable a filter.</p>`; return; }
  const sort = ($("#bsSort") && $("#bsSort").value) || "edhrec";
  // Ramp step: a land only counts as ramp if it makes extra mana or plays
  // extra lands — drop plain fixing/utility lands the classifier tags ramp.
  const rampMode = slotPred === "pred:ramp" && (!slotChk || slotChk.checked);
  const echo = $("#bsQueryEcho");
  if (echo) {
    echo.textContent = `filter: ${parts.join(" ")}${rampMode ? " (ramp lands only)" : ""}  ·  sort: ${sort}`;
  }
  out.innerHTML = `<p class="empty">Searching…</p>`;
  try {
    // Over-fetch so client-side filters (hide-in-deck, ramp-land) still
    // leave a full page.
    const res = await api(`/cards/search?q=${encodeURIComponent(parts.join(" "))}&order=${sort}&limit=40&projection=${encodeURIComponent(_BUILD_LIST_PROJ)}`);
    let cards = res.data || [];
    if ($("#bsHide") && $("#bsHide").checked) cards = cards.filter((c) => !_buildHas(c.name));
    if (rampMode) cards = cards.filter((c) => !_isLandType(c.type_line) || _landRamps(c));
    out.innerHTML = _buildCardRows(cards.slice(0, 20));
  } catch (err) {
    out.innerHTML = `<p class="empty">${escapeHtml(err.message || "no matches")}</p>`;
  }
}

// Parse a plaintext decklist body into a {name: qty} map, skipping the
// section headings. Shared by the paste box and the saved-deck loader.
function _parseListToDeck(text) {
  const deck = {};
  for (const line of (text || "").split("\n")) {
    const m = line.trim().match(/^(\d+)\s*x?\s+(.+)$/i);
    if (m && !/^(commander|deck|mainboard|sideboard|companion|about|maybeboard|tokens)$/i.test(m[2].trim())) {
      const nm = m[2].trim();
      deck[nm] = (deck[nm] || 0) + parseInt(m[1], 10);
    }
  }
  return deck;
}

// Populate the step-1 "load a saved deck" picker from the deck library.
async function _fillSavedDecks() {
  const sel = $("#buildLoadDeck");
  if (!sel) return;
  try {
    const res = await api("/decks?limit=200");
    const opts = (res.data || []).map((d) =>
      `<option value="${escapeHtml(d.id)}">${escapeHtml(d.name || "(untitled)")} · ${d.total_cards || 0}</option>`
    ).join("");
    sel.innerHTML = `<option value="">— choose a saved deck —</option>` + opts;
  } catch (_e) { /* library unavailable */ }
}

// Load a saved deck into the wizard via GET /decks/:id/cards — server-
// parsed rows with clean names + type/cmc/mana_cost (handles set-code
// decklists a naive text parse would mangle). The commander section sets
// the commander; main becomes the deck; sideboard is dropped. Rows also
// seed the metadata cache so the decklist sidebar groups immediately.
async function _loadSavedDeck(id) {
  if (!id) return;
  const res = await api(`/decks/${encodeURIComponent(id)}/cards`).catch(() => null);
  if (!res) return;
  _build.commander = "";
  _build.deck = {};
  _build.sideboard = {};
  // The deck's stored (effective) commander wins over the list's own
  // commander rows; it may be an override naming a card in the main board.
  const stored = _deckDetailData && _deckDetailData.id === id ? _deckDetailData.commander : "";
  let listCmd = "";
  for (const r of (res.data || [])) {
    if (!r.name) continue;
    _cardMetaCache[r.name.toLowerCase()] = {
      type_line: r.type_line, cmc: r.cmc, mana_cost: r.mana_cost, id: r.id,
      oracle_id: r.oracle_id,
    };
    if (r.section === "sideboard") {
      _build.sideboard[r.name] = (_build.sideboard[r.name] || 0) + (r.qty || 1);
      continue;
    }
    // First commander-section row is the fallback commander; further rows
    // (partner/companion) stay in the deck so they aren't lost.
    if (r.section === "commander" && !listCmd) { listCmd = r.name; continue; }
    _build.deck[r.name] = (_build.deck[r.name] || 0) + (r.qty || 1);
  }
  _build.commander = stored || listCmd;
  // Never double-count: the commander is its own slot, not a deck card.
  if (_build.commander && _build.deck[_build.commander]) {
    _build.deck[_build.commander] -= 1;
    if (_build.deck[_build.commander] <= 0) delete _build.deck[_build.commander];
  }
  _build.step = 1;
  _buildAnalysis = null; _buildCmdCard = null;
  _buildOtagSel = new Set(); _buildFocusPool = new Set(); _buildFocusNew = new Set(); _buildCmdTagsFor = null;
  _saveBuild();
  _scheduleBuildAnalyze();
  _renderBuild();
}

function _renderBuild() {
  const pane = buildPaneEl;
  if (!pane) return;
  const step = _build.step;
  if (step !== _REVIEW_STEP) _buildActiveSlot = null;  // slot-split is review-only
  const cur = BUILD_STEPS[step - 1];
  // Lateral step menu — jump straight to any step.
  const nav = BUILD_STEPS.map((s, i) => {
    const n = i + 1;
    const cls = n === step ? "active" : (n < step ? "done" : "");
    // ponytail: the step's guidance lives in its hover title, not on the page.
    return `<button type="button" class="build-navstep ${cls}" data-build-goto="${n}" title="${escapeHtml(s.blurb)}">
      <span class="build-dot">${n}</span> ${escapeHtml(s.title)}</button>`;
  }).join("");
  pane.innerHTML = `
    <div class="build-layout">
      <nav class="build-sidenav" aria-label="Build steps">${nav}</nav>
      <div class="build-main">
        <div id="buildScoreboard" class="build-scoreboard"></div>
        <div class="build-step">
          <h2>${escapeHtml(cur.title)}</h2>
          <div id="buildStepBody"><p class="empty">Loading…</p></div>
        </div>
        <div class="build-nav">
          <button type="button" id="buildBack" ${step <= 1 ? "disabled" : ""}>◀ Back</button>
          <button type="button" id="buildNext" ${step >= BUILD_STEPS.length ? "disabled" : ""}>Next ▶</button>
          <button type="button" id="buildReset" class="btn btn--ghost btn--sm">Reset build</button>
        </div>
      </div>
      <aside class="build-decklist" id="buildDeckList" aria-label="Decklist"></aside>
    </div>`;
  _renderBuildScoreboard();
  _renderBuildStep();
  // Always re-analyze on a step render so the sticky scoreboard reflects
  // the true current deck, even after a debounced add that hadn't landed
  // before a navigation.
  _scheduleBuildAnalyze();
}

async function _renderBuildStep() {
  const body = $("#buildStepBody");
  if (!body) return;
  const step = _build.step;
  try {
    if (step === 1) _renderBuildStep1(body);
    else if (step === 2) await _renderBuildStep2(body);
    else if (step >= 3 && step <= 5) await _renderBuildStepSlots(body, BUILD_STEP_SLOTS[step]);
    else if (step === 6) await _renderBuildStep6(body);
    else if (step === _REVIEW_STEP) await _renderBuildStep7(body);
    // Fill the commander theme chips in whichever step just rendered a
    // search panel (no-op on steps 1 and 7).
    await _renderThemeChips();
  } catch (err) {
    body.innerHTML = `<p class="state-error" role="alert">Error: ${escapeHtml(err.message || "network")}</p>`;
  }
}

function _renderBuildStep1(body) {
  body.innerHTML = `
    <div class="build-cmd">
      <label>Commander name
        <input type="text" id="buildCmdInput" list="buildCmdList" autocomplete="off" value="${escapeHtml(_build.commander)}" placeholder="e.g. Krenko, Mob Boss">
        <datalist id="buildCmdList"></datalist>
      </label>
      <button type="button" id="buildSetCmd" class="btn btn--primary">Set commander</button>
    </div>
    <div id="buildCmdError" aria-live="polite"></div>
    <div class="build-cmd">
      <label>Load a saved deck
        <select id="buildLoadDeck"><option value="">— choose a saved deck —</option></select>
      </label>
      <button type="button" id="buildLoadDeckBtn">Load</button>
    </div>
    <h3>Suggest a commander</h3>
    <div class="build-cmd">
      <input type="text" id="buildCmdColors" spellcheck="false" autocapitalize="off" placeholder="Colours (blank = any)" maxlength="5">
      <button type="button" id="buildSuggestCmd">Suggest commanders</button>
    </div>
    <div id="buildCmdSuggest"></div>
    <details class="build-paste"><summary>Paste a list</summary>
      <textarea id="buildPaste" rows="8" placeholder="1 Sol Ring&#10;1 Cultivate&#10;…"></textarea>
      <button type="button" id="buildLoadPaste">Load list</button>
    </details>
    ${_build.commander ? `<h3>Deck focus</h3>${_buildSearchPanelHtml(null, true)}` : ""}`;
  _fillSavedDecks();
}

async function _renderBuildStep2(body) {
  if (!_build.commander) { body.innerHTML = `<p class="empty">Set a commander in step 1 first.</p>`; return; }
  if (!_buildCmdCard || _buildCmdCard.name !== _build.commander) {
    _buildCmdCard = await api(`/cards/named?exact=${encodeURIComponent(_build.commander)}`).catch(() => null);
  }
  if (!_buildCmdCard) { body.innerHTML = `<p class="empty">Couldn't resolve "${escapeHtml(_build.commander)}".</p>`; return; }
  // The theme chips (rendered into the panel by _renderThemeChips) are the
  // keyword-analysis control now — click one to list cards serving it.
  body.innerHTML = `
    <p class="help">${escapeHtml(_buildCmdCard.type_line || "")}</p>
    <p>${renderSymbols(_buildCmdCard.oracle_text || "")}</p>
    ${_buildSearchPanelHtml(null, true)}`;
}

// The commander's theme chips, shown in the search panel on every step so
// any slot search can be pushed toward the commander's themes. Tags are
// cached per commander; selection (_buildOtagSel) persists across steps.
async function _renderThemeChips() {
  const el = $("#buildThemes");
  if (!el) return;
  if (!_build.commander) { el.innerHTML = ""; return; }
  if (_buildCmdTagsFor !== _build.commander) {
    if (!_buildCmdCard || _buildCmdCard.name !== _build.commander) {
      _buildCmdCard = await api(`/cards/named?exact=${encodeURIComponent(_build.commander)}`).catch(() => null);
    }
    const t = _buildCmdCard ? await api(`/cards/${_buildCmdCard.id}/tags`).catch(() => ({ data: [] })) : { data: [] };
    _buildCmdTags = t.data || [];
    _buildCmdTagsFor = _build.commander;
    // Reseed the candidate pool for the new commander (keeps any already
    // prioritised tags visible even if they came from a card).
    _buildFocusPool = new Set([..._buildCmdTags, ..._buildOtagSel]);
    _buildFocusNew = new Set();
  }
  // Most-represented themes first — surfaces what the deck is built around.
  const pool = Array.from(_buildFocusPool).sort((a, b) =>
    (_buildTagCounts[b] || 0) - (_buildTagCounts[a] || 0) || a.localeCompare(b));
  // The search box is written once per panel; later re-renders (e.g. a
  // card added in step 1 surfaces new tags) only replace the chips, so
  // they never wipe what is being typed.
  if (!$("#buildFocusChips")) {
    el.innerHTML = `<span class="help">Focus</span>
      <input type="search" id="buildFocusQ" list="buildFocusList" class="build-focus-q" placeholder="Find a theme" aria-label="Find a theme" spellcheck="false" autocomplete="off">
      <datalist id="buildFocusList"></datalist>
      <span id="buildFocusChips" class="build-themes"></span>`;
  }
  $("#buildFocusChips").innerHTML = pool.map((t) => {
    const cls = (_buildOtagSel.has(t) ? " selected" : "") + (_buildFocusNew.has(t) ? " new" : "");
    const n = _buildTagCounts[t] || 0;
    return `<button type="button" class="chip build-otag${cls}" data-build-otag="${escapeHtml(t)}" title="${n} deck card(s) with this tag">${escapeHtml(t)} <span class="chip-count">${n}</span></button>`;
  }).join("");
  _filterFocusChips();
}

// Narrow the shown Focus chips to the typed text. Selected chips always
// stay visible so the active themes are never hidden by a search.
function _filterFocusChips() {
  const q = (($("#buildFocusQ") || {}).value || "").trim().toLowerCase();
  for (const chip of document.querySelectorAll("#buildFocusChips [data-build-otag]")) {
    chip.hidden = !!q && !chip.classList.contains("selected") && !chip.dataset.buildOtag.toLowerCase().includes(q);
  }
}

// Merge a just-added card's oracle tags into the focus pool (step 1), so
// its themes become suggestions the user can prioritise — e.g. adding
// Mycosynth Golem surfaces "affinity" / "artifact" chips.
async function _addFocusFromCard(id) {
  if (!id) return;
  const t = await api(`/cards/${id}/tags`).catch(() => ({ data: [] }));
  let added = false;
  for (const tag of (t.data || [])) {
    if (!_buildFocusPool.has(tag)) { _buildFocusPool.add(tag); _buildFocusNew.add(tag); added = true; }
  }
  if (added) await _renderThemeChips();
}

async function _renderBuildStepSlots(body, slots) {
  // Recommendations ARE a slot-filtered search now — one results area, with
  // the filter that produced it shown and tweakable. The goal line comes
  // from analyze deltas (not /decks/suggest).
  await _buildAnalyze();
  const slotPred = slots.length === 1
    ? `pred:${slots[0]}`
    : `(${slots.map((s) => `pred:${s}`).join(" or ")})`;
  body.innerHTML = `<p class="build-slot-goals">${_buildSlotGoalsHtml(slots)}</p>` + _buildSearchPanelHtml(slotPred, true);
  await _runBuildSearch();
}

// "Ramp (-12 to target)" line for the slot steps; rebuilt from the latest
// analysis, so _buildAnalyze refreshes it after every add/remove.
function _buildSlotGoalsHtml(slots) {
  const deltas = (_buildAnalysis && _buildAnalysis.deltas) || {};
  return slots.map((s) => {
    const d = deltas[s];
    const human = s.replace(/_/g, " ");
    return `${escapeHtml(human)}${d != null ? ` <span class="help">(${d} to target)</span>` : ""}`;
  }).join(" · ");
}
function _refreshSlotGoals() {
  const el = $(".build-slot-goals");
  const slots = BUILD_STEP_SLOTS[_build.step];
  if (el && slots) el.innerHTML = _buildSlotGoalsHtml(slots);
}

async function _renderBuildStep6(body) {
  // Step 6's land count / curve come from analysis, not a per-step fetch —
  // refresh it first so the line is never stale (e.g. right after a bulk
  // mana-base add). Steps 3-5 read fresh deltas from /decks/suggest, so
  // only this step needs it.
  await _buildAnalyze();
  const a = _buildAnalysis;
  const lands = (a && a.counts && a.counts.land) || 0;
  const cd = (a && a.curve_deltas) || {};
  const curveNotes = Object.keys(cd).filter((k) => cd[k] > 0)
    .map((k) => `MV${k} +${cd[k]}`).join(", ");
  const total = _buildCount();

  // Two mini charts: colored pips the deck NEEDS vs mana sources it HAS.
  // Await the symbology fetch so the {W}/{U}/… labels render as the mana
  // glyphs, not literal text (local API calls otherwise win the race on a
  // fresh load and the chart paints before symbolMap is populated).
  await symbolsReady;
  const pips = (a && a.pips) || {};
  const sources = await _buildManaSources();
  const chartsHtml = _renderManaRadar(pips, sources);

  // Basics currently in the deck, each with a −1 control.
  const basicsIn = Object.keys(_build.deck)
    .filter((n) => _BASIC_NAMES.has(n))
    .map((n) =>
      `<li><span class="build-card-name">${escapeHtml(n)}</span> ×${_build.deck[n]}
        <button type="button" class="build-rm" data-build-basic-minus="${escapeHtml(n)}">−1</button></li>`
    ).join("");

  // Preview the pip-weighted fill that would take the deck to 38 lands.
  const need = Math.max(0, 38 - lands);
  const alloc = _pipBasicAllocation(need);
  const allocTxt = Object.keys(alloc).map((n) => `${alloc[n]} ${n}`).join(", ");

  let cutHtml = "";
  if (total > 100) {
    const rows = Object.keys(_build.deck).map((name) =>
      `<li><span class="build-card-name">${escapeHtml(name)}</span> ×${_build.deck[name]}
        <button type="button" class="build-rm" data-build-remove="${escapeHtml(name)}">remove</button></li>`
    ).join("");
    cutHtml = `<h3>Cut to 100 (${total} now, ${total - 100} over)</h3><ul class="build-cards">${rows}</ul>`;
  }

  // Nonbasics from the manabase recommender become swap candidates: adding
  // one pulls a basic so the land count holds at 38.
  let mbHtml = `<p class="empty">Set a commander for nonbasic suggestions.</p>`;
  if (_build.commander) {
    try {
      const mb = await _buildPost("/decks/manabase?land_count=38", { commander: _build.commander });
      const nb = (mb.nonbasics || []).filter((c) => c.name && !_buildHas(c.name)).map((c) => {
        const prod = (c.produced_mana || []).filter((x) => "WUBRG".includes(x)).join("");
        return `<li data-id="${escapeHtml(c.id || "")}" data-preview=""><span class="build-card-name">${escapeHtml(c.name)}</span>
          <button type="button" class="build-add" data-build-swap="${escapeHtml(c.name)}" data-build-swap-produces="${prod}">swap in</button></li>`;
      }).join("");
      mbHtml = `<ul class="build-cards">${nb || '<li class="empty">All suggestions already in the deck.</li>'}</ul>`;
    } catch (err) {
      mbHtml = `<p class="state-error" role="alert">Manabase: ${escapeHtml(err.message || "error")}</p>`;
    }
  }

  const fillLabel = need > 0
    ? `Fill to 38 lands with basics${allocTxt ? " — " + escapeHtml(allocTxt) : ""}`
    : "Already at 38 lands";
  body.innerHTML = `
    <p class="help">Lands: <strong>${lands}</strong> / 38 mandate.${curveNotes ? " Curve over target at " + escapeHtml(curveNotes) + "." : ""}</p>
    ${chartsHtml}
    <h3>Basic lands</h3>
    <button type="button" id="buildFillBasics" ${need > 0 && allocTxt ? "" : "disabled"}>${fillLabel}</button>
    ${basicsIn ? `<ul class="build-cards">${basicsIn}</ul>` : ""}
    <div id="buildSwapNote" class="help"></div>
    ${cutHtml}
    <h3>Swap in a nonbasic</h3>
    ${mbHtml}
    ${_buildSearchPanelHtml("t:land")}`;
}

// Combos present in a deck + combos it's one card away from. `present`
// rows carry `cards`/`results`; `near` rows carry `gap`/`in_deck`/`results`.
function _renderComboPanel(present, near) {
  present = present || [];
  near = near || [];
  if (!present.length && !near.length) return "";
  const row = (c) => {
    const cards = (c.cards || []).join(" + ");
    const res = (c.results || []).join(", ");
    return `<li>${escapeHtml(cards)}${res ? " → " + escapeHtml(res) : ""}</li>`;
  };
  const nrow = (c) => {
    const gap = (c.gap || []).map((g) => g.name).join(", ");
    const have = (c.in_deck || []).join(", ");
    const res = (c.results || []).join(", ");
    return `<li>add <strong>${escapeHtml(gap)}</strong>${have ? ` (have ${escapeHtml(have)})` : ""}${res ? " → " + escapeHtml(res) : ""}</li>`;
  };
  let html = `<h3>Combos</h3>`;
  if (present.length) {
    html += `<p class="help">${present.length} in the deck:</p><ul class="build-combos">${present.map(row).join("")}</ul>`;
  }
  if (near.length) {
    html += `<p class="help">${near.length} combo${near.length === 1 ? "" : "s"} one card away:</p><ul class="build-combos">${near.map(nrow).join("")}</ul>`;
  }
  return html;
}

// Final step — the turn-7 goldfish test AND the read-only review, merged.
// Two render phases so the heavy goldfish/combos fetch runs once (on entry)
// while the analysis-driven summary (colour balance, unresolved, ownership)
// repaints cheaply whenever a later analyze lands, via _renderReviewSummary.
async function _renderBuildStep7(body) {
  body.innerHTML = `
    <div id="buildReviewSummary"></div>
    <div id="buildTurn7"><p class="help">Goldfishing…</p></div>
    <p>Happy with it? Save the build or copy the list.</p>
    <div class="build-cmd">
      <input type="text" id="buildSaveName" placeholder="deck name" value="${escapeHtml(_build.commander ? _build.commander + " (built)" : "New EDH deck")}">
      <button type="button" id="buildSaveDeck" class="btn btn--primary">Save as deck</button>
      <button type="button" id="buildCopyList">Copy decklist</button>
    </div>
    <div id="buildSaveStatus" class="help"></div>`;
  _renderReviewSummary();
  const decklist = _buildDecklist();
  const [gf, mull, combos, near] = await Promise.all([
    _buildPost("/decks/goldfish", { decklist, hands: 400, seed: 42 }).catch(() => null),
    _buildPost("/decks/mulligan", { decklist, hands: 400, seed: 42 }).catch(() => null),
    _buildPost("/decks/combos", { decklist }).catch(() => null),
    fetch("/api/decks/near-combos?missing=1&limit=10", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ decklist }),
    }).then((r) => (r.ok ? r.json() : null)).catch(() => null),
  ]);
  const t7 = $("#buildTurn7");
  if (!t7) return;  // navigated away while goldfishing
  const keep = mull && mull.stats ? (mull.stats.keep_rate * 100).toFixed(0) + "%" : "—";
  const last = (arr) => (Array.isArray(arr) && arr.length ? arr[arr.length - 1] : 0);
  const gfLine = gf
    ? `<p class="help">Goldfish (${gf.sims} sims, ${gf.turns} turns): by turn ${gf.turns}, avg <strong>${last(gf.avg_mana_by_turn).toFixed(1)}</strong> mana &amp; <strong>${last(gf.avg_lands_in_play_by_turn).toFixed(1)}</strong> lands in play; ${gf.mulligans_taken} mulligans across the run.</p>`
    : "";
  t7.innerHTML = `
    <p class="help">Keepable opening hands: <strong>${keep}</strong></p>
    ${gfLine}
    ${_renderComboPanel(combos && combos.combos, near && near.combos)}`;
}

// Analysis-driven half of the review: colour balance, unresolved names, and
// inventory coverage. The colour chart is the SAME need-vs-have radar step 6
// draws (_renderManaRadar over pips + mana sources), so the two steps agree.
// Idempotent, so it can repaint on its own when a debounced analyze lands.
async function _renderReviewSummary() {
  const el = $("#buildReviewSummary");
  if (!el) return;
  const a = _buildAnalysis;
  if (!a) { el.innerHTML = `<p class="empty">Add cards first — nothing to review yet.</p>`; return; }
  const unresolved = (a.unresolved || []).length
    ? `<div class="build-review-block"><h3>Unresolved (${a.unresolved.length})</h3>
        <p class="help">These names didn't resolve to a card — fix the spelling in an earlier step.</p>
        <ul class="build-unresolved">${a.unresolved.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul></div>`
    : "";
  const inv = a.inventory_summary;
  const invBlock = inv
    ? `<div class="build-review-block"><h3>Ownership</h3>
        <p class="help">${inv.owned_cards_in_deck} / ${plural(inv.total_cards_in_deck, "card")} owned (${formatPct(inv.coverage_pct)}) · ${inv.total_missing_count} missing${inv.missing_on_wishlist ? ` · ${inv.missing_on_wishlist} on wishlist` : ""}</p></div>`
    : "";
  const lands = (a.counts && a.counts.land) || 0;
  // Same inputs as step 6: await the glyph fetch, then the mana-source tally.
  await symbolsReady;
  const sources = await _buildManaSources();
  const el2 = $("#buildReviewSummary");
  if (!el2) return;  // navigated away while tallying sources
  el2.innerHTML = `
    <div class="build-review-block"><h3>Colour balance</h3>
      <p class="help">Mana base: <strong>${lands}</strong> / 38 lands.</p>
      ${_renderManaRadar(a.pips || {}, sources)}</div>
    ${unresolved}
    ${invBlock}`;
}

// Toggle which slot splits the decklist on the review step. The chip's active
// state lives in the scoreboard and the split itself in the decklist panel, so
// re-render both. (On non-review steps the panel dims by the step's own slot;
// nothing here to toggle.)
function _highlightDeckSlot(label) {
  _buildActiveSlot = _buildActiveSlot === label ? null : label;
  _renderBuildScoreboard();
  _renderDeckPanel();
}

// Which slot(s) the decklist panel should key off for the current step: the
// step's own focus slot on the build steps (so the sidebar dims everything
// that isn't the slot being worked), the clicked slot on the review step, or
// null where the step has no single focus (commander, keyword analysis).
function _panelFocusSlots() {
  const s = _build.step;
  if (s === _REVIEW_STEP) return _buildActiveSlot ? [_buildActiveSlot] : null;
  if (BUILD_STEP_SLOTS[s]) return BUILD_STEP_SLOTS[s];
  if (s === 6) return ["land"];  // Lands & cutting → highlight the mana base
  return null;
}

// A card fills a focus set if it carries one of the labels and isn't a land
// (mirrors the tally's land-exclusion, so the lit set matches the counts) —
// except the land focus, which matches lands.
function _cardFillsSlots(nameLower, slots, byName) {
  const c = byName[nameLower];
  if (!c || !slots) return false;
  if (slots.includes("land")) return !!c.is_land;
  return !c.is_land && (c.predicted_labels || []).some((l) => slots.includes(l));
}

// Recategorise a card by dropping it on a slot: ADD the slot to the card's
// existing labels rather than replace them, so a card that fills two slots
// keeps both. `plan` is the classifier's residue label — mutually exclusive
// with the functional slots — so adding a functional label drops plan, and
// dropping onto plan resets the card to plan-only. Then re-tally so the
// scoreboard + split reflect the change. Same override as the card-detail
// editor (PUT /cards/:id/labels).
async function _recategoriseCard(cardId, label) {
  const cur = ((_buildAnalysis && _buildAnalysis.cards) || [])
    .find((c) => c.id === cardId)?.predicted_labels || [];
  const labels = label === "plan"
    ? ["plan"]
    : [...new Set([...cur.filter((l) => l !== "plan"), label])];
  try {
    const resp = await fetch(`/api/cards/${encodeURIComponent(cardId)}/labels`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ labels }),
    });
    if (!resp.ok) {
      throw new Error((await resp.json().catch(() => ({}))).details || resp.status);
    }
    await _buildAnalyze();
  } catch (err) {
    showBanner(`Recategorise failed: ${err.message || "network"}`);
  }
}

// Drag a decklist row onto a slot chip (review step) to move that card into
// the slot. `land` is not a drop target — lands are typed by type_line, not
// labels, so a label can't make a card count as land.
let _dragCardId = null;
const _isDropSlot = (el) => {
  const s = el && el.closest("[data-build-slot]");
  return s && s.dataset.buildSlot !== "land" ? s : null;
};
buildPaneEl.addEventListener("dragstart", (e) => {
  const row = e.target.closest(".build-dl-row[draggable]");
  _dragCardId = row && row.dataset.id ? row.dataset.id : null;
  if (_dragCardId && e.dataTransfer) {
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", _dragCardId); } catch (_e) { /* ignore */ }
  }
});
buildPaneEl.addEventListener("dragover", (e) => {
  const slot = _isDropSlot(e.target);
  if (!slot || !_dragCardId) return;
  e.preventDefault();  // required to make the chip a valid drop target
  if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
  slot.classList.add("drag-over");
});
buildPaneEl.addEventListener("dragleave", (e) => {
  const slot = _isDropSlot(e.target);
  if (slot && !slot.contains(e.relatedTarget)) slot.classList.remove("drag-over");
});
buildPaneEl.addEventListener("drop", async (e) => {
  const slot = _isDropSlot(e.target);
  if (!slot) return;
  e.preventDefault();
  slot.classList.remove("drag-over");
  const id = _dragCardId
    || (e.dataTransfer ? e.dataTransfer.getData("text/plain") : "");
  _dragCardId = null;
  if (id) await _recategoriseCard(id, slot.dataset.buildSlot);
});

// One delegated listener for the whole wizard.
buildPaneEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.id === "bsQ") { e.preventDefault(); _runBuildSearch(); }
});
// Focus search: typing narrows the shown chips and suggests any other
// oracle tag (prefix match on /tags). Picking a suggestion adds it to the
// pool, selects it and re-runs the search, like clicking a chip.
let _buildFocusAcTimer = null;
buildPaneEl.addEventListener("input", async (e) => {
  if (e.target.id !== "buildFocusQ") return;
  const v = e.target.value.trim();
  const picked = Array.from(($("#buildFocusList") || {}).options || []).some((o) => o.value === v);
  if (picked) {
    e.target.value = "";
    $("#buildFocusList").innerHTML = "";
    _buildFocusPool.add(v);
    _buildOtagSel.add(v);
    _buildFocusNew.delete(v);
    await _renderThemeChips();
    await _runBuildSearch();
    return;
  }
  _filterFocusChips();
  if (_buildFocusAcTimer) clearTimeout(_buildFocusAcTimer);
  if (v.length < 2) return;
  _buildFocusAcTimer = setTimeout(async () => {
    try {
      const res = await api(`/tags?prefix=${encodeURIComponent(v)}&limit=15`);
      const dl = $("#buildFocusList");
      if (dl) {
        dl.innerHTML = (res.data || [])
          .map((t) => `<option value="${escapeHtml(t.slug)}" label="${t.count} cards"></option>`).join("");
      }
    } catch (_e) { /* no suggestions */ }
  }, 200);
});
// Commander-name autocomplete via a native <datalist>: fill it with the
// top commanders matching the fragment (is:commander, EDHREC-ranked) so
// typing "Radagast" surfaces the card even when it is far down the
// colours-based suggest list.
let _buildCmdAcTimer = null;
buildPaneEl.addEventListener("input", (e) => {
  if (e.target.id !== "buildCmdInput") return;
  const q = e.target.value.trim();
  if (_buildCmdAcTimer) clearTimeout(_buildCmdAcTimer);
  if (q.length < 2) return;
  _buildCmdAcTimer = setTimeout(async () => {
    try {
      const res = await api(`/cards/search?q=${encodeURIComponent("is:commander " + q)}&order=edhrec&limit=10&projection=name`);
      const dl = $("#buildCmdList");
      if (dl) dl.innerHTML = (res.data || []).map((c) => `<option value="${escapeHtml(c.name)}"></option>`).join("");
    } catch (_e) { /* no suggestions */ }
  }, 200);
});
buildPaneEl.addEventListener("click", async (e) => {
  if (e.target.closest("#bsGo")) { await _runBuildSearch(); return; }
  // Add/remove swap the clicked button IN PLACE (not a full step re-render)
  // so a search/suggest list survives adding several cards from it; the
  // scoreboard refreshes via the debounced analyze inside _buildAdd/Remove.
  const add = e.target.closest("[data-build-add]");
  if (add) {
    const nm = add.dataset.buildAdd;
    const rowId = add.closest("li") && add.closest("li").dataset.id;
    _buildAdd(nm);
    add.outerHTML = `<button type="button" class="build-rm" data-build-remove="${escapeHtml(nm)}">✓ in deck — remove</button>`;
    // Step 1: pull the added card's themes into the focus pool as suggestions.
    if (_build.step === 1) await _addFocusFromCard(rowId);
    return;
  }
  const rm = e.target.closest("[data-build-remove]");
  if (rm) {
    const nm = rm.dataset.buildRemove;
    _buildRemove(nm);
    rm.outerHTML = `<button type="button" class="build-add" data-build-add="${escapeHtml(nm)}">+ add</button>`;
    return;
  }
  // Step 6: pip-weighted basic fill, per-basic decrement, and swap-in.
  if (e.target.closest("#buildFillBasics")) {
    const lands = (_buildAnalysis && _buildAnalysis.counts && _buildAnalysis.counts.land) || 0;
    const alloc = _pipBasicAllocation(Math.max(0, 38 - lands));
    for (const n of Object.keys(alloc)) _build.deck[n] = (_build.deck[n] || 0) + alloc[n];
    _saveBuild();
    await _renderBuildStep();
    return;
  }
  const bm = e.target.closest("[data-build-basic-minus]");
  if (bm) { _buildRemoveOne(bm.dataset.buildBasicMinus); await _renderBuildStep(); return; }
  const sw = e.target.closest("[data-build-swap]");
  if (sw) {
    const nm = sw.dataset.buildSwap;
    const victim = _buildSwapIn(nm, sw.dataset.buildSwapProduces || "");
    await _renderBuildStep();
    const note = $("#buildSwapNote");
    if (note) {
      note.textContent = victim
        ? `Swapped in ${nm} — removed 1 ${victim}.`
        : `Added ${nm} — no basic left to remove (deck is now ${_buildCount()}).`;
    }
    return;
  }

  // Step 8 review — clicking a scoreboard slot lights its cards in the decklist.
  const slot = e.target.closest("[data-build-slot]");
  if (slot) { _highlightDeckSlot(slot.dataset.buildSlot); return; }

  const goto = e.target.closest("[data-build-goto]");
  if (goto) { _build.step = parseInt(goto.dataset.buildGoto, 10); _saveBuild(); _renderBuild(); return; }
  if (e.target.closest("#buildBack")) { _build.step = Math.max(1, _build.step - 1); _saveBuild(); _renderBuild(); return; }
  if (e.target.closest("#buildNext")) { _build.step = Math.min(BUILD_STEPS.length, _build.step + 1); _saveBuild(); _renderBuild(); return; }
  if (e.target.closest("#buildReset")) {
    if (!window.confirm("Discard this build and start over?")) return;
    _build = { step: 1, commander: "", deck: {}, sideboard: {} };
    _buildAnalysis = null; _buildCmdCard = null;
    _buildOtagSel = new Set(); _buildFocusPool = new Set(); _buildFocusNew = new Set(); _buildCmdTagsFor = null;
    _saveBuild(); _renderBuild(); return;
  }

  if (e.target.closest("#buildSetCmd")) {
    const v = ($("#buildCmdInput").value || "").trim();
    const errEl = $("#buildCmdError");
    let card = null;
    if (v) {
      // Resolve now: an unresolved name used to be accepted and only failed
      // steps later. The canonical card name is what we store.
      const btn = e.target.closest("#buildSetCmd");
      btn.disabled = true;
      try { card = await api(`/cards/named?fuzzy=${encodeURIComponent(v)}`); }
      catch { card = null; }
      btn.disabled = false;
      if (!card) {
        if (errEl) errEl.innerHTML = `<p class="state-error" role="alert">Couldn't find a card called “${escapeHtml(v)}”. Check the spelling, or pick one from the suggestions.</p>`;
        return;
      }
    }
    _build.commander = card ? card.name : ""; _buildCmdCard = card;
    _buildOtagSel = new Set(); _buildFocusPool = new Set(); _buildFocusNew = new Set(); _buildCmdTagsFor = null;
    _saveBuild();
    _scheduleBuildAnalyze(); _renderBuildStep(); return;
  }
  if (e.target.closest("#buildSuggestCmd")) {
    const colors = ($("#buildCmdColors").value || "").trim();
    const qs = colors ? `?colors=${encodeURIComponent(colors)}&limit=10` : "?limit=10";
    const out = $("#buildCmdSuggest");
    try {
      const res = await api(`/commanders/suggest${qs}`);
      out.innerHTML = `<ul class="build-cards">${(res.data || []).map((c) =>
        `<li data-id="${escapeHtml(c.id)}" data-preview=""><span class="build-card-name">${escapeHtml(c.name)}</span>
          <span class="help">${escapeHtml(c.type_line || "")}</span>
          <button type="button" class="build-add" data-build-use-cmd="${escapeHtml(c.name)}">use</button></li>`).join("")}</ul>`;
    } catch (err) { out.innerHTML = `<p class="empty">${escapeHtml(err.message || "error")}</p>`; }
    return;
  }
  const useCmd = e.target.closest("[data-build-use-cmd]");
  if (useCmd) {
    _build.commander = useCmd.dataset.buildUseCmd; _buildCmdCard = null;
    _buildOtagSel = new Set(); _buildFocusPool = new Set(); _buildFocusNew = new Set(); _buildCmdTagsFor = null;
    _saveBuild();
    _scheduleBuildAnalyze(); _renderBuildStep(); return;
  }
  if (e.target.closest("#buildLoadPaste")) {
    const txt = ($("#buildPaste").value || "").trim();
    if (!txt) return;
    _build.deck = _parseListToDeck(txt);
    _saveBuild(); _scheduleBuildAnalyze(); _renderBuildStep(); return;
  }
  if (e.target.closest("#buildLoadDeckBtn")) {
    await _loadSavedDeck($("#buildLoadDeck") && $("#buildLoadDeck").value);
    return;
  }

  const otag = e.target.closest("[data-build-otag]");
  if (otag) {
    // Toggle the theme; it's applied as a filter by _runBuildSearch.
    const tag = otag.dataset.buildOtag;
    if (_buildOtagSel.has(tag)) { _buildOtagSel.delete(tag); otag.classList.remove("selected"); }
    else { _buildOtagSel.add(tag); otag.classList.add("selected"); }
    _buildFocusNew.delete(tag); otag.classList.remove("new");
    _filterFocusChips();
    await _runBuildSearch();
    return;
  }

  if (e.target.closest("#buildCopyList")) {
    try { await navigator.clipboard.writeText(_buildDecklist()); $("#buildSaveStatus").textContent = "Copied."; }
    catch (_e) { $("#buildSaveStatus").textContent = "Copy failed — select the text manually."; }
    return;
  }
  if (e.target.closest("#buildSaveDeck")) {
    const name = ($("#buildSaveName").value || "New EDH deck").trim();
    const st = $("#buildSaveStatus");
    try {
      const deck = await _buildPost("/decks", { name, decklist: _buildDecklist(), format: "commander", commander: _build.commander || null });
      st.innerHTML = `Saved as <strong>${escapeHtml(deck.name)}</strong>. Open the Decks tab to evaluate it.`;
    } catch (err) { st.textContent = "Save failed: " + (err.message || "error"); }
    return;
  }
});

_bootFromUrl();
