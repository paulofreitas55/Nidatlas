const MONTH_NAMES = {
  en: ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
  pt: ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"],
  es: ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"],
};

// Warm autumn sequential scale for map data (sand -> ochre -> burnt orange ->
// deep brown). The site's own dark green (--green in style.css) stays the UI
// color everywhere else -- this ramp is only for density fill.
const DENSITY_COLORS = ["#f6ecd9", "#eed6a8", "#e0b370", "#cf8b42", "#a85c2a", "#6b3a1f"];

// Mainland Iberia + Azores + Madeira + Canaries. Verified against the actual
// grid_cells extent in the database (lat 24.8-46.7, lon -35.6-6.3), not
// guessed -- a tighter guess previously left real Azores cells outside these
// bounds, which is part of why maxBounds wasn't containing the map properly.
const STUDY_BOUNDS = L.latLngBounds([24, -36], [47, 7]);

// Land fill/outline for the vector basemap. Land = the site's own light
// paper tone (matches --paper in style.css); outline = a darker shade of the
// sea color itself (classic coastline technique), not a third unrelated hue.
// The map container's blue-grey sea background lives in species.css
// (#map.leaflet-container), since that's a plain container fill, not a layer
// Leaflet needs a JS color string for.
const LAND_FILL = "#f6f2e7";
const LAND_OUTLINE = "#94a3aa";

// On load the map frames mainland Iberia specifically, not the whole study
// area -- showing the Azores/Canaries by default made the actual mainland
// data tiny and buried the framing in irrelevant open ocean. Archipelagos
// are reached via the region selector below, which reuses these same bounds
// for "Mainland" so there's one definition, not two slightly different ones.
const MAINLAND_BOUNDS = L.latLngBounds([35.5, -10], [44, 4]);

// Jump-to-region shortcuts for the archipelagos, which are too far from the
// mainland to frame together without shrinking the mainland to a speck.
// Bounds computed directly from static/iberia.geojson's real geometry, each
// padded a little rather than guessed.
const REGIONS = {
  mainland: { label: "Mainland", bounds: MAINLAND_BOUNDS },
  azores: { label: "Azores", bounds: L.latLngBounds([36.5, -31.8], [40.0, -24.5]) },
  madeira: { label: "Madeira", bounds: L.latLngBounds([32.35, -17.55], [33.15, -16.15]) },
  canaries: { label: "Canaries", bounds: L.latLngBounds([27.25, -18.65], [29.65, -12.95]) },
};

const state = {
  id: null,
  profile: null,
  taxaLabels: { orders: {}, families: {} },
  lang: "en",
  selectedMonth: null, // null = All
  map: null,
  markersLayer: null,
  legendControl: null,
};

async function init() {
  const params = new URLSearchParams(location.search);
  state.id = params.get("id");
  const statusEl = document.getElementById("load-status");

  if (!state.id) {
    statusEl.textContent = "No species specified.";
    return;
  }

  let profileResp;
  let iberiaGeoJson;
  try {
    [profileResp, state.taxaLabels, iberiaGeoJson] = await Promise.all([
      fetch(`/api/species/${state.id}`).then((r) => (r.ok ? r.json() : Promise.reject(r.status))),
      fetch("/taxa_labels.json").then((r) => r.json()),
      fetch("/iberia.geojson").then((r) => r.json()),
    ]);
  } catch (err) {
    statusEl.textContent = err === 404 ? "Species not found." : "Could not load species.";
    return;
  }
  state.profile = profileResp;

  statusEl.hidden = true;
  for (const id of ["identity", "key-figures", "seasonality", "distribution"]) {
    document.getElementById(id).hidden = false;
  }

  state.lang = initLangSwitch((lang) => {
    state.lang = lang;
    renderIdentity();
    renderSeasonality();
    initMonthFilter();
  });

  renderIdentity();
  renderKeyFigures();
  renderSeasonality();
  initMap(iberiaGeoJson);
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
  document.getElementById("global-rank").textContent = `${p.global_rank.rank} of ${p.global_rank.total}`;
  document.getElementById("commonness").textContent =
    `More common than ${p.global_rank.percentile}% of atlas species`;
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
  svg.setAttribute("aria-label", "Monthly seasonality chart");

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

  const options = [{ label: "All", month: null }].concat(
    MONTH_NAMES[state.lang].map((label, i) => ({ label, month: i + 1 }))
  );

  for (const { label, month } of options) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "month-btn" + (month === state.selectedMonth ? " active" : "");
    btn.textContent = label;
    btn.addEventListener("click", () => {
      state.selectedMonth = month;
      initMonthFilter();
      loadCells(month);
    });
    container.appendChild(btn);
  }
}

// --- Map ---

