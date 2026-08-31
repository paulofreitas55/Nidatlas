// Region map (map.html): the 97 administrative regions from GET /api/regions,
// drawn as clickable polygons over the same panel-group framing species.js
// uses, shaded by total occurrences, plus an "Alto-mar"/"Open sea" entry for
// the cells that belong to no administrative region (see
// scripts/assign_regions.py). Shares panel/color/legend machinery with
// species.js via static/common.js.

// Mirrors --accent in style.css -- Leaflet needs a literal color string for
// its style objects, the same reason common.js's LAND_FILL/LAND_OUTLINE/
// BORDER_COLOR are literals rather than CSS custom properties.
const ACCENT_COLOR = "#b9532f";

// The offshore/"Alto-mar" overview panel has no natural bounds of its own
// (its cells are scattered along every coastline and out into open ocean),
// so it uses the union of every archipelago-framing panel's bounds instead --
// a single wide view of the whole study area.
function unionAllPanelBounds() {
  let bounds = null;
  for (const group of PANEL_GROUPS) {
    for (const panel of group.panels) {
      bounds = bounds ? bounds.extend(panel.bounds) : L.latLngBounds(panel.bounds.getSouthWest(), panel.bounds.getNorthEast());
    }
  }
  return bounds;
}

// PANEL_GROUPS (common.js) plus one more entry for the offshore fallback --
// not added to PANEL_GROUPS itself, since that array is also used as-is by
// species.js (via its REGIONS alias) where an "Alto-mar" selector button
// would be meaningless (a species' cells are either in bounds of a real
// panel or excluded, never bucketed into a separate administrative
// fallback).
const MAP_GROUPS = PANEL_GROUPS.concat([
  {
    id: "offshore",
    labelKey: "region.offshore",
    panels: [{ id: "offshore", labelKey: "region.offshore", bounds: unionAllPanelBounds(), flex: 1 }],
  },
]);

function findMapGroup(groupId) {
  return MAP_GROUPS.find((g) => g.id === groupId) || MAP_GROUPS[0];
}

// Which panel(s) a region's geometry is drawn into is a pure geometric
// bounds-contains check against its representative point (the feature's own
// bounding-box center) -- exactly how species.js buckets individual cell
// points into panels, just applied to a polygon's center instead of a
// single lat/lon. Deliberately NOT restricted to one "home" panel group per
// region: MAINLAND_BOUNDS is intentionally widened to also cover the
// Balearics (see common.js's comment on it) so a mainland-tab viewer still
// sees them, which means a Balearic island's center legitimately satisfies
// both the mainland panel's bounds and the Balearics panel's own -- it is
// correctly drawn (and independently clickable) in both views, the same way
// a species' Balearic-cell circles already show up on both today.
function regionCenter(feature) {
  return L.geoJSON(feature).getBounds().getCenter();
}

const state = {
  lang: "en",
  iberiaGeoJson: null,
  borderGeoJson: null,
  regionFeatures: [], // static/regions.geojson features, augmented with DB id/total_occurrences + a precomputed center
  choroplethEdges: [],
  offshoreId: null,
  offshoreCells: [],
  panelMaps: {}, // panel id -> Leaflet map instance
  circleLayers: {}, // panel id -> current L.layerGroup (offshore panel only)
  legendControl: null,
  legendMap: null,
  currentGroupId: null,
  selectedRegionId: null,
  selectedLayer: null, // the clicked L.Path, so its highlight can be undone without a full re-render
  selectedFeature: null,
  selectedMonth: null, // null = All
  lastSummary: null, // last GET /api/regions/{id} response, kept so a lang switch can re-render without refetching
};

