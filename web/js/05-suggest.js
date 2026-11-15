// Server-ranked name suggestions under `input`, rendered into `list` (a
// .ac-dropdown <ul>). Not a <datalist>: browsers re-filter those by substring,
// hiding fuzzy hits ("yshtola" never showed "Y'shtola, Night's Blessed").
// `fetchNames(q)` resolves to names, or null to show nothing.
function attachSuggest(input, list, fetchNames, onPick) {
  let timer = null;
  let seq = 0;
  let items = [];
  let active = -1;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", list.id);
  input.setAttribute("aria-expanded", "false");

  function hide() {
    seq++;
    list.hidden = true;
    list.innerHTML = "";
    items = [];
    active = -1;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function render() {
    if (items.length === 0) { hide(); return; }
    list.innerHTML = items.map((name, i) => {
      const on = i === active;
      return `<li id="${list.id}-${i}" role="option" class="ac-item${on ? " active" : ""}" data-name="${escapeHtml(name)}" aria-selected="${on}">${escapeHtml(name)}</li>`;
    }).join("");
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    if (active >= 0) input.setAttribute("aria-activedescendant", `${list.id}-${active}`);
    else input.removeAttribute("aria-activedescendant");
  }

  function pick(name) {
    input.value = name;
    hide();
    onPick(name);
  }

  input.addEventListener("input", () => {
    const q = input.value.trim();
    const mine = ++seq;
    if (timer) clearTimeout(timer);
    if (q.length < 2) { hide(); return; }
    timer = setTimeout(async () => {
      let names = null;
      try { names = await fetchNames(q); } catch (_e) { names = null; }
      if (mine !== seq) return;
      items = (names || []).slice(0, 20);
      active = -1;
      render();
    }, 200);
  });

  input.addEventListener("keydown", (e) => {
    if (list.hidden || items.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      active = (active + 1) % items.length;
      render();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      active = (active - 1 + items.length) % items.length;
      render();
    } else if (e.key === "Enter" && active >= 0) {
      e.preventDefault();
      pick(items[active]);
    } else if (e.key === "Escape") {
      // Close only: a type=search input would otherwise also clear itself.
      e.preventDefault();
      e.stopPropagation();
      hide();
    } else if (e.key === "Tab" && active >= 0) {
      e.preventDefault();
      input.value = items[active];
      hide();
    }
  });

  // Delay so a mousedown on an item still lands before the list hides.
  input.addEventListener("blur", () => setTimeout(hide, 120));

  list.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li.ac-item[data-name]");
    if (!li) return;
    e.preventDefault();
    pick(li.dataset.name);
  });

  return { hide };
}
