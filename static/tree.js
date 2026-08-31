// Tree of Life view (tree.html): the full 577-species phylogeny, fetched
// ONCE from GET /api/phylo/root + GET /api/phylo/{root}/subtree (the whole
// tree is ~2,600 nodes, a modest payload) and rendered as a single
// continuous rectangular cladogram, fully expanded by default -- see
// CLAUDE.md's "Phylogeny frontend" section. Earlier versions of this page
// navigated level-by-level with a breadcrumb; that's gone in favour of one
// scrollable, zoomable canvas plus a search box, per the standard
// published-phylogeny layout (tip labels flush in a column on the right,
// branching structure on the left).

const ZOOM_MIN = 0.05;
const ZOOM_MAX = 3;
const ZOOM_STEP = 1.3;

const COL_WIDTH = 26; // compact -- with ~27 effective branch-point levels, a roomy per-tip colWidth would make the diagram absurdly wide (see git history for the measurement); paired with .tree-main's widened max-width so the aligned tip-label column still fits on screen without horizontal scrolling on most desktop viewports
const ROW_HEIGHT = 16;

const state = {
  lang: "en",
  rawNodesById: {}, // phylo_nodes.id -> raw API node (parent_id, is_tip, species_id, name, ...)
  childrenByParent: {}, // phylo_nodes.id -> [childId, ...], built once from parent_id
  speciesCountByNode: {}, // phylo_nodes.id -> count of species tips in its subtree
  rootId: null,
  collapsed: new Set(), // node ids whose subtree the user has manually hidden -- empty by default (fully expanded)
  zoom: 1,
  baseWidth: 0,
  baseHeight: 0, // last rendered SVG's own intrinsic (viewBox) size, at zoom=1
};

async function init() {
  const statusEl = document.getElementById("load-status");

  try {
    await loadTranslations();
  } catch (err) {
    statusEl.textContent = "Could not load the tree.";
    return;
  }

  state.lang = initLangSwitch((lang) => {
    state.lang = lang;
    document.documentElement.lang = lang;
    applyStaticTranslations(lang);
    renderFooter(lang);
    if (state.rootId != null) render();
  });
  document.documentElement.lang = state.lang;
  document.title = t("page.tree_title", state.lang);
  applyStaticTranslations(state.lang);
  renderFooter(state.lang);

  // Sequential, not Promise.all: the subtree request needs the root's own
  // id (never hardcoded -- see CLAUDE.md on phylo_nodes.id stability),
  // which only the first request reveals.
  let root;
  let subtree;
  try {
    // r.ok check matters here specifically: a 404's JSON body
    // ({"detail":"..."}) parses just fine and would otherwise be misread as
    // a valid (empty) response instead of a real request failure (e.g. an
    // old server process still running without these routes).
    root = await fetch("/api/phylo/root").then((r) => (r.ok ? r.json() : Promise.reject(r.status)));
    subtree = await fetch(`/api/phylo/${root.node_id}/subtree`).then((r) => (r.ok ? r.json() : Promise.reject(r.status)));
  } catch (err) {
    statusEl.textContent = t("tree.error_load", state.lang);
    return;
  }

  state.rootId = root.node_id;
  buildIndices(subtree.nodes);

  statusEl.hidden = true;
  document.getElementById("tree-content").hidden = false;

  render();

  const params = new URLSearchParams(location.search);
  const requestedNode = Number(params.get("node"));
  if (requestedNode && state.rawNodesById[requestedNode]) {
    revealNode(requestedNode);
  }

  setupZoomControls();
  setupSearch();
}

// --- Indices built once from the flat node list ---

function buildIndices(rawNodes) {
  state.rawNodesById = {};
  state.childrenByParent = {};
  for (const n of rawNodes) {
    state.rawNodesById[n.id] = n;
    if (n.parent_id != null) {
      (state.childrenByParent[n.parent_id] = state.childrenByParent[n.parent_id] || []).push(n.id);
    }
  }

  // Bottom-up species-tip count per node -- cheap here (one pass, ~2,600
  // nodes) and used both for the "(N species)" hint on a collapsed clade and
  // for search-result context.
  const order = rawNodes.slice().sort((a, b) => b.depth - a.depth); // deepest first
  for (const n of order) {
    if (n.is_tip) {
      state.speciesCountByNode[n.id] = n.species_id != null ? 1 : 0;
    } else {
      const kids = state.childrenByParent[n.id] || [];
      state.speciesCountByNode[n.id] = kids.reduce((sum, c) => sum + (state.speciesCountByNode[c] || 0), 0);
    }
  }
}