async function init() {
  const statusEl = document.getElementById("load-status");

  try {
    await loadTranslations();
  } catch (err) {
    // Can't localize this one: i18n.json itself is what failed to fetch.
    statusEl.textContent = "Could not load the region map.";
    return;
  }

  state.lang = initLangSwitch((lang) => {
    state.lang = lang;
    document.documentElement.lang = lang;
    document.title = t("page.index_title", lang);
    applyStaticTranslations(lang);
    renderFooter(lang);
    relabelUI();
    if (state.lastSummary) renderRegionPanel(state.lastSummary);
  });
  document.documentElement.lang = state.lang;
  document.title = t("page.index_title", state.lang);
  applyStaticTranslations(state.lang);
  renderFooter(state.lang);

  let regionsList;
  let regionsGeoJson;
  try {
    [regionsList, regionsGeoJson, state.iberiaGeoJson, state.borderGeoJson] = await Promise.all([
      fetch("/api/regions").then((r) => r.json()),
      fetch("/regions.geojson").then((r) => r.json()),
      fetch("/iberia.geojson").then((r) => r.json()),
      fetch("/pt_es_border.geojson").then((r) => r.json()),
    ]);
  } catch (err) {
    statusEl.textContent = t("regionmap.error_load", state.lang);
    return;
  }

  // regions.geojson's only shared identifier with the DB is region_key (see
  // src/queries.py's list_regions) -- this is where geometry and the DB's
  // numeric id/total_occurrences get joined.
  const metaByKey = {};
  for (const r of regionsList) metaByKey[r.region_key] = r;

  const offshoreMeta = regionsList.find((r) => r.kind === "fallback");
  state.offshoreId = offshoreMeta ? offshoreMeta.id : null;

  state.regionFeatures = regionsGeoJson.features.map((f) => {
    const meta = metaByKey[f.properties.region_key] || {};
    return {
      ...f,
      properties: { ...f.properties, id: meta.id, total_occurrences: meta.total_occurrences || 0 },
      _center: regionCenter(f),
    };
  });

  // One color scale for every region on the map, computed once here --
  // switching panel groups never changes what a given color means.
  state.choroplethEdges = computeLogBins(
    state.regionFeatures.map((f) => f.properties.total_occurrences),
    DENSITY_COLORS.length
  );

  if (state.offshoreId != null) {
    try {
      state.offshoreCells = await fetch(`/api/regions/${state.offshoreId}/cells`).then((r) => r.json());
    } catch (err) {
      state.offshoreCells = []; // non-fatal: the offshore tab just renders empty
    }
  }

  statusEl.hidden = true;
  document.getElementById("map-content").hidden = false;

  initRegionSelector();
  initMonthFilter();
  document.getElementById("region-panel-close").addEventListener("click", closeRegionPanel);

  showGroup("mainland");
}

// --- Panel-group selector ---

function initRegionSelector() {
  const container = document.getElementById("region-selector");
  container.innerHTML = "";
  for (const group of MAP_GROUPS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "region-btn";
    btn.dataset.group = group.id;
    btn.textContent = t(group.labelKey, state.lang);
    btn.addEventListener("click", () => showGroup(group.id));
    container.appendChild(btn);
  }
}

// Re-labels the selector/month-filter buttons and whatever panel labels are
// on screen, without touching the Leaflet map instances -- same rationale as
// species.js's relabelMapUI (called on a language switch only).
function relabelUI() {
  document.querySelectorAll(".region-btn").forEach((btn) => {
    const group = findMapGroup(btn.dataset.group);
    btn.textContent = t(group.labelKey, state.lang);
  });
  initMonthFilter(); // rebuilds with new labels, preserving state.selectedMonth's active state
  if (state.currentGroupId) {
    const group = findMapGroup(state.currentGroupId);
    const labelEls = document.querySelectorAll(".map-panel-label");
    group.panels.forEach((panel, i) => {
      if (labelEls[i]) labelEls[i].textContent = t(panel.labelKey, state.lang);
    });

    const statusEl = document.getElementById("map-status");
    if (group.id === "offshore") {
      statusEl.textContent = tPlural("map.record_count", state.offshoreCells.length, state.lang, {
        count: state.offshoreCells.length.toLocaleString(),
      });
    } else {
      const totalFeatures = group.panels.reduce(
        (sum, panel) => sum + state.regionFeatures.filter((f) => panel.bounds.contains(f._center)).length,
        0
      );
      statusEl.textContent = tPlural("regionmap.region_count", totalFeatures, state.lang, { count: totalFeatures });
    }
  }
}

