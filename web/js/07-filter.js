// Advanced filter: one dialog, opened from any card-list filter input
// ([data-filter-for="<input id>"]), writes search syntax into that input.

const _fq = (v) => /[\s"():]/.test(v) ? `"${v.replace(/"/g, "")}"` : v;
const _fAny = (terms) => terms.length > 1 ? `(${terms.join(" or ")})` : terms.join("");
const _fCmp = (v) => v.n === "" ? "" : `${v.key}${v.op}${v.n}`;

let _fTypes = null;
let _fSets = null;
const _fTypeList = () => (_fTypes ||= api("/types").then((b) => b.data.map((r) => r.type)));
const _fSetList = () => (_fSets ||= api("/sets").then((b) => b.data.map((s) => `${s.name} (${s.code})`)));
const _fPrefix = (list, q) => {
  const lo = q.toLowerCase();
  return list.filter((s) => s.toLowerCase().startsWith(lo)).concat(
    list.filter((s) => !s.toLowerCase().startsWith(lo) && s.toLowerCase().includes(lo)));
};

// `type` takes several words; suggestions complete the last one.
async function _fSuggestTypes(q) {
  const head = q.slice(0, q.lastIndexOf(" ") + 1);
  const last = q.slice(head.length);
  return last ? _fPrefix(await _fTypeList(), last).map((t) => head + t) : null;
}

async function _fSuggestSets(q) {
  return _fPrefix(await _fSetList(), q);
}

const _FILTER_FIELDS = [
  { id: "name", label: "Name", kind: "text", term: (v) => `name:${_fq(v)}`,
    suggest: async (q) => (await api(`/cards/autocomplete?q=${encodeURIComponent(q)}`)).data },
  { id: "text", label: "Text", kind: "text", term: (v) => `o:${_fq(v)}` },
  { id: "type", label: "Type", kind: "text", suggest: _fSuggestTypes,
    term: (v) => v.split(/\s+/).map((w) => `t:${_fq(w)}`).join(" ") },
  { id: "color", label: "Colors", kind: "colors", pips: ["w", "u", "b", "r", "g", "c"],
    ops: [["=", "Exactly"], [">=", "Including"], ["<=", "At most"]],
    term: (v) => v.on.length ? `c${v.op}${v.on.join("")}` : "" },
  { id: "identity", label: "Identity", kind: "colors", pips: ["w", "u", "b", "r", "g"],
    term: (v) => v.length ? `id<=${v.join("")}` : "" },
  { id: "stat", label: "Stats", kind: "compare", keys: [["cmc", "Mana value"], ["pow", "Power"], ["tou", "Toughness"]], term: _fCmp },
  { id: "price", label: "Price", kind: "compare", keys: [["usd", "USD"]], term: _fCmp },
  { id: "rarity", label: "Rarity", kind: "chips",
    options: [["c", "Common"], ["u", "Uncommon"], ["r", "Rare"], ["m", "Mythic"]],
    term: (v) => _fAny(v.map((r) => `r:${r}`)) },
  { id: "role", label: "Role", kind: "chips",
    options: PRED_LABELS.map((l) => [l, l.replace(/_/g, " "), `chip-pred pred-${l}`]),
    term: (v) => _fAny(v.map((l) => `pred:${l}`)) },
  { id: "format", label: "Legal in", kind: "select",
    options: ["commander", "standard", "pioneer", "modern", "legacy", "vintage", "pauper", "brawl", "oathbreaker"],
    term: (v) => `f:${v}` },
  { id: "tag", label: "Tag", kind: "text", term: (v) => `otag:${_fq(v)}`,
    suggest: async (q) => (await api(`/tags?prefix=${encodeURIComponent(q)}&limit=20`)).data.map((r) => r.slug) },
  { id: "keyword", label: "Keyword", kind: "text", term: (v) => `kw:${_fq(v)}`,
    suggest: async (q) => (await api(`/keywords?prefix=${encodeURIComponent(q)}&limit=20`)).data.map((r) => r.keyword) },
  { id: "set", label: "Set", kind: "text", suggest: _fSuggestSets,
    term: (v) => `set:${(v.match(/\(([^)]+)\)$/) || [, v])[1]}` },
  { id: "collection", label: "Collection", kind: "chips",
    options: [["owned", "Owned"], ["unowned", "Not owned"], ["wanted", "Wanted"]],
    term: (v) => v.map((x) => `is:${x}`).join(" ") },
];

// What each section already filters by itself: a field id or a chip value.
const _FILTER_OMIT = { invQ: ["owned", "unowned"], wlQ: ["wanted"], bsQ: ["owned", "unowned", "format"] };

function _filterQuery(values) {
  return _FILTER_FIELDS.map((f) => values[f.id] ? f.term(values[f.id]) : "").filter(Boolean).join(" ");
}

const _F_COLOR_NAMES = { w: "White", u: "Blue", b: "Black", r: "Red", g: "Green", c: "Colorless" };

function _filterRow(f, omit) {
  const id = `ff-${f.id}`;
  const chip = ([v, label, cls]) => omit.includes(v) ? ""
    : `<button type="button" class="chip ${cls || ""}" data-v="${v}" aria-pressed="false">${label}</button>`;
  const pip = (c) => `<button type="button" class="ci-btn" data-v="${c}" aria-pressed="false" aria-label="${_F_COLOR_NAMES[c]}"><img src="https://svgs.scryfall.io/card-symbols/${c.toUpperCase()}.svg" alt="" width="20" height="20"></button>`;
  let ctl;
  if (f.kind === "text") {
    ctl = `<div class="filter-text"><input type="text" id="${id}" autocomplete="off" spellcheck="false" autocapitalize="off"><ul id="${id}-ac" class="ac-dropdown" role="listbox" hidden></ul></div>`;
  } else if (f.kind === "select") {
    ctl = `<select id="${id}"><option value=""></option>${f.options.map((o) => `<option>${o}</option>`).join("")}</select>`;
  } else if (f.kind === "compare") {
    const keys = f.keys.length > 1
      ? `<select data-part="key" aria-label="${f.label}">${f.keys.map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select>`
      : `<input type="hidden" data-part="key" value="${f.keys[0][0]}">`;
    ctl = `${keys}<select data-part="op" aria-label="Comparison">${["<=", "<", "=", ">=", ">"].map((o) => `<option${o === "<=" ? " selected" : ""}>${o}</option>`).join("")}</select>`
      + `<input type="number" id="${id}" data-part="n" inputmode="decimal" min="0" step="any">`;
  } else {
    const opts = f.kind === "colors" ? f.pips.map(pip).join("") : f.options.map(chip).join("");
    if (!opts.trim()) return "";
    const ops = f.ops ? `<select data-part="op" aria-label="Color match">${f.ops.map(([o, l]) => `<option value="${o}">${l}</option>`).join("")}</select>` : "";
    ctl = `<div class="chips" role="group" aria-labelledby="${id}">${opts}</div>${ops}`;
  }
  const label = f.kind === "chips" || f.kind === "colors"
    ? `<span id="${id}" class="filter-label">${f.label}</span>`
    : `<label for="${id}" class="filter-label">${f.label}</label>`;
  return `<div class="filter-row" data-field="${f.id}">${label}<div class="filter-ctl">${ctl}</div></div>`;
}

function _filterValues(form) {
  const values = {};
  for (const row of form.querySelectorAll("[data-field]")) {
    const f = _FILTER_FIELDS.find((x) => x.id === row.dataset.field);
    const part = (p) => row.querySelector(`[data-part="${p}"]`)?.value;
    const on = [...row.querySelectorAll('[aria-pressed="true"]')].map((b) => b.dataset.v);
    if (f.kind === "compare") values[f.id] = { key: part("key"), op: part("op"), n: part("n").trim() };
    else if (f.kind === "colors" && f.ops) values[f.id] = { op: part("op"), on };
    else if (f.kind === "colors" || f.kind === "chips") values[f.id] = on;
    else values[f.id] = row.querySelector("input, select").value.trim();
  }
  return values;
}

// One form per input, kept so reopening shows the last choices.
const _filterForms = new Map();
let _filterTarget = null;

function _filterForm(input) {
  let entry = _filterForms.get(input.id);
  if (entry) return entry;
  const omit = _FILTER_OMIT[input.id] || [];
  const form = document.createElement("form");
  form.className = "filter-form";
  form.innerHTML = _FILTER_FIELDS.filter((f) => !omit.includes(f.id)).map((f) => _filterRow(f, omit)).join("");
  const preview = document.getElementById("filterPreview");
  const update = () => { preview.textContent = _filterQuery(_filterValues(form)); };
  for (const f of _FILTER_FIELDS) {
    const el = form.querySelector(`#ff-${f.id}`);
    if (f.suggest && el) attachSuggest(el, form.querySelector(`#ff-${f.id}-ac`), f.suggest, update);
  }
  form.addEventListener("input", update);
  form.addEventListener("click", (e) => {
    const b = e.target.closest("button[aria-pressed]");
    if (!b) return;
    b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true"));
    update();
  });
  form.addEventListener("submit", (e) => { e.preventDefault(); _filterApply(); });
  entry = { form, update, applied: "" };
  _filterForms.set(input.id, entry);
  return entry;
}

function _filterOpen(input) {
  _filterTarget = input;
  _fTypeList().catch(() => { _fTypes = null; });
  _fSetList().catch(() => { _fSets = null; });
  const { form, update } = _filterForm(input);
  document.getElementById("filterFields").replaceChildren(form);
  update();
  document.getElementById("filterDialog").showModal();
}

// Replaces what the popup wrote last time and keeps anything typed by hand.
function _filterApply() {
  const input = _filterTarget;
  const entry = _filterForms.get(input.id);
  const built = _filterQuery(_filterValues(entry.form));
  const typed = entry.applied ? input.value.replace(entry.applied, "") : input.value;
  input.value = [typed.trim(), built].filter(Boolean).join(" ");
  entry.applied = built;
  document.getElementById("filterDialog").close();
  if (input.form) input.form.requestSubmit();
  else input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
}

function _filterClear() {
  const { form, update } = _filterForms.get(_filterTarget.id);
  form.reset();
  for (const b of form.querySelectorAll('[aria-pressed="true"]')) b.setAttribute("aria-pressed", "false");
  update();
}

document.addEventListener("click", (e) => {
  const opener = e.target.closest("[data-filter-for]");
  if (opener) { _filterOpen(document.getElementById(opener.dataset.filterFor)); return; }
  if (e.target.closest("#filterApply")) _filterApply();
  else if (e.target.closest("#filterClear")) _filterClear();
});
