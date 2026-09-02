// MONTH_NAMES, DENSITY_COLORS, LAND_FILL/OUTLINE, BORDER_COLOR, MAINLAND_BOUNDS,
// PANEL_GROUPS (the archipelago-framing groups, aliased below to this file's
// pre-existing REGIONS name), findPanelGroup, padBounds, panelGroupForLatLng,
// createPanelMap, computeLogBins/binIndexForValue, renderLegend,
// renderCellCircles and the GBIF-link helpers all now live in
// static/common.js (loaded before this file) -- see that file for their
// definitions and rationale. These three aliases exist only so the rest of
// this file, which predates the region map and its separate, administrative
// sense of "region", doesn't need renaming throughout.
const REGIONS = PANEL_GROUPS;
const findRegion = findPanelGroup;
const regionForLatLng = panelGroupForLatLng;

// A species confined to (or dominated by) a single archipelago (e.g.
// Regulus madeirensis) should open directly on that archipelago, not on an
// empty-looking mainland view. Picking by which region has the most cells,
// not just "any mainland cell present", matters in practice: Phylloscopus
// canariensis has 116 Canary Islands cells and a single stray vagrant record
// near Madrid (occurrences=4) -- an "any mainland cell wins" rule would let
// that one record bury the Canary Islands view the page should actually
// open on. Ties favor mainland via REGIONS' declaration order. Runs once, at
// initial load only -- neither manual region clicks nor month-filter
// changes re-trigger this, so the view never jumps out from under the user.
function determineDefaultRegion(cells) {
  const counts = new Map();
  for (const cell of cells) {
    const regionId = regionForLatLng(cell.centroid_lat, cell.centroid_lon);
    if (regionId) counts.set(regionId, (counts.get(regionId) || 0) + 1);
  }
  let best = "mainland";
  let bestCount = 0;
  for (const region of REGIONS) {
    const count = counts.get(region.id) || 0;
    if (count > bestCount) {
      best = region.id;
      bestCount = count;
    }
  }
  return best;
}

const state = {
  id: null,
  profile: null,
  taxaLabels: { orders: {}, families: {} },
  lang: "en",
  errorKey: null, // set when load-status is showing an error, so a later lang switch can re-translate it
  selectedMonth: null, // null = All
  iberiaGeoJson: null,
  borderGeoJson: null,
  panelMaps: {}, // panel id -> Leaflet map instance
  circleLayers: {}, // panel id -> current L.layerGroup of circles
  currentRegionId: null,
  cells: [],
  legendControl: null,
  legendMap: null,
  phyloNeighbourhood: null, // last GET /api/species/{id}/phylo-neighbourhood response, kept for lang-switch re-renders with no refetch
};

function showError(key) {
  state.errorKey = key;
  document.getElementById("load-status").textContent = t(key, state.lang);
}