// --- Month filter (applies to the side panel's species lists, not to how
// the choropleth itself is shaded -- GET /api/regions has no month
// breakdown, only GET /api/regions/{id} does) ---

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
      if (month === state.selectedMonth) return;
      state.selectedMonth = month;
      initMonthFilter();
      if (state.selectedRegionId != null) loadRegionPanel(state.selectedRegionId);
    });
    container.appendChild(btn);
  }
}

// --- Panel maps ---

function destroyPanelMaps() {
  for (const map of Object.values(state.panelMaps)) {
    map.remove(); // also tears down any layers/controls attached to it
  }
  state.panelMaps = {};
  state.circleLayers = {};
  state.legendControl = null;
  state.legendMap = null;
}

// Full rebuild: tears down and recreates the panel DOM + map instances for a
// panel group. Unlike species.js's showRegion, there's no "keep the maps,
// just re-render the data" path here -- switching groups always means an
// entirely different set of regions or, for the offshore tab, cells instead
// of polygons, so there is no shared state worth preserving across a switch.
function showGroup(groupId) {
  state.currentGroupId = groupId;
  closeRegionPanel();

  const group = findMapGroup(groupId);
  destroyPanelMaps();
  const container = document.getElementById("map-panels");
  container.innerHTML = "";

  // See buildPanelDom's own comment (static/common.js) for why panel DOM is
  // built in a separate pass before any L.map is constructed.
  const mapEls = buildPanelDom(container, group.panels, state.lang);

  // Every panel group locks its floor at the fitted zoom -- see
  // createPanelMap (static/common.js).
  group.panels.forEach((panel, i) => {
    state.panelMaps[panel.id] = createPanelMap(
      mapEls[i], panel.bounds, state.iberiaGeoJson, state.borderGeoJson
    );
  });

  document.querySelectorAll(".region-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.group === groupId);
  });

  if (groupId === "offshore") {
    renderOffshoreCells(group);
  } else {
    renderRegionPolygons(group);
  }
}

// --- Region polygons (choropleth) ---

function regionStyle(props, selected) {
  const fillColor = DENSITY_COLORS[binIndexForValue(props.total_occurrences, state.choroplethEdges)];
  return {
    fillColor,
    fillOpacity: 0.75,
    color: selected ? ACCENT_COLOR : LAND_OUTLINE,
    weight: selected ? 3 : 1,
  };
}

function selectRegionFromFeature(feature, layer) {
  if (state.selectedLayer && state.selectedFeature) {
    state.selectedLayer.setStyle(regionStyle(state.selectedFeature.properties, false));
  }
  state.selectedLayer = layer;
  state.selectedFeature = feature;
  layer.setStyle(regionStyle(feature.properties, true));
  loadRegionPanel(feature.properties.id);
}