function initMap(iberiaGeoJson) {
  state.map = L.map("map", {
    maxBounds: STUDY_BOUNDS,
    // No maxBoundsViscosity: at 1.0 (a hard wall) dragging felt sticky even
    // well before actually reaching the study-area edge. Leaving it unset
    // uses Leaflet's default elastic bounce-back instead -- panning still
    // can't escape maxBounds, it just doesn't fight the drag on the way
    // there.
    maxZoom: 18,
    // No tile layer to derive a ceiling from anymore -- 18 is picked directly
    // so a single 10km cell is still meaningfully zoomable (street-level).
  });

  // Vector basemap: just the study area's countries, no surrounding tiles.
  // interactive:false so clicks pass through to the density cells drawn on
  // top of it, since the land fill otherwise covers the same points.
  L.geoJSON(iberiaGeoJson, {
    style: { fillColor: LAND_FILL, fillOpacity: 1, color: LAND_OUTLINE, weight: 1 },
    interactive: false,
  }).addTo(state.map);

  state.markersLayer = L.layerGroup().addTo(state.map);

  // Outer zoom-out limit stays the whole study area (scroll/pinch can still
  // reach the archipelagos organically, not just via the region selector).
  // getBoundsZoom(bounds, inside=true): see the region selector below for
  // why "inside" containment, not the default "fit the whole thing" zoom.
  state.map.setMinZoom(state.map.getBoundsZoom(STUDY_BOUNDS, true));

  // The actual on-load framing (distinct from the zoom-out floor above) is
  // mainland Iberia specifically -- see MAINLAND_BOUNDS. A plain fitBounds
  // (not the "inside" trick) is correct here: mainland's aspect ratio is
  // close enough to a phone viewport that it doesn't force wild overshoot,
  // and the brief explicitly asked for a normal fit-with-padding, verified
  // visually rather than assumed.
  state.map.fitBounds(MAINLAND_BOUNDS, { padding: [16, 16] });
}

// Simple view selector rather than true printed-atlas inset panels (offered
// as the fallback in the brief): jumps the same map to a region's bounds.
// maxBounds/minZoom stay fixed at the whole study area, so this is a
// shortcut, not a mode switch -- the user can still zoom back out to the
// overview afterward.
function initRegionSelector() {
  const container = document.getElementById("region-selector");
  container.innerHTML = "";

  for (const region of Object.values(REGIONS)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "region-btn";
    btn.textContent = region.label;
    btn.addEventListener("click", () => {
      const zoom = state.map.getBoundsZoom(region.bounds, true);
      state.map.setView(region.bounds.getCenter(), zoom);
    });
    container.appendChild(btn);
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

function renderLegend(edges) {
  if (state.legendControl) {
    state.map.removeControl(state.legendControl);
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
  legend.addTo(state.map);
  state.legendControl = legend;
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

function gbifSearchUrl(speciesName, centroidLat, centroidLon) {
  const params = new URLSearchParams({
    q: speciesName,
    geometry: approxCellBoundsWKT(centroidLat, centroidLon),
    eventDate: `${CUBE_MIN_EVENT_DATE},${CUBE_DOWNLOAD_DATE}`,
  });
  return `https://www.gbif.org/occurrence/search?${params.toString()}`;
}

async function loadCells(month) {
  const statusEl = document.getElementById("map-status");
  statusEl.textContent = "Loading…";

  const url = month
    ? `/api/species/${state.id}/cells?month=${month}`
    : `/api/species/${state.id}/cells`;

  let cells;
  try {
    const resp = await fetch(url);
    cells = await resp.json();
  } catch (err) {
    statusEl.textContent = "Could not load distribution data.";
    return;
  }

  state.markersLayer.clearLayers();

  if (cells.length === 0) {
    statusEl.textContent = "No records for this period.";
    if (state.legendControl) {
      state.map.removeControl(state.legendControl);
      state.legendControl = null;
    }
    return;
  }
  statusEl.textContent = `${cells.length.toLocaleString()} cell${cells.length === 1 ? "" : "s"}`;

  const edges = computeLogBins(
    cells.map((c) => c.occurrences),
    DENSITY_COLORS.length
  );

  for (const cell of cells) {
    const color = DENSITY_COLORS[binIndexForValue(cell.occurrences, edges)];
    const circle = L.circle([cell.centroid_lat, cell.centroid_lon], {
      radius: CELL_CIRCLE_RADIUS_M,
      stroke: false,
      fillColor: color,
      fillOpacity: 0.55,
    });
    const gbifUrl = gbifSearchUrl(state.profile.gbif_name, cell.centroid_lat, cell.centroid_lon);
    circle.bindPopup(
      `<div class="map-popup"><strong>${cell.mgrs_cell}</strong><br>` +
        `${cell.occurrences.toLocaleString()} records<br>` +
        `<a href="${gbifUrl}">View on GBIF &rarr;</a></div>`
    );
    circle.addTo(state.markersLayer);
  }

  renderLegend(edges);
  // No fitBounds here: the map's on-load framing (mainland Iberia, see
  // initMap) stays fixed across month-filter changes and species loads --
  // it shouldn't jump around based on where a given species' cells happen
  // to be. Reaching an island endemic's actual cells is what the region
  // selector is for.
}

init();