async function init() {
  // Canonical URL is now the clean /species/{id} path (see the FastAPI route
  // in src/api.py, which server-renders this same species.html with
  // per-species metadata injected). The old ?id= query string is kept as a
  // fallback purely so this file still works if ever opened directly against
  // a static copy without the server-side route in front of it -- every real
  // link in the app (and /species.html?id=N itself, via a 301) now points at
  // the path form.
  const pathMatch = location.pathname.match(/\/species\/(\d+)/);
  const params = new URLSearchParams(location.search);
  state.id = pathMatch ? pathMatch[1] : params.get("id");
  const statusEl = document.getElementById("load-status");

  try {
    await loadTranslations();
  } catch (err) {
    // Can't localize this one: i18n.json itself is what failed to fetch.
    statusEl.textContent = "Could not load species.";
    return;
  }

  state.lang = initLangSwitch((lang) => {
    state.lang = lang;
    document.documentElement.lang = lang;
    applyStaticTranslations(lang);
    renderFooter(lang);
    if (state.errorKey) {
      document.title = t("page.species_fallback_title", state.lang);
      showError(state.errorKey);
    } else if (state.profile) {
      document.title = `${state.profile.gbif_name} — Nidatlas`;
      renderIdentity();
      renderKeyFigures();
      renderSeasonality();
      initMonthFilter();
      relabelMapUI();
      renderCellsIntoCurrentPanels(); // rebuilds popups so their text follows the new language too
      const statusEl = document.getElementById("map-status");
      if (statusEl.textContent) {
        statusEl.textContent =
          state.cells.length === 0
            ? t("map.no_records", state.lang)
            : tPlural("map.record_count", state.cells.length, state.lang, {
                count: state.cells.length.toLocaleString(),
              });
      }
      if (state.phyloNeighbourhood) renderPhylogeny(); // re-render with the new language's vernacular names, no refetch
    }
  });
  document.documentElement.lang = state.lang;
  document.title = t("page.species_fallback_title", state.lang); // real species name replaces this once loaded
  applyStaticTranslations(state.lang);
  renderFooter(state.lang);
  applyFeatureFlags();

  if (!state.id) {
    showError("species.error_no_id");
    return;
  }

  let profileResp;
  try {
    [profileResp, state.taxaLabels, state.iberiaGeoJson, state.borderGeoJson] = await Promise.all([
      fetch(`/api/species/${state.id}`).then((r) => (r.ok ? r.json() : Promise.reject(r.status))),
      fetch("/taxa_labels.json").then((r) => r.json()),
      fetch("/iberia.geojson").then((r) => r.json()),
      fetch("/pt_es_border.geojson").then((r) => r.json()),
    ]);
  } catch (err) {
    showError(err === 404 ? "species.error_not_found" : "species.error_load");
    return;
  }
  state.profile = profileResp;
  document.title = `${state.profile.gbif_name} — Nidatlas`;

  // The static, server-rendered summary paragraph (see <!--SEO_SUMMARY--> in
  // src/api.py's species page renderer) exists so a crawler that doesn't run
  // JS still sees real content -- once the real interactive page has loaded,
  // it's redundant for an actual visitor and comes out.
  document.getElementById("seo-summary")?.remove();

  statusEl.hidden = true;
  for (const id of ["identity", "key-figures", "seasonality", "distribution", "phylogeny"]) {
    document.getElementById(id).hidden = false;
  }

  renderIdentity();
  renderKeyFigures();
  renderSeasonality();
  initRegionSelector();
  initMonthFilter();
  loadPhylogeny(); // independent of the distribution map below -- don't block on it either way
  await loadCells(state.selectedMonth);
}

// --- Phylogeny ("Position in the tree of life") ---
//
// One fetch: GET /api/species/{id}/phylo-neighbourhood already returns a
// pruned node list -- the species' own tip, its closest handful of
// relatives, and the branch points connecting them, with anything further
// away collapsed server-side into a "+N more species" placeholder (see
// phylo_species_neighbourhood in src/queries.py for why this is NOT the
// same as the full induced subtree of clade_node_id: that subtree can pull
// in hundreds of unrelated tips for a species whose nearest relatives, by
// raw tree-hop count, happen to sit in a sparsely-sampled corner of the
// Open Tree synthesis). clade_node_id is still returned, unchanged in
// meaning from the old /relatives-based version -- it's the MRCA of the
// species and its listed relatives, used only as the scroll/highlight
// target for the "open in full tree" link below, not as a subtree to draw.

async function loadPhylogeny() {
  const statusEl = document.getElementById("phylo-status");
  statusEl.textContent = t("species.phylo_loading", state.lang);

  try {
    // r.ok check matters here specifically: a 404's JSON body
    // ({"detail": "..."}) parses just fine and has no clade_node_id, which
    // renderPhylogeny would otherwise misread as "this species has no tree
    // placement" instead of a real request failure (e.g. an old server
    // process still running without this route).
    state.phyloNeighbourhood = await fetch(`/api/species/${state.id}/phylo-neighbourhood?limit=6`).then(
      (r) => (r.ok ? r.json() : Promise.reject(r.status))
    );
  } catch (err) {
    state.phyloNeighbourhood = null;
    statusEl.textContent = t("species.phylo_error", state.lang);
    return;
  }
  renderPhylogeny();
}