function renderRegionPolygons(group) {
  // Anchored to whichever panel actually has the most regions, not just the
  // first one -- same convention as species.js's cell-density legend (e.g.
  // the Azores' Western panel would otherwise host an empty-looking legend
  // if most of that group's regions are in the Central/Eastern panels).
  let legendHost = null;
  let legendHostCount = -1;

  let totalFeatures = 0;
  for (const panel of group.panels) {
    const panelMap = state.panelMaps[panel.id];
    const featuresForPanel = state.regionFeatures.filter((f) => panel.bounds.contains(f._center));
    totalFeatures += featuresForPanel.length;

    L.geoJSON(featuresForPanel, {
      style: (feature) => regionStyle(feature.properties, false),
      onEachFeature: (feature, layer) => {
        layer.on("click", () => selectRegionFromFeature(feature, layer));
        layer.on("mouseover", () => {
          if (layer === state.selectedLayer) return;
          layer.setStyle({ weight: 2, color: BORDER_COLOR });
        });
        layer.on("mouseout", () => {
          if (layer === state.selectedLayer) return;
          layer.setStyle(regionStyle(feature.properties, false));
        });
      },
    }).addTo(panelMap);

    if (featuresForPanel.length > legendHostCount) {
      legendHost = panelMap;
      legendHostCount = featuresForPanel.length;
    }
  }

  if (legendHost) {
    state.legendControl = renderLegend(state.choroplethEdges, legendHost);
    state.legendMap = legendHost;
  }

  document.getElementById("map-status").textContent = tPlural(
    "regionmap.region_count", totalFeatures, state.lang, { count: totalFeatures }
  );
}

// --- Offshore/"Alto-mar" cells (no polygon of their own -- see
// scripts/assign_regions.py's RESCUE_THRESHOLD_KM comment) ---

function offshoreCellPopupHtml(cell) {
  // No species filter (q) -- unlike the species map's popup, a cell here
  // represents every species combined, not one.
  const gbifUrl = gbifSearchUrl(cell.centroid_lat, cell.centroid_lon, {});
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

function renderOffshoreCells(group) {
  const panel = group.panels[0];
  const panelMap = state.panelMaps[panel.id];
  const edges = computeLogBins(
    state.offshoreCells.map((c) => c.occurrences),
    DENSITY_COLORS.length
  );
  state.circleLayers[panel.id] = renderCellCircles(panelMap, state.offshoreCells, edges, offshoreCellPopupHtml);
  state.legendControl = renderLegend(edges, panelMap);
  state.legendMap = panelMap;

  document.getElementById("map-status").textContent = tPlural(
    "map.record_count", state.offshoreCells.length, state.lang, { count: state.offshoreCells.length.toLocaleString() }
  );

  // Exactly one DB region sits behind every offshore cell (there is no
  // per-region ambiguity to click through, unlike the polygon groups), so
  // opening this tab can select it immediately.
  if (state.offshoreId != null) loadRegionPanel(state.offshoreId);
}

// --- Region side panel ---

async function loadRegionPanel(regionId) {
  state.selectedRegionId = regionId;
  document.getElementById("region-panel-empty").hidden = true;
  document.getElementById("region-panel-content").hidden = false;

  const statusEl = document.getElementById("region-panel-status");
  const listsEl = document.getElementById("region-panel-lists");
  listsEl.hidden = true;
  statusEl.textContent = t("map.loading", state.lang);

  const url = state.selectedMonth
    ? `/api/regions/${regionId}?month=${state.selectedMonth}`
    : `/api/regions/${regionId}`;

  let summary;
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(String(resp.status));
    summary = await resp.json();
  } catch (err) {
    statusEl.textContent = t("regionmap.error_panel", state.lang);
    return;
  }
  state.lastSummary = summary;
  renderRegionPanel(summary);
}

function renderRegionPanel(summary) {
  document.getElementById("region-panel-name").textContent = pickLabel(
    { pt: summary.name_pt, es: summary.name_es, en: summary.name_en },
    state.lang
  );
  document.getElementById("region-panel-total").textContent = summary.total_occurrences.toLocaleString();
  document.getElementById("region-panel-species-count").textContent = summary.distinct_species.toLocaleString();

  const statusEl = document.getElementById("region-panel-status");
  const listsEl = document.getElementById("region-panel-lists");
  if (summary.distinct_species === 0) {
    statusEl.textContent = t("map.no_records", state.lang);
    listsEl.hidden = true;
    return;
  }
  statusEl.textContent = "";
  listsEl.hidden = false;
  renderSpeciesList(document.getElementById("region-panel-top"), summary.top_species);
  renderSpeciesList(document.getElementById("region-panel-bottom"), summary.bottom_species);
}

