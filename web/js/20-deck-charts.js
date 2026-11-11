// D12.6 — Decklist analysis: POST /api/decks/analyze with the textarea body,
// render slot bar with deltas, an unresolved list when present, and a
// click-through card table that routes selectCard() back through the same
// detail pane the search tab uses.
const _DECK_LABEL_ORDER = [
  ...PRED_LABELS, "land",
];

function _deltaClass(delta) {
  if (delta > 0) return "over";
  if (delta < 0) return "under";
  return "zero";
}

// Hover explanations for each deck class (edh-guide.md §4/§5).
const _SLOT_HELP = {
  ramp: "Mana acceleration — extra mana to deploy multiple spells per turn cycle. Target 10-12, prioritise the 2-mana slot.",
  card_advantage: "Net-positive card draw (draw 2+). Cantrips that only replace themselves don't count. Target 12.",
  targeted_disrupt: "Targeted, one-for-one interaction — spot removal, counterspells, phase-outs. Target 12.",
  mass_disrupt: "Mass resets — board wipes, artifact/enchantment sweepers, graveyard hate. Target 6.",
  plan: "The deck's core: synergy pieces, payoffs and win conditions. Target 30.",
  land: "Your mana base — the 38-land mandate for early-game consistency.",
};

// `interactive` (step-8 review): render each slot as a button carrying
// data-build-slot, so clicking it highlights that slot's cards in the
// decklist sidebar (_highlightDeckSlot) instead of being an inert readout.
function _renderDeckSlots(body, interactive) {
  const targets = body.targets || {};
  const counts = body.counts || {};
  const deltas = body.deltas || {};
  // An empty deck is "not started", not "every slot is short": no red deficits.
  const started = Object.values(counts).some((n) => n > 0);
  const cells = _DECK_LABEL_ORDER.map((lab) => {
    const count = counts[lab] ?? 0;
    // Land is excluded from the model's targets/deltas server-side, but the
    // guide's 38-land mandate is a real target — show it like the others.
    const target = targets[lab] ?? (lab === "land" ? 38 : undefined);
    const delta = deltas[lab] ?? (lab === "land" ? count - 38 : undefined);
    const cls = lab === "land" ? "" : `pred-${lab}`;
    const targetTxt = target != null ? `/ ${target}` : "";
    const deltaTxt = delta == null
      ? ""
      : `<span class="deck-slot-delta ${started ? _deltaClass(delta) : "zero"}">${delta > 0 ? "+" : ""}${delta}</span>`;
    const inner = `<span class="deck-slot-label">${escapeHtml(lab.replace(/_/g, " "))}</span>
      <span class="deck-slot-count">${count} ${escapeHtml(targetTxt)}</span>
      ${deltaTxt}`;
    return interactive
      ? `<button type="button" class="deck-slot build-slot-btn ${cls}${_buildActiveSlot === lab ? " active" : ""}" data-build-slot="${escapeHtml(lab)}" title="${escapeHtml(_SLOT_HELP[lab] || "")}">${inner}</button>`
      : `<div class="deck-slot ${cls}" title="${escapeHtml(_SLOT_HELP[lab] || "")}">${inner}</div>`;
  }).join("");
  return `<div class="deck-slots" aria-label="Slot counts vs targets">${cells}</div>`;
}

// Mana curve: 8 buckets (0..7+) as a small bar chart. Bar heights scale
// against the deck's own max bucket so the shape reads even for small
// samples; the count sits above the bar.
const _CURVE_BUCKETS = ["0", "1", "2", "3", "4", "5", "6", "7+"];

// Guide §6 bell as a per-bucket target for the 8-column chart. The guide
// lumps MV6+ into one bucket of 5; shown here on the "6" column, with 0 at
// "7+" (few top-end drops). Passed in to draw a dashed recommendation line.
const _CURVE_TARGET_MAP = { "0": 0, "1": 9, "2": 18, "3": 15, "4": 10, "5": 5, "6": 5, "7+": 0 };

function _renderDeckCurve(curve, targets) {
  if (!curve) return "";
  // Scale to the larger of the deck's peak and the tallest target, so bars
  // and target lines share one axis and line up 1:1 at full-target buckets.
  const max = Math.max(
    1,
    ..._CURVE_BUCKETS.map((b) => Math.max(curve[b] || 0, targets ? (targets[b] || 0) : 0)),
  );
  const bars = _CURVE_BUCKETS.map((b) => {
    const n = curve[b] || 0;
    const h = Math.round((n / max) * 100);
    const t = targets ? (targets[b] || 0) : 0;
    const mvLabel = b === "6" ? "MV6+" : "MV" + b;
    const colTitle = `${mvLabel}: ${n} in deck${t > 0 ? `, recommended ${t}` : ""}`;
    // Bar and target both live inside .deck-curve-track (the middle band),
    // each a % of the track — so neither can escape the chart.
    const marker = t > 0
      ? `<div class="deck-curve-target" style="bottom: ${Math.round((t / max) * 100)}%" title="${escapeHtml(colTitle)}"></div>`
      : "";
    return `<div class="deck-curve-col" title="${escapeHtml(colTitle)}">
      <span class="deck-curve-count">${n}</span>
      <div class="deck-curve-track">${marker}<div class="deck-curve-bar" style="height: ${h}%"></div></div>
      <span class="deck-curve-label">${b}</span>
    </div>`;
  }).join("");
  return `<div class="deck-curve" aria-label="Mana curve" title="Bars = this deck's curve. Dashed lines = the guide's target card count per mana value (from the deck guide).">${bars}</div>`;
}