function renderPhylogeny() {
  const statusEl = document.getElementById("phylo-status");
  const canvasEl = document.getElementById("phylo-canvas");
  const linkEl = document.getElementById("phylo-open-tree-link");
  const nb = state.phyloNeighbourhood;

  if (!nb || nb.clade_node_id == null) {
    // A real species can legitimately have no placement at all (see
    // CLAUDE.md) -- that's a normal outcome, not the same as a fetch error,
    // so it gets its own explanatory copy rather than the generic error one.
    statusEl.textContent = nb && nb.clade_node_id == null
      ? t("species.phylo_no_placement", state.lang)
      : t("species.phylo_error", state.lang);
    canvasEl.hidden = true;
    linkEl.hidden = true;
    return;
  }

  statusEl.textContent = "";
  linkEl.hidden = false;
  linkEl.href = `/tree?node=${nb.clade_node_id}`;

  const nodesById = buildNeighbourhoodNodes(nb.nodes);
  canvasEl.innerHTML = "";
  canvasEl.hidden = false;
  // Deliberately more compact than tree.js's full-tree defaults (colWidth
  // 190, labelMargin 300): this view is already pruned down to a handful of
  // tips (see phylo_species_neighbourhood), but a species whose closest
  // relatives sit across several REAL nested splits -- not just OToL's
  // synthesis wrapper chains, see that function's own docstring -- can still
  // run 8-11 columns deep. A smaller column width, paired with
  // #phylo-canvas's own smaller cladogram-label font-size and the
  // max-width:100%/height:auto responsive scaling in species.css, keeps the
  // whole thing scaling down to fit any viewport width instead of ever
  // needing horizontal scroll.
  canvasEl.appendChild(
    renderCladogram(nb.clade_node_id, nodesById, {
      highlightId: nb.node_id,
      colWidth: 70,
      labelMargin: 170,
      rowHeight: 24,
    })
  );
}

// Builds cladogram.js's expected node map from a GET
// /api/species/{id}/phylo-neighbourhood response -- static, non-interactive
// here (no clickable/onNodeClick): this widget's only way to reach the full
// tree is the explicit "open in full tree" link, not clicking into the mini
// view. A synthetic is_summary node (the "+N more species" marker for
// whatever this neighbourhood collapsed away) renders via cladogram.js's
// existing `collapsed` flag -- built for a user manually collapsing a clade
// in the full tree view, but the same filled-marker/no-children visual is
// exactly right here for a branch this view never expands at all.
function buildNeighbourhoodNodes(rawNodes) {
  const childrenByParent = {};
  for (const n of rawNodes) {
    if (n.parent_id != null) {
      (childrenByParent[n.parent_id] = childrenByParent[n.parent_id] || []).push(n.id);
    }
  }

  const nodesById = {};
  for (const n of rawNodes) {
    let label;
    let href = null;
    let muted = false;
    let collapsed = false;
    if (n.is_summary) {
      label = tPlural("species.phylo_more_species", n.excluded_species_count, state.lang, {
        count: n.excluded_species_count.toLocaleString(),
      });
      muted = true;
      collapsed = true;
    } else if (n.is_tip && n.species_id != null) {
      const vernacular = pickLabel(
        { pt: n.common_name_pt, es: n.common_name_es, en: n.common_name_en },
        state.lang
      );
      label = vernacular ? `${n.gbif_name} — ${vernacular}` : n.gbif_name;
      href = `/species/${n.species_id}`;
    } else if (n.is_tip) {
      label = t("tree.unresolved_tip", state.lang);
      muted = true;
    } else {
      // Unnamed internal node: no placeholder text -- same as tree.js's full
      // view, the branch point itself is the information (cladogram.js's
      // addLabel skips rendering entirely when label is empty).
      label = n.name || "";
    }
    nodesById[n.id] = {
      id: n.id,
      children: collapsed ? [] : childrenByParent[n.id] || [],
      label,
      isTip: n.is_tip,
      href,
      clickable: false,
      muted,
      collapsed,
    };
  }
  return nodesById;
}