// Plain-language concentration tiers -- proposed boundaries/wording, see
// the task this was introduced for; easy to retune since it's just this
// one table. Checked in order, first match wins (>= min, descending), so
// order matters: most distinctive first, the open-ended "below average"
// catch-all last.
//
// The 1x-2x "slight" tier isn't from the original brief (which specified
// >20x/5-20x/2-5x/below 1x) -- added to cover that gap so every
// concentration value maps to some tier rather than falling through.
const CONCENTRATION_TIERS = [
  { min: 20, key: "regionmap.tier_very_high" },
  { min: 5, key: "regionmap.tier_high" },
  { min: 2, key: "regionmap.tier_moderate" },
  { min: 1, key: "regionmap.tier_slight" },
  { min: -Infinity, key: "regionmap.tier_below" },
];

function concentrationTierKey(concentration) {
  return CONCENTRATION_TIERS.find((tier) => concentration >= tier.min).key;
}

// Below-10 gets one decimal place (matches the earlier "12x" precedent);
// 10 and up rounds to a whole number, since a fractional multiple stops
// being meaningful at that scale and just adds visual noise. concentration
// can legitimately run into the thousands for a single-occurrence vagrant
// far outside its normal range (see queries.py's _rank_by_concentration) --
// toLocaleString adds thousands separators so those stay readable rather
// than a long unbroken digit string.
function formatRatio(x) {
  if (!Number.isFinite(x)) return "∞";
  return x >= 10 ? Math.round(x).toLocaleString() : x.toFixed(1);
}

// The small, muted number shown next to the plain-language tier phrase --
// secondary to it, not the headline. >= 1 shows directly ("12x"); below 1
// the raw fraction (e.g. 0.0007x) is unreadable, so it's shown as "1:N"
// instead -- same ratio, just the legible way to write a small fraction.
function ratioText(concentration) {
  if (!Number.isFinite(concentration)) return "∞";
  if (concentration >= 1) return formatRatio(concentration) + "x";
  const inverse = concentration > 0 ? 1 / concentration : Infinity;
  return "1:" + formatRatio(inverse);
}

function renderSpeciesList(ulEl, rows) {
  ulEl.innerHTML = "";
  for (const row of rows) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.className = "region-species-row";
    a.href = `species.html?id=${row.species_id}`;

    const name = document.createElement("span");
    name.className = "region-species-name";
    name.textContent = row.gbif_name;
    a.appendChild(name);

    const tier = document.createElement("span");
    tier.className = "region-species-tier";
    const phrase = document.createElement("span");
    phrase.className = "region-species-tier-phrase";
    phrase.textContent = t(concentrationTierKey(row.concentration), state.lang);
    tier.appendChild(phrase);
    const ratio = document.createElement("span");
    ratio.className = "region-species-tier-ratio";
    ratio.textContent = ratioText(row.concentration);
    tier.appendChild(ratio);
    a.appendChild(tier);

    const meta = document.createElement("span");
    meta.className = "region-species-meta";
    meta.textContent = t("regionmap.species_row_meta", state.lang, {
      family: row.family,
      occurrences: row.occurrences.toLocaleString(),
    });
    a.appendChild(meta);

    li.appendChild(a);
    ulEl.appendChild(li);
  }
}

function closeRegionPanel() {
  state.selectedRegionId = null;
  state.lastSummary = null;
  if (state.selectedLayer && state.selectedFeature) {
    state.selectedLayer.setStyle(regionStyle(state.selectedFeature.properties, false));
  }
  state.selectedLayer = null;
  state.selectedFeature = null;
  document.getElementById("region-panel-content").hidden = true;
  document.getElementById("region-panel-empty").hidden = false;
}

init();
