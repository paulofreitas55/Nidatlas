// Shared map/color/panel logic used by both species.js (per-species
// distribution map) and map.js (region choropleth map). Plain <script>
// include, no bundler -- must load before either page script, right after
// lang.js and the Leaflet library itself.

const MONTH_NAMES = {
  en: ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
  pt: ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"],
  es: ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"],
};

// Warm autumn sequential scale for map data (sand -> ochre -> burnt orange ->
// deep brown). The site's own dark green (--green in style.css) stays the UI
// color everywhere else -- this ramp is only for density/occurrence fill,
// on both the species map's cell circles and the region map's choropleth.
const DENSITY_COLORS = ["#f6ecd9", "#eed6a8", "#e0b370", "#cf8b42", "#a85c2a", "#6b3a1f"];

// Land fill/outline for the vector basemap. Land = the site's own light
// paper tone (matches --paper in style.css); outline = a darker shade of the
// sea color itself (classic coastline technique), not a third unrelated hue.
// Each panel's own blue-grey sea background lives in each page's own CSS
// (.map-panel.leaflet-container), since that's a plain container fill, not a
// layer Leaflet needs a JS color string for.
const LAND_FILL = "#f6f2e7";
const LAND_OUTLINE = "#94a3aa";

// Subtle Portugal/Spain border stroke -- muted ink tone from the site
// palette (style.css --muted), not the sea-outline color (would read as a
// second coastline) or --accent (reserved for interactive highlights).
const BORDER_COLOR = "#6b6357";

// Mainland Iberia is its own "panel group" with a single panel, so it reuses
// the exact same rendering path as the multi-panel archipelagos below rather
// than being a special case. East edge is 4.5, not a tighter 4, specifically
// so the Balearics (Menorca reaches lon 4.33) stay fully visible here too,
// not just in their own archipelago panel below.
const MAINLAND_BOUNDS = L.latLngBounds([35.5, -10], [44, 4.5]);