// --- Identity & key figures ---

function renderIdentity() {
  const p = state.profile;

  const photoWrap = document.getElementById("species-photo-wrap");
  photoWrap.innerHTML = "";
  photoWrap.appendChild(buildPhotoThumb(p, "thumb-placeholder"));
  if (p.image_attribution) {
    const credit = document.createElement("p");
    credit.className = "species-photo-credit";

    const attributionEl = document.createElement("span");
    attributionEl.textContent = p.image_attribution;
    credit.appendChild(attributionEl);

    if (p.image_source_url) {
      const link = document.createElement("a");
      link.className = "species-photo-link";
      link.href = p.image_source_url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = t("species.photo_view_original", state.lang);
      credit.appendChild(link);
    }
    photoWrap.appendChild(credit);
  }

  document.getElementById("dex-number").textContent = "#" + String(p.dex_number).padStart(3, "0");
  document.getElementById("species-name").textContent = p.gbif_name;

  const vernacular = pickLabel(
    { pt: p.common_name_pt, es: p.common_name_es, en: p.common_name_en },
    state.lang
  );
  document.getElementById("species-vernacular").textContent = vernacular || "";

  const orderLabel = pickLabel(state.taxaLabels.orders[p.order] || {}, state.lang);
  const familyLabel = pickLabel(state.taxaLabels.families[p.family] || {}, state.lang);
  const orderText = orderLabel ? `${p.order} (${orderLabel})` : p.order;
  const familyText = familyLabel ? `${p.family} (${familyLabel})` : p.family;
  document.getElementById("species-taxon").textContent = `${orderText} · ${familyText}`;
}

function renderKeyFigures() {
  const p = state.profile;
  document.getElementById("total-records").textContent = p.total_occurrences.toLocaleString();
  document.getElementById("global-rank").textContent = t("species.global_rank_value", state.lang, {
    rank: p.global_rank.rank,
    total: p.global_rank.total,
  });
  document.getElementById("commonness").textContent = t("species.commonness", state.lang, {
    percent: p.global_rank.percentile,
  });
}

// --- Seasonality (pure SVG/CSS, hover handled entirely by CSS) ---

function renderSeasonality() {
  const container = document.getElementById("monthly-chart");
  container.innerHTML = "";

  const monthly = state.profile.monthly_profile; // 12 entries, months 1..12 in order
  const maxShare = Math.max(...monthly.map((m) => m.share), 0.0001);

  const width = 300;
  const height = 110;
  const chartTop = 14;
  const chartBottom = 92;
  const chartHeight = chartBottom - chartTop;
  const barWidth = 16;
  const gap = (width - 12 * barWidth) / 13;

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "monthly-chart-svg");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", t("species.seasonality_chart_aria", state.lang));

  monthly.forEach((m, i) => {
    const x = gap + i * (barWidth + gap);
    const barHeight = Math.max(2, (m.share / maxShare) * chartHeight);
    const y = chartBottom - barHeight;

    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.setAttribute("class", "bar-group");

    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", x);
    rect.setAttribute("y", y);
    rect.setAttribute("width", barWidth);
    rect.setAttribute("height", barHeight);
    rect.setAttribute("rx", 2);
    rect.setAttribute("class", "bar-rect");
    g.appendChild(rect);

    const pct = document.createElementNS("http://www.w3.org/2000/svg", "text");
    pct.setAttribute("x", x + barWidth / 2);
    pct.setAttribute("y", Math.max(y - 4, 9));
    pct.setAttribute("text-anchor", "middle");
    pct.setAttribute("class", "bar-pct");
    pct.textContent = (m.share * 100).toFixed(1) + "%";
    g.appendChild(pct);

    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", x + barWidth / 2);
    label.setAttribute("y", height - 2);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("class", "bar-label");
    label.textContent = MONTH_NAMES[state.lang][i];
    g.appendChild(label);

    svg.appendChild(g);
  });

  container.appendChild(svg);
}

