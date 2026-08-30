const MONTH_NAMES = {
  en: ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
  pt: ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"],
  es: ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"],
};

// Warm autumn sequential scale for map data (sand -> ochre -> burnt orange ->
// deep brown). The site's own dark green (--green in style.css) stays the UI
// color everywhere else -- this ramp is only for density fill.
const DENSITY_COLORS = ["#f6ecd9", "#eed6a8", "#e0b370", "#cf8b42", "#a85c2a", "#6b3a1f"];

// Land fill/outline for the vector basemap. Land = the site's own light
// paper tone (matches --paper in style.css); outline = a darker shade of the
// sea color itself (classic coastline technique), not a third unrelated hue.
// Each panel's own blue-grey sea background lives in species.css
// (.map-panel.leaflet-container), since that's a plain container fill, not a
// layer Leaflet needs a JS color string for.
const LAND_FILL = "#f6f2e7";
const LAND_OUTLINE = "#94a3aa";

// Subtle Portugal/Spain border stroke -- muted ink tone from the site
// palette (style.css --muted), not the sea-outline color (would read as a
// second coastline) or --accent (reserved for interactive highlights).
const BORDER_COLOR = "#6b6357";

// Mainland Iberia is its own "region" with a single panel, so it reuses the
// exact same rendering path as the multi-panel archipelagos below rather
// than being a special case. East edge is 4.5, not a tighter 4, specifically
// so the Balearics (Menorca reaches lon 4.33) stay fully visible here too,
// not just in their own archipelago panel below.
const MAINLAND_BOUNDS = L.latLngBounds([35.5, -10], [44, 4.5]);

// Every region the map can show, as a generic list of named sub-region
// panels with bounds -- not hardcoded per archipelago. Panel bounds are
// computed directly from static/iberia.geojson's real polygon clusters
// (grouped by proximity, then padded), not guessed: island groups are
// genuinely too far apart to share one sane frame (e.g. the Azores span
// ~7 degrees of longitude across three clusters), which is also why a
// species confined to one archipelago needs its own multi-panel view rather
// than a single fitBounds. flex controls each panel's relative width in the
// row layout (see species.css); unlisted defaults to 1 (equal).
// label -> labelKey: every region/panel name shown in the UI (region-selector
// buttons, panel headings) now resolves through static/i18n.json at render
// time (see relabelMapUI) instead of storing literal English text -- island
// names themselves (Flores, Corvo, Tenerife, ...) stay untranslated in every
// language, since they're proper place names with no distinct pt/es/en
// exonyms here, not UI prose.
const REGIONS = [
  {
    id: "mainland",
    labelKey: "region.mainland",
    panels: [{ id: "mainland", labelKey: "region.mainland", bounds: MAINLAND_BOUNDS, flex: 1 }],
  },
  {
    id: "azores",
    labelKey: "region.azores",
    panels: [
      {
        id: "azores-west",
        labelKey: "region.azores_west_panel",
        bounds: L.latLngBounds([39.29, -31.40], [39.81, -30.96]),
        flex: 1,
      },
      {
        id: "azores-central",
        labelKey: "region.azores_central_panel",
        bounds: L.latLngBounds([38.23, -28.99], [39.25, -26.89]),
        flex: 1,
      },
      {
        id: "azores-east",
        labelKey: "region.azores_east_panel",
        bounds: L.latLngBounds([36.78, -26.01], [38.06, -24.63]),
        flex: 1,
      },
    ],
  },
  {
    id: "madeira",
    labelKey: "region.madeira",
    panels: [
      {
        id: "madeira-main",
        labelKey: "region.madeira",
        bounds: L.latLngBounds([32.28, -17.39], [33.25, -16.16]),
        flex: 4,
      },
      // Selvagens sits ~280km south of Madeira -- folding it into the main
      // panel's bounds would triple that panel's latitude span and shrink
      // Madeira/Porto Santo/Desertas to a sliver, so it gets its own small
      // secondary panel instead (measured, not guessed: see the bounds gap).
      {
        id: "madeira-selvagens",
        labelKey: "region.madeira_selvagens_panel",
        bounds: L.latLngBounds([29.93, -16.15], [30.26, -15.75]),
        flex: 1,
      },
    ],
  },
  {
    id: "canaries",
    labelKey: "region.canaries",
    // All seven islands in one panel -- unlike the Azores (~7 degrees of
    // longitude across three separated clusters) the Canaries span a more
    // modest ~4.7 degrees end to end and are close enough to share one
    // frame without any island shrinking to an unreadable speck.
    panels: [
      {
        id: "canaries",
        labelKey: "region.canaries_panel",
        bounds: L.latLngBounds([27.54, -18.31], [29.39, -13.27]),
        flex: 1,
      },
    ],
  },
  {
    id: "balearics",
    labelKey: "region.balearics",
    panels: [
      {
        id: "balearics",
        labelKey: "region.balearics_panel",
        bounds: L.latLngBounds([38.54, 1.06], [40.17, 4.48]),
        flex: 1,
      },
    ],
  },
];