// A node with exactly one child conveys no branching information -- OToL's
// synthesis frequently inserts long single-child "mrcaottXottY" chains
// purely as an artifact of how it labels ancestors (observed directly: some
// species sit under 30+ nested single-child wrappers before the next real
// split -- measured max effective branch-point depth is 27, vs. a raw depth
// of 55). Walking straight through those to the next tip or real branch
// point is what keeps the rendered tree to "only the splits that actually
// happened".
function collapseToBranch(nodeId) {
  let current = nodeId;
  while (true) {
    const node = state.rawNodesById[current];
    if (node.is_tip) return current;
    const kids = state.childrenByParent[current] || [];
    if (kids.length !== 1) return current; // real branch point (>=2 children), or a childless internal node (shouldn't happen)
    current = kids[0];
  }
}

function effectiveChildren(nodeId) {
  return (state.childrenByParent[nodeId] || []).map(collapseToBranch);
}

// --- Collapsing (optional; default state is fully expanded) ---

function toggleCollapse(nodeId) {
  if (state.collapsed.has(nodeId)) {
    state.collapsed.delete(nodeId);
  } else {
    state.collapsed.add(nodeId);
  }
  render();
}

// Expands every collapsed ancestor of nodeId so it's actually present in the
// rendered slice -- used by both the `?node=` deep link and search, neither
// of which should silently do nothing just because some ancestor happens to
// be collapsed.
function expandAncestorsOf(nodeId) {
  let current = state.rawNodesById[nodeId].parent_id;
  let changed = false;
  while (current != null) {
    if (state.collapsed.delete(current)) changed = true;
    current = state.rawNodesById[current].parent_id;
  }
  return changed;
}

function revealNode(nodeId) {
  if (expandAncestorsOf(nodeId)) render();
  scrollToNode(nodeId);
}

function scrollToNode(nodeId) {
  const el = document.querySelector(`#tree-canvas [data-node-id="${nodeId}"]`);
  if (!el) return;
  el.scrollIntoView({ block: "center", inline: "center" });
  document.querySelectorAll(".cladogram-node.is-search-highlight").forEach((n) => n.classList.remove("is-search-highlight"));
  el.classList.add("is-search-highlight");
}

// --- Rendering ---
//
// Builds the FULL slice (every node from the root down, minus whatever the
// user has manually collapsed) and hands it to the shared renderer in one
// go -- no level cutoff. See CLAUDE.md for the measured render time.
function buildFullSlice() {
  const slice = {};

  function visit(nodeId) {
    if (slice[nodeId]) return;
    const raw = state.rawNodesById[nodeId];
    const isCollapsed = !raw.is_tip && state.collapsed.has(nodeId);
    const children = !raw.is_tip && !isCollapsed ? effectiveChildren(nodeId) : [];

    let label;
    let href = null;
    let muted = false;
    const count = state.speciesCountByNode[nodeId] || 0;

    if (raw.is_tip) {
      if (raw.species_id != null) {
        const vernacular = pickLabel(
          { pt: raw.common_name_pt, es: raw.common_name_es, en: raw.common_name_en },
          state.lang
        );
        label = vernacular ? `${raw.gbif_name} — ${vernacular}` : raw.gbif_name;
        href = `species.html?id=${raw.species_id}`;
      } else {
        label = t("tree.unresolved_tip", state.lang);
        muted = true;
      }
    } else if (isCollapsed) {
      const countLabel = tPlural("tree.node_species_count", count, state.lang, { count });
      label = raw.name ? `${raw.name} (${countLabel})` : countLabel;
    } else {
      label = raw.name || ""; // unnamed, expanded internal node: no placeholder text -- the branch point itself is the information
    }

    const clickable = !raw.is_tip;
    const nameForAria = raw.name || tPlural("tree.node_species_count", count, state.lang, { count });

    slice[nodeId] = {
      id: nodeId,
      children,
      label,
      isTip: raw.is_tip,
      href,
      clickable,
      muted,
      collapsed: isCollapsed,
      ariaLabel: clickable
        ? t(isCollapsed ? "tree.expand_aria" : "tree.collapse_aria", state.lang, { name: nameForAria })
        : null,
    };
    children.forEach(visit);
  }

  visit(state.rootId);
  return slice;
}