// --- Month filter ---

function initMonthFilter() {
  const container = document.getElementById("month-filter");
  container.innerHTML = "";

  const options = [{ label: t("species.month_all", state.lang), month: null }].concat(
    MONTH_NAMES[state.lang].map((label, i) => ({ label, month: i + 1 }))
  );

  for (const { label, month } of options) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "month-btn" + (month === state.selectedMonth ? " active" : "");
    btn.textContent = label;
    btn.addEventListener("click", () => {
      if (month === state.selectedMonth) return; // already showing this filter, no new data to fetch
      state.selectedMonth = month;
      initMonthFilter();
      loadCells(month);
    });
    container.appendChild(btn);
  }
}

// --- Map ---
//
// Panel-map construction (createPanelMap), the panel-group model
// (PANEL_GROUPS/REGIONS), and padBounds all live in static/common.js now --
// see that file for the full rationale (multi-panel archipelagos, Leaflet
// tuning, etc). destroyPanelMaps stays here since it tears down this page's
// own state shape (state.panelMaps/circleLayers/legendControl/legendMap).
function destroyPanelMaps() {
  for (const map of Object.values(state.panelMaps)) {
    map.remove();
  }
  state.panelMaps = {};
  state.circleLayers = {};
  state.legendControl = null;
  state.legendMap = null;
}

// Full rebuild: tears down and recreates the panel DOM + map instances for
// a region. Used for the initial load and manual region-selector clicks --
// not for month-filter changes, which reuse the existing panels (see
// renderCellsIntoCurrentPanels) so the maps don't flicker on every click.
function showRegion(regionId) {
  state.currentRegionId = regionId;
  const region = findRegion(regionId);

  destroyPanelMaps();
  const container = document.getElementById("map-panels");
  container.innerHTML = "";

  // See buildPanelDom's own comment (static/common.js) for why panel DOM is
  // built in a separate pass before any L.map is constructed.
  const mapEls = buildPanelDom(container, region.panels, state.lang);

  region.panels.forEach((panel, i) => {
    state.panelMaps[panel.id] = createPanelMap(
      mapEls[i], panel.bounds, state.iberiaGeoJson, state.borderGeoJson
    );
  });

  document.querySelectorAll(".region-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.region === regionId);
  });

  renderCellsIntoCurrentPanels();
}

function initRegionSelector() {
  const container = document.getElementById("region-selector");
  container.innerHTML = "";

  for (const region of REGIONS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "region-btn";
    btn.dataset.region = region.id;
    btn.textContent = t(region.labelKey, state.lang);
    btn.addEventListener("click", () => showRegion(region.id));
    container.appendChild(btn);
  }
}

// Re-labels the region-selector buttons and whatever panel labels are
// currently on screen, without touching the Leaflet map instances -- used
// on a language switch, where rebuilding every map would be both wasteful
// and visually jarring for something that's just a text change.
function relabelMapUI() {
  document.querySelectorAll(".region-btn").forEach((btn) => {
    const region = findRegion(btn.dataset.region);
    btn.textContent = t(region.labelKey, state.lang);
  });
  if (state.currentRegionId) {
    const region = findRegion(state.currentRegionId);
    const labelEls = document.querySelectorAll(".map-panel-label");
    region.panels.forEach((panel, i) => {
      if (labelEls[i]) labelEls[i].textContent = t(panel.labelKey, state.lang);
    });
  }
}

