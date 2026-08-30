const state = {
  all: [],
  taxaLabels: { orders: {}, families: {} },
  lang: "en",
};

async function init() {
  const statusEl = document.getElementById("status");
  try {
    const [speciesResp, labelsResp] = await Promise.all([
      fetch("/api/species/all"),
      fetch("/taxa_labels.json"),
    ]);
    state.all = await speciesResp.json();
    state.taxaLabels = await labelsResp.json();
  } catch (err) {
    statusEl.textContent = "Could not load species list.";
    return;
  }

  state.lang = initLangSwitch((lang) => {
    state.lang = lang;
    applyFilterAndRender();
  });
  buildQuickNav(state.all);
  applyFilterAndRender();
  document.getElementById("search-box").addEventListener("input", applyFilterAndRender);
}

// --- Language (see lang.js for detectDefaultLang / initLangSwitch / pickLabel) ---

function commonNameFor(sp, lang) {
  return pickLabel({ pt: sp.common_name_pt, es: sp.common_name_es, en: sp.common_name_en }, lang);
}

// --- Search ---

function applyFilterAndRender() {
  const q = document.getElementById("search-box").value.trim().toLowerCase();
  const filtered = q ? state.all.filter((s) => matchesQuery(s, q)) : state.all;
  render(filtered);
}

function matchesQuery(sp, q) {
  return (
    sp.gbif_name.toLowerCase().includes(q) ||
    sp.bioclip_name.toLowerCase().includes(q) ||
    (sp.common_name_pt && sp.common_name_pt.toLowerCase().includes(q)) ||
    (sp.common_name_es && sp.common_name_es.toLowerCase().includes(q)) ||
    (sp.common_name_en && sp.common_name_en.toLowerCase().includes(q))
  );
}

// --- Quick-nav ---

function orderSlug(order) {
  return "order-" + order.toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

function buildQuickNav(species) {
  const nav = document.getElementById("quick-nav");
  nav.innerHTML = "";
  const seen = new Set();

  for (const sp of species) {
    if (seen.has(sp.order)) continue;
    seen.add(sp.order);

    const link = document.createElement("a");
    link.href = `#${orderSlug(sp.order)}`;
    link.textContent = sp.order;
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const target = document.getElementById(orderSlug(sp.order));
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
    nav.appendChild(link);
  }
}

// --- Rendering ---

function render(species) {
  const container = document.getElementById("species-list");
  const statusEl = document.getElementById("status");
  container.innerHTML = "";

  statusEl.textContent =
    species.length === state.all.length
      ? `${state.all.length} species`
      : `${species.length} of ${state.all.length} species`;

  if (species.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No species match your search.";
    container.appendChild(empty);
    return;
  }

  let currentOrder = null;
  let currentFamily = null;
  let orderBody = null;
  let familyGrid = null;

  for (const sp of species) {
    if (sp.order !== currentOrder) {
      currentOrder = sp.order;
      currentFamily = null;
      const { section, body } = renderOrderSection(sp.order);
      orderBody = body;
      container.appendChild(section);
    }

    if (sp.family !== currentFamily) {
      currentFamily = sp.family;
      const { box, grid } = renderFamilyBox(sp.family);
      familyGrid = grid;
      orderBody.appendChild(box);
    }

    familyGrid.appendChild(renderCard(sp));
  }
}

function renderOrderSection(order) {
  const section = document.createElement("section");
  section.className = "order-group";
  section.id = orderSlug(order);

  const header = document.createElement("div");
  header.className = "order-header";

  const name = document.createElement("span");
  name.className = "order-name";
  name.textContent = order;
  header.appendChild(name);

  const desc = pickLabel(state.taxaLabels.orders[order] || {}, state.lang);
  if (desc) {
    const descEl = document.createElement("span");
    descEl.className = "order-desc";
    descEl.textContent = desc;
    header.appendChild(descEl);
  }

  section.appendChild(header);

  const body = document.createElement("div");
  body.className = "order-body";
  section.appendChild(body);

  return { section, body };
}

function renderFamilyBox(family) {
  const box = document.createElement("div");
  box.className = "family-box";

  const header = document.createElement("div");
  header.className = "family-header";

  const name = document.createElement("span");
  name.className = "family-name";
  name.textContent = family;
  header.appendChild(name);

  const desc = pickLabel(state.taxaLabels.families[family] || {}, state.lang);
  if (desc) {
    const descEl = document.createElement("span");
    descEl.className = "family-desc";
    descEl.textContent = desc;
    header.appendChild(descEl);
  }

  box.appendChild(header);

  const grid = document.createElement("div");
  grid.className = "card-grid";
  box.appendChild(grid);

  return { box, grid };
}

function renderCard(sp) {
  const a = document.createElement("a");
  a.className = "species-card";
  a.href = `species.html?id=${sp.id}`;

  const dex = document.createElement("span");
  dex.className = "card-dex";
  dex.textContent = "#" + String(sp.dex_number).padStart(3, "0");
  a.appendChild(dex);

  const thumb = document.createElement("span");
  thumb.className = "card-thumb";
  a.appendChild(thumb);

  const name = document.createElement("span");
  name.className = "card-name";
  name.textContent = sp.gbif_name;
  a.appendChild(name);

  const common = commonNameFor(sp, state.lang);
  if (common) {
    const commonEl = document.createElement("span");
    commonEl.className = "card-common";
    commonEl.textContent = common;
    a.appendChild(commonEl);
  }

  return a;
}

init();