function findRegion(regionId) {
  return REGIONS.find((r) => r.id === regionId) || REGIONS[0];
}

// Small padding around a region's own fitted bounds, used as maxBounds so
// panning can wander a little beyond the tight fit without losing the
// region entirely (see createPanelMap).
function padBounds(bounds, factor) {
  const sw = bounds.getSouthWest();
  const ne = bounds.getNorthEast();
  const latPad = (ne.lat - sw.lat) * factor;
  const lonPad = (ne.lng - sw.lng) * factor;
  return L.latLngBounds([sw.lat - latPad, sw.lng - lonPad], [ne.lat + latPad, ne.lng + lonPad]);
}

// Which region a lat/lon falls in, checked against every panel of every
// region -- used only to auto-pick the initial region for a species (see
// determineDefaultRegion), never to fitBounds a rectangle spanning regions.
// Mainland is checked last, not in REGIONS' declared order: its bounds were
// widened to also cover the Balearics (so they stay visible on the mainland
// view too), which means mainland's box now geographically overlaps the
// Balearics panel's box. Checking the more specific archipelago bounds
// first keeps a Balearics-only species resolving to "balearics", not
// silently absorbed into "mainland" just because that region happens to be
// declared first.
function regionForLatLng(lat, lon) {
  for (const region of REGIONS) {
    if (region.id === "mainland") continue;
    for (const panel of region.panels) {
      if (panel.bounds.contains([lat, lon])) return region.id;
    }
  }
  if (MAINLAND_BOUNDS.contains([lat, lon])) return "mainland";
  return null;
}

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
};

function showError(key) {
  state.errorKey = key;
  document.getElementById("load-status").textContent = t(key, state.lang);
}

async function init() {
  const params = new URLSearchParams(location.search);
  state.id = params.get("id");
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
      document.title = `${state.profile.gbif_name} — Nidario`;
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
            : tPlural("map.cell_count", state.cells.length, state.lang, {
                count: state.cells.length.toLocaleString(),
              });
      }
    }
  });
  document.documentElement.lang = state.lang;
  document.title = t("page.species_fallback_title", state.lang); // real species name replaces this once loaded
  applyStaticTranslations(state.lang);
  renderFooter(state.lang);

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
  document.title = `${state.profile.gbif_name} — Nidario`;

  statusEl.hidden = true;
  for (const id of ["identity", "key-figures", "seasonality", "distribution"]) {
    document.getElementById(id).hidden = false;
  }

  renderIdentity();
  renderKeyFigures();
  renderSeasonality();
  initRegionSelector();
  initMonthFilter();
  await loadCells(state.selectedMonth);
}

// --- Identity & key figures ---