// computeLogBins, binIndexForValue, renderLegend, renderCellCircles,
// gbifSearchUrl and the CELL_CIRCLE_RADIUS_M/CUBE_*/METERS_PER_DEGREE_LAT
// constants they use all now live in static/common.js. popupHtml stays here:
// it's species-specific (links to a GBIF search for this one species).
function popupHtml(cell) {
  const gbifUrl = gbifSearchUrl(cell.centroid_lat, cell.centroid_lon, { q: state.profile.gbif_name });
  const recordsText = tPlural("map.popup_records", cell.occurrences, state.lang, {
    count: cell.occurrences.toLocaleString(),
  });
  const viewLink = t("map.popup_view_gbif", state.lang);
  return (
    `<div class="map-popup"><strong>${cell.mgrs_cell}</strong><br>` +
    `${recordsText}<br>` +
    `<a href="${gbifUrl}">${viewLink}</a></div>`
  );
}

// Renders state.cells into whatever panels currently exist for
// state.currentRegionId, without touching the panel DOM/map instances
// themselves -- used both by showRegion (right after it builds fresh panels)
// and by the month filter (which must NOT rebuild maps on every click, or
// every filter change would flicker/refit the view).
function renderCellsIntoCurrentPanels() {
  const region = findRegion(state.currentRegionId);
  const cells = state.cells;

  for (const layer of Object.values(state.circleLayers)) {
    layer.remove();
  }
  state.circleLayers = {};

  if (state.legendControl && state.legendMap) {
    state.legendMap.removeControl(state.legendControl);
    state.legendControl = null;
    state.legendMap = null;
  }

  if (cells.length === 0) return;

  // Shared color scale across every panel in the region (and in principle
  // across the whole species, since bins are computed once here from all
  // currently-loaded cells) -- a cell's color means the same thing no matter
  // which panel it's drawn in.
  const edges = computeLogBins(
    cells.map((c) => c.occurrences),
    DENSITY_COLORS.length
  );

  // Anchored to whichever panel actually has the most cells, not just the
  // first one -- e.g. Pyrrhula murina only occurs on São Miguel, so the
  // Azores' Western/Central panels render empty and the legend belongs next
  // to the Eastern panel where the data actually is.
  let legendHost = null;
  let legendHostCount = -1;
  for (const panel of region.panels) {
    const panelMap = state.panelMaps[panel.id];
    if (!panelMap) continue;
    const cellsInPanel = cells.filter((c) => panel.bounds.contains([c.centroid_lat, c.centroid_lon]));
    state.circleLayers[panel.id] = renderCellCircles(panelMap, cellsInPanel, edges, popupHtml);
    if (cellsInPanel.length > legendHostCount) {
      legendHost = panelMap;
      legendHostCount = cellsInPanel.length;
    }
  }

  if (legendHost) {
    state.legendControl = renderLegend(edges, legendHost);
    state.legendMap = legendHost;
  }
}

async function loadCells(month) {
  const statusEl = document.getElementById("map-status");
  statusEl.textContent = t("map.loading", state.lang);

  const url = month
    ? `/api/species/${state.id}/cells?month=${month}`
    : `/api/species/${state.id}/cells`;

  let cells;
  try {
    const resp = await fetch(url);
    cells = await resp.json();
  } catch (err) {
    statusEl.textContent = t("map.error_load", state.lang);
    return;
  }
  state.cells = cells;

  statusEl.textContent =
    cells.length === 0
      ? t("map.no_records", state.lang)
      : tPlural("map.record_count", cells.length, state.lang, { count: cells.length.toLocaleString() });

  // Region is auto-picked once, on first load (see determineDefaultRegion) --
  // a species confined to one archipelago opens directly on it instead of an
  // empty mainland view. Every later call (region-selector clicks, month
  // filter changes) keeps whatever region is already showing; showRegion
  // rebuilds the panel maps only when the region itself actually changes,
  // otherwise cells are just re-rendered into the existing panels.
  if (state.currentRegionId === null) {
    showRegion(determineDefaultRegion(cells));
  } else {
    renderCellsIntoCurrentPanels();
  }
}

init();