function render() {
  const canvas = document.getElementById("tree-canvas");
  canvas.innerHTML = "";

  const t0 = performance.now();
  const slice = buildFullSlice();
  const svg = renderCladogram(state.rootId, slice, {
    colWidth: COL_WIDTH,
    rowHeight: ROW_HEIGHT,
    alignTips: true,
    onNodeClick: toggleCollapse,
  });
  canvas.appendChild(svg);
  const elapsed = performance.now() - t0;
  console.log(`[tree] rendered ${Object.keys(slice).length} nodes in ${elapsed.toFixed(1)}ms`);

  state.baseWidth = Number(svg.dataset.baseWidth);
  state.baseHeight = Number(svg.dataset.baseHeight);
  applyZoom();
}

// --- Zoom ---
//
// Deliberately NOT a re-layout: the SVG's viewBox (its internal coordinate
// system) never changes, only its rendered width/height attributes -- the
// browser scales the whole drawing (lines, dots, text) proportionally, the
// same way scaling an <img> would. Cheap enough to run on every click with
// no visible lag, unlike recomputing colWidth/rowHeight and redrawing.
function applyZoom() {
  const svg = document.querySelector("#tree-canvas .cladogram-svg");
  if (!svg || !state.baseWidth) return;
  svg.setAttribute("width", state.baseWidth * state.zoom);
  svg.setAttribute("height", state.baseHeight * state.zoom);
  document.getElementById("tree-zoom-level").textContent = `${Math.round(state.zoom * 100)}%`;
}

function setZoom(zoom) {
  state.zoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom));
  applyZoom();
}

function setupZoomControls() {
  document.getElementById("tree-zoom-in").addEventListener("click", () => setZoom(state.zoom * ZOOM_STEP));
  document.getElementById("tree-zoom-out").addEventListener("click", () => setZoom(state.zoom / ZOOM_STEP));
  document.getElementById("tree-zoom-reset").addEventListener("click", () => setZoom(1));
}

// --- Search ---
//
// Matches a tip's scientific name or its vernacular name in the CURRENT
// language only (unlike the atlas grid's cross-language search) -- this
// search exists to jump to a spot in an already-visible tree, not to
// discover species the visitor doesn't already have a name for in front of
// them.
function searchTips(query) {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const results = [];
  for (const id in state.rawNodesById) {
    const n = state.rawNodesById[id];
    if (!n.is_tip || n.species_id == null) continue;
    const vernacular = pickLabel({ pt: n.common_name_pt, es: n.common_name_es, en: n.common_name_en }, state.lang) || "";
    if (n.gbif_name.toLowerCase().includes(q) || vernacular.toLowerCase().includes(q)) {
      results.push({ id: n.id, gbif_name: n.gbif_name, vernacular });
    }
    if (results.length >= 8) break;
  }
  return results;
}

function setupSearch() {
  const input = document.getElementById("tree-search-input");
  const resultsEl = document.getElementById("tree-search-results");

  function renderResults(matches) {
    resultsEl.innerHTML = "";
    if (matches.length === 0) {
      const empty = document.createElement("li");
      empty.className = "tree-search-empty";
      empty.textContent = t("tree.search_no_matches", state.lang);
      resultsEl.appendChild(empty);
    } else {
      for (const m of matches) {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "tree-search-result";
        btn.textContent = m.vernacular ? `${m.gbif_name} — ${m.vernacular}` : m.gbif_name;
        btn.addEventListener("click", () => {
          revealNode(m.id);
          resultsEl.hidden = true;
        });
        li.appendChild(btn);
        resultsEl.appendChild(li);
      }
    }
    resultsEl.hidden = false;
  }

  input.addEventListener("input", () => {
    const q = input.value;
    if (!q.trim()) {
      resultsEl.hidden = true;
      return;
    }
    renderResults(searchTips(q));
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      const matches = searchTips(input.value);
      if (matches.length > 0) {
        revealNode(matches[0].id);
        resultsEl.hidden = true;
      }
    } else if (event.key === "Escape") {
      resultsEl.hidden = true;
    }
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".tree-search")) resultsEl.hidden = true;
  });
}

init();
