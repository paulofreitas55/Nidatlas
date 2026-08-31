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
      loadTranslations(),
    ]);
    state.all = await speciesResp.json();
    state.taxaLabels = await labelsResp.json();
  } catch (err) {
    // Can't localize this one: the fetch that failed is the same
    // Promise.all that was also trying to load i18n.json itself, so there's
    // no translation table available to render an error message from.
    statusEl.textContent = "Could not load species list.";
    return;
  }

  const lang = initLangSwitch((newLang) => applyLang(newLang));
  buildQuickNav(state.all);
  applyLang(lang);
  document.getElementById("search-box").addEventListener("input", applyFilterAndRender);

  updateHeaderHeightVar();
  window.addEventListener("resize", updateHeaderHeightVar);
}

// Everything that needs to change when the language switches: static
// data-i18n bindings, the footer, document metadata, and every dynamically
// rendered piece of UI (quick-nav labels, the species list itself). Called
// once on load (with the detected/saved language) and again on every
// lang-switch click.
function applyLang(lang) {
  state.lang = lang;
  document.documentElement.lang = lang;
  document.title = t("page.index_title", lang);
  applyStaticTranslations(lang);
  renderFooter(lang);
  applyFilterAndRender();
}

// --- Sticky header height, kept live for scroll-margin-top (see quick-nav
// click handler and .order-group in style.css) ---
//
// The header's real height isn't a constant: header-row goes from a wrapped
// column (title+lang, then search, on two lines) to a single row at the
// 640px breakpoint, and quick-nav itself can wrap onto a second line at
// narrow widths or with enough orders. A CSS value baked in ahead of time
// can't track that, so it's measured from the live DOM instead.
function updateHeaderHeightVar() {
  const header = document.querySelector(".site-header");
  document.documentElement.style.setProperty("--header-h", `${header.offsetHeight}px`);
}

// --- Language (see lang.js for detectDefaultLang / initLangSwitch / pickLabel) ---

function commonNameFor(sp, lang) {
  return pickLabel({ pt: sp.common_name_pt, es: sp.common_name_es, en: sp.common_name_en }, lang);
}

// --- Search ---
//
// Matching stays cross-language always (searching "melro" must still find
// Turdus merula with the interface set to English) -- restricting to the
// active language would make the search feel broken for anyone whose
// interface language differs from the name they remember. When a result
// only matched because of a name in a language that ISN'T currently shown
// on its card, matchInfo below surfaces which name and which language, so
// the result doesn't look like it appeared for no reason.

function applyFilterAndRender() {
  const q = document.getElementById("search-box").value.trim().toLowerCase();
  const filtered = q ? state.all.filter((s) => matchesQuery(s, q)) : state.all;
  render(filtered, q);
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

// Returns {name, lang} for a vernacular name that matched the query in a
// language other than the active one, or null if the match is already
// self-evident from what the card shows (scientific name, always visible;
// or the active-language common name, if the card has one). Only the
// active language's OWN vernacular field counts as "already shown" --
// bioclip_name is a second scientific-name variant (not shown on the card,
// but still a scientific name, not something a language tag makes sense
// for), so a bioclip_name-only match is treated the same as a
// gbif_name-only match: self-evident, no tag.
function matchInfo(sp, q, lang) {
  if (sp.gbif_name.toLowerCase().includes(q)) return null;
  if (sp.bioclip_name.toLowerCase().includes(q)) return null;

  const shown = commonNameFor(sp, lang);
  if (shown && shown.toLowerCase().includes(q)) return null;

  for (const candidateLang of LANGS) {
    if (candidateLang === lang) continue;
    const name = sp[`common_name_${candidateLang}`];
    if (name && name.toLowerCase().includes(q)) {
      return { name, lang: candidateLang };
    }
  }
  return null;
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
        // Re-measure right before scrolling, not just on load/resize -- the
        // header can change height between those events and this click
        // (e.g. a web font swap reflowing quick-nav onto a second line), and
        // scroll-margin-top needs to be correct at the moment the browser
        // reads it, not just whenever it was last set.
        updateHeaderHeightVar();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
    nav.appendChild(link);
  }
}

// --- Rendering ---

function render(species, query) {
  const container = document.getElementById("species-list");
  const statusEl = document.getElementById("status");
  container.innerHTML = "";

  statusEl.textContent =
    species.length === state.all.length
      ? tPlural("atlas.status_all", state.all.length, state.lang, { count: state.all.length })
      : tPlural("atlas.status_filtered", species.length, state.lang, {
          count: species.length,
          total: state.all.length,
        });

  if (species.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = t("atlas.no_match", state.lang);
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

    familyGrid.appendChild(renderCard(sp, query));
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

function renderCard(sp, query) {
  const a = document.createElement("a");
  a.className = "species-card";
  a.href = `species.html?id=${sp.id}`;

  const dex = document.createElement("span");
  dex.className = "card-dex";
  dex.textContent = "#" + String(sp.dex_number).padStart(3, "0");
  a.appendChild(dex);

  a.appendChild(buildPhotoThumb(sp, "card-thumb"));

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

  const credit = buildPhotoCredit(sp, state.lang, { compact: true });
  if (credit) a.appendChild(credit);

  if (query) {
    const match = matchInfo(sp, query, state.lang);
    if (match) {
      const tagEl = document.createElement("span");
      tagEl.className = "card-match-tag";
      tagEl.textContent = t("atlas.match_tag", state.lang, {
        name: match.name,
        lang: match.lang.toUpperCase(),
      });
      a.appendChild(tagEl);
    }
  }

  return a;
}

init();