// Every panel group either map can show, as a generic list of named
// sub-region panels with bounds -- not hardcoded per archipelago. Panel
// bounds are computed directly from static/iberia.geojson's real polygon
// clusters (grouped by proximity, then padded), not guessed: island groups
// are genuinely too far apart to share one sane frame (e.g. the Azores span
// ~7 degrees of longitude across three clusters), which is also why a
// species (or, on the region map, an administrative region) confined to one
// archipelago needs its own multi-panel view rather than a single fitBounds.
// flex controls each panel's relative width in the row layout (each page's
// own CSS); unlisted defaults to 1 (equal).
//
// This is deliberately named PANEL_GROUPS, not REGIONS: the region map
// introduces a second, unrelated sense of "region" (the 97 administrative
// districts/provinces/islands from GET /api/regions) that these five
// archipelago-framing groups must not be confused with -- species.js keeps
// its own REGIONS alias for these, since that's what its existing code
// (predating the region map) already calls them internally.
//
// label -> labelKey: every panel group/panel name shown in the UI (region
// selector buttons, panel headings) resolves through static/i18n.json at
// render time -- island names themselves (Flores, Corvo, Tenerife, ...) stay
// untranslated in every language, since they're proper place names with no
// distinct pt/es/en exonyms here, not UI prose.
const PANEL_GROUPS = [
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
        // Base box is Madeira + Porto Santo + Desertas' own combined
        // bounding box, measured directly from static/regions.geojson (not
        // guessed): lat 32.41-33.10, lon -17.27--16.29. padBounds(...,
        // 0.25) adds 25% margin on every side so Porto Santo (the
        // northeasternmost point) and Desertas (the southeasternmost) both
        // sit comfortably inside the frame at the default zoom rather than
        // hugging the edge -- the previous hand-picked box left only
        // ~0.13deg of margin there, tight enough that Porto Santo was
        // barely visible until zoomed in.
        bounds: padBounds(L.latLngBounds([32.41, -17.27], [33.10, -16.29]), 0.25),
        flex: 4,
      },
      // Selvagens sits ~280km south of Madeira -- folding it into the main
      // panel's bounds would triple that panel's latitude span and shrink
      // Madeira/Porto Santo/Desertas to a sliver, so it gets its own small
      // secondary panel instead.
      {
        id: "madeira-selvagens",
        labelKey: "region.madeira_selvagens_panel",
        // Same approach as madeira-main: base box is Selvagens' own two
        // island groups' combined bounding box (lat 30.03-30.15, lon
        // -16.04--15.86, measured from static/regions.geojson), padded 25%
        // so both groups (~15km apart) are clearly separated and neither
        // sits flush against the frame edge.
        bounds: padBounds(L.latLngBounds([30.03, -16.04], [30.15, -15.86]), 0.25),
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

function findPanelGroup(groupId) {
  return PANEL_GROUPS.find((g) => g.id === groupId) || PANEL_GROUPS[0];
}

// Small padding around a panel group's own fitted bounds, used as maxBounds
// so panning can wander a little beyond the tight fit without losing the
// area entirely (see createPanelMap).
function padBounds(bounds, factor) {
  const sw = bounds.getSouthWest();
  const ne = bounds.getNorthEast();
  const latPad = (ne.lat - sw.lat) * factor;
  const lonPad = (ne.lng - sw.lng) * factor;
  return L.latLngBounds([sw.lat - latPad, sw.lng - lonPad], [ne.lat + latPad, ne.lng + lonPad]);
}

// Which panel group a lat/lon falls in, checked against every panel of every
// group -- used to auto-pick a default view from a set of points (species.js)
// and to bucket administrative-region polygons into their panel group
// (map.js), never to fitBounds a rectangle spanning groups. Mainland is
// checked last, not in PANEL_GROUPS' declared order: its bounds were widened
// to also cover the Balearics (so they stay visible on the mainland view
// too), which means mainland's box now geographically overlaps the
// Balearics panel's box. Checking the more specific archipelago bounds
// first keeps a Balearics-only point resolving to "balearics", not silently
// absorbed into "mainland" just because that group happens to be declared
// first.
function panelGroupForLatLng(lat, lon) {
  for (const group of PANEL_GROUPS) {
    if (group.id === "mainland") continue;
    for (const panel of group.panels) {
      if (panel.bounds.contains([lat, lon])) return group.id;
    }
  }
  if (MAINLAND_BOUNDS.contains([lat, lon])) return "mainland";
  return null;
}

// --- Panel map factory ---
//
// Each panel group (mainland, or an archipelago) renders as one or more
// side-by-side panels, each its own independent Leaflet map instance tightly
// framed on its own sub-region -- island groups are too far apart to share
// one frame without either shrinking the mainland to a speck or forcing a
// rectangle that spans Morocco and half of Europe. Because each panel is a
// genuinely separate map, cross-panel panning is architecturally impossible,
// which is what makes "never fitBounds a rectangle spanning mainland and
// islands" hold by construction rather than by convention.

// preferCanvas: true renders every circle/polygon to a single <canvas>
// instead of one <path> SVG node each -- on the densest layers (thousands of
// species cells, or 97 region polygons) the old SVG renderer had to
// reposition every node on every drag frame, which caused a visible post-drag
// stall. Canvas just repaints once.
//
// zoomSnap: 0 makes zoom fully continuous (no snapping to any grid of
// levels at all). zoomDelta 0.1 and a much higher wheelPxPerZoomLevel mean a
// single scroll notch or +/- click is a small, gradual nudge rather than a
// jump, which matters at this scale where a whole default zoom level can be
// the difference between "whole archipelago" and "one island". maxZoom is
// high (24) so a single 10km cell is zoomable in much further.
//
// maxBounds fences each panel to its own area (padded a little) with
// maxBoundsViscosity: 1.0 so panning hits a firm, immediate stop at the edge
// instead of the elastic bounce-back a lower viscosity gives -- the area
// should never scroll off-screen. Computed fresh from this panel's own
// `bounds` argument every time createPanelMap runs.
function createPanelMap(containerEl, bounds, iberiaGeoJson, borderGeoJson) {
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
  // interactive:false so clicks pass through to whatever's drawn on top of
  // it (species density circles, or region polygons), since the land fill
  // otherwise covers the same points.
  L.geoJSON(iberiaGeoJson, {
    style: { fillColor: LAND_FILL, fillOpacity: 1, color: LAND_OUTLINE, weight: 1 },
    interactive: false,
  }).addTo(map);

  // Portugal/Spain border only actually falls inside the mainland panel's
  // bounds -- added to every panel anyway rather than special-cased, since
  // Leaflet skips out-of-view geometry for free and this keeps panel
  // creation generic.
  L.geoJSON(borderGeoJson, {
    style: { color: BORDER_COLOR, weight: 1, opacity: 0.7 },
    interactive: false,
  }).addTo(map);

  map.fitBounds(bounds, { padding: [12, 12] });

  // Every panel is already the tightly-fitted view of its own area --
  // zooming out below that just reveals surrounding empty space (open
  // ocean for an archipelago, or the gap where an archipelago would be but
  // isn't, for mainland) with nothing useful in it, so the fitted zoom is
  // always the floor. Applies uniformly on both pages now: mainland used to
  // be the one exception (free to zoom out), but that just meant scrolling
  // past the peninsula revealed dead space instead of anything real.
  map.setMinZoom(map.getZoom());

  return map;
}

// Builds the panel-wrap/label/map-container DOM for every panel in a group,
// appending each wrap to `container` before returning the map containers --
// two passes, not one: creating and sizing a Leaflet map interleaved with
// appending its still-to-come sibling panels was the actual cause of a past
// multi-panel misalignment bug (the Canaries, in species.js's original
// history). A flex child's width depends on how many siblings currently
// exist -- so a panel built in the SAME loop iteration that appends it
// (before its siblings are appended) gets initialized at its temporary,
// too-wide single-child width. L.map() measures the container once at
// construction and does not re-measure on its own, so every panel but the
// last in a multi-panel group ended up with a pixel origin computed for a
// container size it no longer had once the rest of the row appeared.
// Building every container first, and only then constructing any L.map
// (which the caller does with the returned elements), guarantees the flex
// row is already at its final layout before Leaflet ever measures anything.
function buildPanelDom(container, panels, lang) {
  return panels.map((panel) => {
    const wrap = document.createElement("div");
    wrap.className = "map-panel-wrap";
    wrap.style.flexGrow = panel.flex || 1;

    const label = document.createElement("div");
    label.className = "map-panel-label";
    label.textContent = t(panel.labelKey, lang);
    wrap.appendChild(label);

    const mapEl = document.createElement("div");
    mapEl.className = "map-panel";
    wrap.appendChild(mapEl);

    container.appendChild(wrap);
    return mapEl;
  });
}

// --- Density color scale (occurrence counts span 1 to tens of
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

// Adds a legend control (bottom-right) for the given bin edges to targetMap
// and returns it -- the caller owns removing any previous legend control
// before calling this again (each page tracks its own "current legend"
// state, since e.g. map.js's choropleth legend and species.js's cell-density
// legend are never both on screen at once but are otherwise independent).
function renderLegend(edges, targetMap) {
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
  return legend;
}

// Fixed radius, not data-scaled: density is conveyed entirely by color.
// ~6km against the 10km grid spacing means neighboring cells' circles
// overlap a little, so adjacent color bands blend into a continuous surface
// instead of a hard-edged tile grid.
const CELL_CIRCLE_RADIUS_M = 6000;

const METERS_PER_DEGREE_LAT = 111320;

// Batch-add: builds every circle first and adds them to the panel in one
// L.layerGroup rather than calling circle.addTo(map) inside a loop, so the
// densest layers (thousands of cells) trigger one insertion, not hundreds.
// popupHtmlFn is optional and page-specific (a species-map cell popup links
// to a GBIF search for that one species; a region-map "Alto-mar" cell popup
// has no single species to link to) -- omit it for circles with no popup.
function renderCellCircles(panelMap, cellsInPanel, edges, popupHtmlFn) {
  const circles = cellsInPanel.map((cell) => {
    const color = DENSITY_COLORS[binIndexForValue(cell.occurrences, edges)];
    const circle = L.circle([cell.centroid_lat, cell.centroid_lon], {
      radius: CELL_CIRCLE_RADIUS_M,
      stroke: false,
      fillColor: color,
      fillOpacity: 0.55,
    });
    if (popupHtmlFn) circle.bindPopup(popupHtmlFn(cell));
    return circle;
  });
  const group = L.layerGroup(circles);
  group.addTo(panelMap);
  return group;
}

// --- GBIF live-search link ---
//
// Approximate +/-5km box around the centroid, longitude-compensated for
// latitude (a naive fixed-degree box would be visibly non-square away from
// the equator). This is a convenience search-link footprint, not the exact
// MGRS cell geometry -- close enough for "show me GBIF records near this
// cell", where the value is a working link, not surveying-grade precision.
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

// eventDate lower bound matches MIN_YEAR in scripts/prepare_cube.py, so a
// live search doesn't surface pre-1990 records that our own dataset
// deliberately excludes. CUBE_DOWNLOAD_DATE bounds it to the same window the
// local occurrence cube covers, so the live count doesn't drift far past
// what our own numbers show as new sightings get uploaded to GBIF over time.
const CUBE_MIN_EVENT_DATE = "1990-01-01";
const CUBE_DOWNLOAD_DATE = "2026-08-28";

// Matches the licence filter already applied when building the local cube
// (scripts/prepare_cube.py's species-list/occurrence downloads exclude
// CC-BY-NC records) -- without this, GBIF's live count includes NC records
// our own dataset never had.
const CUBE_LICENSES = ["CC0_1_0", "CC_BY_4_0"];

// extraParams merges in on top of geometry/eventDate (e.g. {q: speciesName}
// for a species-specific search, or {} for a location-only one).
function gbifSearchUrl(centroidLat, centroidLon, extraParams) {
  const params = new URLSearchParams({
    geometry: approxCellBoundsWKT(centroidLat, centroidLon),
    eventDate: `${CUBE_MIN_EVENT_DATE},${CUBE_DOWNLOAD_DATE}`,
    ...extraParams,
  });
  for (const license of CUBE_LICENSES) {
    params.append("license", license);
  }
  return `https://www.gbif.org/occurrence/search?${params.toString()}`;
}