function renderIdentity() {
  const p = state.profile;

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
// Each region (mainland, or an archipelago) renders as one or more
// side-by-side panels, each its own independent Leaflet map instance tightly
// framed on its own sub-region -- island groups are too far apart to share
// one frame without either shrinking the mainland to a speck or forcing a
// rectangle that spans Morocco and half of Europe. Because each panel is a
// genuinely separate map, cross-panel panning is architecturally impossible,
// which is what makes "never fitBounds a rectangle spanning mainland and
// islands" hold by construction rather than by convention.

// preferCanvas: true renders every circle to a single <canvas> instead of
// one <path> SVG node each -- on the densest species (thousands of cells)
// the old SVG renderer had to reposition every node on every drag frame,
// which is what caused the ~2s post-drag stall. Canvas just repaints once.
//
// zoomSnap: 0 makes zoom fully continuous (no snapping to any grid of
// levels at all -- 0.25 was still discrete steps, just finer ones).
// zoomDelta 0.1 and a much higher wheelPxPerZoomLevel mean a single scroll
// notch or +/- click is a small, gradual nudge rather than a jump, which
// matters at this scale where a whole default zoom level can be the
// difference between "whole archipelago" and "one island". maxZoom raised
// again (20 -> 24) so a single 10km cell is zoomable in much further.
// zoomAnimation stays at Leaflet's default true -- continuous zoomSnap
// doesn't need it disabled, and animated is what "smooth" means here.
//
// maxBounds fences each panel to its own region (padded a little) with
// maxBoundsViscosity: 1.0 so panning hits a firm, immediate stop at the
// edge instead of the elastic bounce-back a lower viscosity gives -- the
// region should never scroll off-screen. Computed fresh from this panel's
// own `bounds` argument every time createPanelMap runs, which is every time
// showRegion rebuilds the panels for a newly selected view (it always tears
// down and recreates every L.map instance first, see destroyPanelMaps), so
// there's no stale carry-over from whatever view was showing before.
function createPanelMap(containerEl, bounds, isArchipelago) {
  const map = L.map(containerEl, {
    preferCanvas: true,
    maxZoom: 24,
    zoomSnap: 0,
    zoomDelta: 0.1,
    wheelPxPerZoomLevel: 250,
    zoomAnimation: true,
    maxBounds: padBounds(bounds, 0.15),
    maxBoundsViscosity: 1.0,
  });

  // Vector basemap: just the study area's countries, no surrounding tiles.
  // interactive:false so clicks pass through to the density cells drawn on
  // top of it, since the land fill otherwise covers the same points.
  L.geoJSON(state.iberiaGeoJson, {
    style: { fillColor: LAND_FILL, fillOpacity: 1, color: LAND_OUTLINE, weight: 1 },
    interactive: false,
  }).addTo(map);

  // Portugal/Spain border only actually falls inside the mainland panel's
  // bounds -- added to every panel anyway rather than special-cased, since
  // Leaflet skips out-of-view geometry for free and this keeps panel
  // creation generic.
  L.geoJSON(state.borderGeoJson, {
    style: { color: BORDER_COLOR, weight: 1, opacity: 0.7 },
    interactive: false,
  }).addTo(map);

  map.fitBounds(bounds, { padding: [12, 12] });

  // Archipelago panels are already the zoomed-in view of their region --
  // zooming out below that just reveals surrounding open ocean with nothing
  // in it, so the fitted zoom becomes the floor. Mainland stays free to zoom
  // out (e.g. scroll-zooming past the peninsula is a reasonable thing to do
  // on the default/overview region).
  if (isArchipelago) {
    map.setMinZoom(map.getZoom());
  }

  return map;
}

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

  // Two passes, not one: creating and sizing a Leaflet map interleaved with
  // appending its still-to-come sibling panels was the actual cause of the
  // Canaries misalignment bug. A flex child's width depends on how many
  // siblings currently exist -- so a panel built in the SAME loop iteration
  // that appends it (before its siblings are appended) gets initialized at
  // its temporary, too-wide single-child width. L.map() measures the
  // container once at construction and does not re-measure on its own, so
  // every panel but the last in a multi-panel region ended up with a pixel
  // origin computed for a container size it no longer had once the rest of
  // the row appeared -- circles rendered at the coordinates that stale size
  // implied, offset from the correctly-sized visible panel underneath them.
  // Building every container first, and only then constructing any L.map,
  // guarantees the flex row is already at its final layout before Leaflet
  // ever measures anything.
  const mapEls = region.panels.map((panel) => {
    const wrap = document.createElement("div");
    wrap.className = "map-panel-wrap";
    wrap.style.flexGrow = panel.flex || 1;

    const label = document.createElement("div");
    label.className = "map-panel-label";
    label.textContent = t(panel.labelKey, state.lang);
    wrap.appendChild(label);

    const mapEl = document.createElement("div");
    mapEl.className = "map-panel";
    wrap.appendChild(mapEl);

    container.appendChild(wrap);
    return mapEl;
  });

  const isArchipelago = region.id !== "mainland";
  region.panels.forEach((panel, i) => {
    state.panelMaps[panel.id] = createPanelMap(mapEls[i], panel.bounds, isArchipelago);
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

// --- Density color scale: log bins (occurrence counts span 1 to tens of
// thousands -- linear bins would put almost everything in the bottom bin) ---

function computeLogBins(values, numBins) {
  const positive = values.filter((v) => v > 0);
  const min = positive.length ? Math.min(...positive) : 1;
  const max = positive.length ? Math.max(...positive) : 1;
  const logMin = Math.log(min);
  const logMax = Math.log(Math.max(max, min + 1));

  const edges = [];
  for (let i = 0; i <= numBins; i++) {
    edges.push(Math.exp(logMin + ((logMax - logMin) * i) / numBins));
  }
  edges[0] = min;
  edges[numBins] = max;
  return edges;
}

function binIndexForValue(value, edges) {
  for (let i = 0; i < edges.length - 2; i++) {
    if (value <= edges[i + 1]) return i;
  }
  return edges.length - 2;
}

// All panels share one legend rather than repeating it per panel -- attached
// to the region's first panel only.
function renderLegend(edges, targetMap) {
  if (state.legendControl && state.legendMap) {
    state.legendMap.removeControl(state.legendControl);
  }
  const legend = L.control({ position: "bottomright" });
  legend.onAdd = () => {
    const div = L.DomUtil.create("div", "map-legend");
    for (let i = 0; i < DENSITY_COLORS.length; i++) {
      const row = L.DomUtil.create("div", "legend-row", div);
      const swatch = L.DomUtil.create("span", "legend-swatch", row);
      swatch.style.background = DENSITY_COLORS[i];
      const label = L.DomUtil.create("span", "legend-label", row);
      const from = Math.max(1, Math.round(edges[i]));
      const to = Math.round(edges[i + 1]);
      label.textContent = i === DENSITY_COLORS.length - 1 ? `${from}+` : `${from}–${to}`;
    }
    return div;
  };
  legend.addTo(targetMap);
  state.legendControl = legend;
  state.legendMap = targetMap;
}

// Fixed radius, not data-scaled: density is conveyed entirely by color now.
// ~6km against the 10km grid spacing means neighboring cells' circles
// overlap a little, so adjacent color bands blend into a continuous surface
// instead of a hard-edged tile grid.
const CELL_CIRCLE_RADIUS_M = 6000;

const METERS_PER_DEGREE_LAT = 111320;

// Approximate +/-5km box around the centroid, longitude-compensated for
// latitude (a naive fixed-degree box would be visibly non-square away from
// the equator). This is a convenience search-link footprint, not the exact
// MGRS cell geometry (which needs a real UTM projection to get precisely --
// see the git history for that derivation) -- close enough for "show me
// GBIF records near this cell", where the value is a working link, not
// surveying-grade precision.
function approxCellBoundsWKT(centroidLat, centroidLon) {
  const halfLat = 5000 / METERS_PER_DEGREE_LAT;
  const halfLon = 5000 / (METERS_PER_DEGREE_LAT * Math.cos((centroidLat * Math.PI) / 180));
  const south = centroidLat - halfLat;
  const north = centroidLat + halfLat;
  const west = centroidLon - halfLon;
  const east = centroidLon + halfLon;
  return (
    `POLYGON((${west} ${south}, ${east} ${south}, ${east} ${north}, ` +
    `${west} ${north}, ${west} ${south}))`
  );
}

// q (free-text) + geometry (WKT polygon) -- verified geometry against the
// real GBIF API directly (api.gbif.org/v1/occurrence/search) since the
// www.gbif.org search UI itself sits behind a bot-check that blocked
// automated verification of its exact client-side param names. q is the
// same field the portal's own search box uses, so it's a safe choice even
// without that confirmation; if geometry isn't honored by the portal for
// some reason, the link still degrades gracefully to a plain species search.
// eventDate lower bound matches MIN_YEAR in scripts/prepare_cube.py, so the
// live search doesn't surface pre-1990 records that our own dataset
// deliberately excludes -- without this, GBIF's count would diverge from
// ours for a reason unrelated to the live-vs-snapshot gap described below.
// Bounds the live GBIF search to the same window the local occurrence cube
// covers (MIN_YEAR in scripts/prepare_cube.py, and the cube's download date
// per README.md's citation). This keeps the live count from drifting far
// past what's in the popup as new sightings get uploaded to GBIF over time --
// it can't be exact (a record can be backfilled into GBIF after our download
// date while carrying an eventDate inside this window), but it removes the
// main, fast-growing source of drift: brand-new observations.
const CUBE_MIN_EVENT_DATE = "1990-01-01";
const CUBE_DOWNLOAD_DATE = "2026-08-28";

// Matches the licence filter already applied when building the local cube
// (scripts/prepare_cube.py's species-list/occurrence downloads exclude
// CC-BY-NC records) -- without this, GBIF's live count includes NC records
// our own dataset never had. Verified directly against api.gbif.org: a
// repeated license= param OR-filters (license=CC0_1_0&license=CC_BY_4_0
// plus a CC_BY_NC_4_0-only query partition the unfiltered count exactly,
// confirming both that these are GBIF's only three license values and that
// the two we want are being correctly selected.
const CUBE_LICENSES = ["CC0_1_0", "CC_BY_4_0"];

function gbifSearchUrl(speciesName, centroidLat, centroidLon) {
  const params = new URLSearchParams({
    q: speciesName,
    geometry: approxCellBoundsWKT(centroidLat, centroidLon),
    eventDate: `${CUBE_MIN_EVENT_DATE},${CUBE_DOWNLOAD_DATE}`,
  });
  for (const license of CUBE_LICENSES) {
    params.append("license", license);
  }
  return `https://www.gbif.org/occurrence/search?${params.toString()}`;
}

function popupHtml(cell) {
  const gbifUrl = gbifSearchUrl(state.profile.gbif_name, cell.centroid_lat, cell.centroid_lon);
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

// Batch-add: builds every circle first and adds them to the panel in one
// L.layerGroup rather than calling circle.addTo(map) inside the loop, so the
// densest species (thousands of cells) triggers one insertion, not hundreds.
function renderCirclesForPanel(panelMap, cellsInPanel, edges) {
  const circles = cellsInPanel.map((cell) => {
    const color = DENSITY_COLORS[binIndexForValue(cell.occurrences, edges)];
    const circle = L.circle([cell.centroid_lat, cell.centroid_lon], {
      radius: CELL_CIRCLE_RADIUS_M,
      stroke: false,
      fillColor: color,
      fillOpacity: 0.55,
    });
    circle.bindPopup(popupHtml(cell));
    return circle;
  });
  const group = L.layerGroup(circles);
  group.addTo(panelMap);
  return group;
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
    state.circleLayers[panel.id] = renderCirclesForPanel(panelMap, cellsInPanel, edges);
    if (cellsInPanel.length > legendHostCount) {
      legendHost = panelMap;
      legendHostCount = cellsInPanel.length;
    }
  }

  if (legendHost) renderLegend(edges, legendHost);
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
      : tPlural("map.cell_count", cells.length, state.lang, { count: cells.length.toLocaleString() });

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
