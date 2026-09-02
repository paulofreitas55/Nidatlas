// Rank view (rank.html): species ordered strictly by total_occurrences --
// raw GBIF record counts, no per-area normalisation (that's what the Map
// view's concentration ranking is for -- see rank.explainer in i18n.json).
// GET /api/species/ranking returns all 584 species pre-sorted, each already
// carrying the same RANK() OVER (ORDER BY total_occurrences DESC) value
// shown as "X of 584" on the species page, so a species' position here
// always agrees with its own page.

const TOP_BOTTOM_COUNT = 50;

const state = {
  lang: "en",
  ranking: [], // full 584-row response, sorted most- to least-recorded
  showingFull: false,
  fullSearchQuery: "", // filters the full-ranking list only; kept in state (not just the input's value) so a lang switch mid-search re-renders the same filtered set instead of silently clearing it
};

async function init() {
  const statusEl = document.getElementById("load-status");

  try {
    await loadTranslations();
  } catch (err) {
    statusEl.textContent = "Could not load the ranking.";
    return;
  }

  state.lang = initLangSwitch((lang) => {
    state.lang = lang;
    document.documentElement.lang = lang;
    document.title = t("page.rank_title", lang);
    applyStaticTranslations(lang);
    renderFooter(lang);
    updateToggleButton();
    if (state.ranking.length) render();
  });
  document.documentElement.lang = state.lang;
  document.title = t("page.rank_title", state.lang);
  applyStaticTranslations(state.lang);
  renderFooter(state.lang);
  applyFeatureFlags();

  try {
    const response = await fetch("/api/species/ranking");
    if (!response.ok) throw new Error(String(response.status));
    state.ranking = await response.json();
  } catch (err) {
    statusEl.textContent = t("rank.error_load", state.lang);
    return;
  }

  statusEl.hidden = true;
  document.getElementById("rank-content").hidden = false;

  render();
  document.getElementById("rank-toggle").addEventListener("click", () => {
    state.showingFull = !state.showingFull;
    updateToggleButton();
    render();
    if (state.showingFull) {
      // Without this, the page stays scrolled wherever the toggle button
      // happened to be (below 50 rows), which now lands somewhere in the
      // MIDDLE of the full 584-row list instead of at rank #1 -- jarring
      // since nothing about clicking "view full ranking" should imply
      // landing anywhere other than the top of it.
      window.scrollTo({ top: 0, behavior: "auto" });
    }
  });

  const searchInput = document.getElementById("rank-full-search");
  searchInput.addEventListener("input", () => {
    state.fullSearchQuery = searchInput.value;
    renderFullList();
  });
}

function updateToggleButton() {
  const btn = document.getElementById("rank-toggle");
  btn.textContent = t(state.showingFull ? "rank.view_top_bottom" : "rank.view_full", state.lang);
}

function commonNameFor(sp, lang) {
  return pickLabel({ pt: sp.common_name_pt, es: sp.common_name_es, en: sp.common_name_en }, lang);
}

function render() {
  document.getElementById("rank-columns").hidden = state.showingFull;
  document.getElementById("rank-full").hidden = !state.showingFull;

  if (state.showingFull) {
    renderFullList();
  } else {
    renderList(document.getElementById("rank-most-list"), state.ranking.slice(0, TOP_BOTTOM_COUNT));
    renderList(document.getElementById("rank-least-list"), state.ranking.slice(-TOP_BOTTOM_COUNT).reverse());
  }
}

// Cross-language, substring match on scientific or vernacular name -- same
// shape as the atlas grid's own search (matchesQuery in app.js), just
// scoped to this one list instead of the whole page.
function matchesFullSearch(sp, query) {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return (
    sp.gbif_name.toLowerCase().includes(q) ||
    (sp.common_name_pt && sp.common_name_pt.toLowerCase().includes(q)) ||
    (sp.common_name_es && sp.common_name_es.toLowerCase().includes(q)) ||
    (sp.common_name_en && sp.common_name_en.toLowerCase().includes(q))
  );
}

function renderFullList() {
  const listEl = document.getElementById("rank-full-list");
  const rows = state.ranking.filter((sp) => matchesFullSearch(sp, state.fullSearchQuery));

  listEl.innerHTML = "";
  if (rows.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty-state";
    empty.textContent = t("atlas.no_match", state.lang);
    listEl.appendChild(empty);
    return;
  }
  for (const sp of rows) {
    listEl.appendChild(renderRow(sp));
  }
}

function renderList(listEl, rows) {
  listEl.innerHTML = "";
  for (const sp of rows) {
    listEl.appendChild(renderRow(sp));
  }
}

function renderRow(sp) {
  const li = document.createElement("li");

  const a = document.createElement("a");
  a.className = "rank-row-link";
  a.href = `/species/${sp.id}`;

  const rank = document.createElement("span");
  rank.className = "rank-number";
  rank.textContent = `#${sp.rank}`;
  a.appendChild(rank);

  a.appendChild(buildPhotoThumb(sp, "card-thumb rank-thumb"));

  const info = document.createElement("span");
  info.className = "rank-info";

  const name = document.createElement("span");
  name.className = "card-name";
  name.textContent = sp.gbif_name;
  info.appendChild(name);

  const common = commonNameFor(sp, state.lang);
  if (common) {
    const commonEl = document.createElement("span");
    commonEl.className = "card-common";
    commonEl.textContent = common;
    info.appendChild(commonEl);
  }

  const credit = buildPhotoCredit(sp, state.lang);
  if (credit) info.appendChild(credit);
  a.appendChild(info);

  const count = document.createElement("span");
  count.className = "rank-count";
  count.textContent = sp.total_occurrences.toLocaleString();
  a.appendChild(count);

  li.appendChild(a);
  return li;
}

init();
