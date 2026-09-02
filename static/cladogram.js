// Shared rectangular-cladogram renderer used by tree.js (the full navigable
// tree) and species.js (a species' local neighbourhood). Deliberately knows
// nothing about phylogeny, i18n or the API -- it only lays out and draws
// whatever small node graph its caller hands it, as plain SVG (no graph
// library). Each caller is responsible for its own slice of the tree: which
// nodes to include, how deep to go, and what a click on a given node means
// (see tree.js's level-navigation and species.js's static neighbourhood).

const SVG_NS = "http://www.w3.org/2000/svg";
const XLINK_NS = "http://www.w3.org/1999/xlink";

/**
 * nodesById: { [id]: {
 *   id, children: [id, ...] (empty for a leaf),
 *   label: string,           // "" renders no text at all -- see alignTips below
 *   isTip: boolean,
 *   href: string|null,       // set on a real species tip -- makes the node a link, never clickable
 *   clickable: boolean,      // set on a non-tip node the caller wants navigable (ignored if href is set)
 *   muted: boolean,          // dimmed styling -- used for tips with no resolved species (see CLAUDE.md)
 *   collapsed: boolean,      // this node's own subtree is hidden (children == []) by the caller's choice,
 *                            // not because it has none -- drawn as a distinct filled marker
 * } }
 * rootId: id to treat as this slice's own root (its real parent, if any, is irrelevant here)
 * options: {
 *   colWidth, rowHeight, highlightId, onNodeClick(id),
 *   labelMargin: extra width (beyond the deepest column) reserved for label text -- default
 *     300, enough for a scientific name plus a long vernacular one at the full tree's own font
 *     size. species.js's compact neighbourhood view passes a smaller value since its labels sit
 *     at a smaller font size to begin with (see its own renderCladogram call).
 *   alignTips: bool -- when true, every tip's dot stays at its real (depth-based) x, but its
 *     label is pushed out to a shared rightmost column with a dashed guide line connecting the
 *     two, so tip names read as a flush column regardless of how deep each one's branch runs
 *     (the standard published-cladogram layout tree.js uses for the full tree; species.js's
 *     small neighbourhood view leaves this off since its depth range is shallow enough not to
 *     need it).
 * }
 *
 * Returns the <svg> element (not yet attached) so the caller decides how/where to mount it.
 */
function renderCladogram(rootId, nodesById, options = {}) {
  const colWidth = options.colWidth || 190;
  const rowHeight = options.rowHeight || 26;
  const labelMargin = options.labelMargin || 300;
  const onNodeClick = options.onNodeClick || (() => {});
  const highlightId = options.highlightId;
  const alignTips = options.alignTips || false;

  // Depth is computed fresh from rootId (not read off any absolute
  // phylo_nodes.depth the caller might have) so this module works
  // identically whether it's drawing the whole tree from its true root or
  // a small neighbourhood rooted several levels down -- see species.js.
  const depthOf = {};
  const leafOrder = [];
  (function assignDepth(id, depth) {
    depthOf[id] = depth;
    const node = nodesById[id];
    if (node.children.length === 0) {
      leafOrder.push(id);
    } else {
      node.children.forEach((childId) => assignDepth(childId, depth + 1));
    }
  })(rootId, 0);

  // Leaves get an evenly-spaced row each, in the order this slice's own
  // children arrays list them (the caller controls that ordering); every
  // internal node's y is then the midpoint of its children's y -- the
  // standard rectangular-cladogram convention, computed bottom-up.
  const yOf = {};
  leafOrder.forEach((id, i) => {
    yOf[id] = i * rowHeight + rowHeight / 2;
  });
  (function computeY(id) {
    if (yOf[id] !== undefined) return yOf[id];
    const childYs = nodesById[id].children.map(computeY);
    const y = (Math.min(...childYs) + Math.max(...childYs)) / 2;
    yOf[id] = y;
    return y;
  })(rootId);

  const xOf = (id) => depthOf[id] * colWidth + 16;

  const maxDepth = Math.max(...Object.values(depthOf));
  const tipLabelX = maxDepth * colWidth + 16; // shared right-hand column for aligned tip labels
  const width = (maxDepth + 1) * colWidth + labelMargin; // labelMargin: room for the deepest column's own label text
  const height = leafOrder.length * rowHeight + rowHeight;

  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.setAttribute("class", "cladogram-svg");
  svg.setAttribute("role", "img");
  svg.dataset.baseWidth = width;
  svg.dataset.baseHeight = height;

  function addLine(x1, y1, x2, y2, extraClass) {
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", x1);
    line.setAttribute("y1", y1);
    line.setAttribute("x2", x2);
    line.setAttribute("y2", y2);
    line.setAttribute("class", extraClass ? `cladogram-edge ${extraClass}` : "cladogram-edge");
    svg.appendChild(line);
  }

  function addLabel(g, text, xOffset) {
    if (!text) return; // unnamed internal nodes render no text at all -- the branching IS the information
    const el = document.createElementNS(SVG_NS, "text");
    el.setAttribute("x", xOffset === undefined ? 8 : xOffset);
    el.setAttribute("y", 4);
    el.setAttribute("class", "cladogram-label");
    el.textContent = text;
    g.appendChild(el);
  }

  // Draws every node reachable from `id`, plus the elbow connecting each
  // parent to its children -- one vertical bar at the parent's own x
  // spanning the full range of its children's y (visually "this is where
  // the clade splits"), then one horizontal leg per child out to that
  // child's own x. This is the standard rectangular-cladogram link shape,
  // not a diagonal/curved one.
  function drawSubtree(id) {
    const node = nodesById[id];
    const x = xOf(id);
    const y = yOf[id];

    if (node.children.length > 0) {
      const childYs = node.children.map((c) => yOf[c]);
      if (node.children.length > 1) {
        addLine(x, Math.min(...childYs), x, Math.max(...childYs));
      }
      for (const childId of node.children) {
        addLine(x, yOf[childId], xOf(childId), yOf[childId]);
        drawSubtree(childId);
      }
    }

    // Aligned tip labels: the dot stays at the tip's real (depth-based) x --
    // topology stays undistorted -- but the label is pushed out to the
    // shared rightmost column, with a dashed guide connecting the two.
    const useAlignedLabel = alignTips && node.isTip && node.label && x < tipLabelX;
    if (useAlignedLabel) {
      addLine(x, y, tipLabelX, y, "cladogram-tip-extension");
    }

    const g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("class", [
      "cladogram-node",
      node.isTip ? "is-tip" : "is-internal",
      node.muted ? "is-muted" : "",
      node.collapsed ? "is-collapsed" : "",
      id === highlightId ? "is-highlighted" : "",
    ].filter(Boolean).join(" "));
    g.setAttribute("transform", `translate(${x},${y})`);
    g.setAttribute("data-node-id", id);

    const dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("r", node.collapsed ? 5 : 4);
    dot.setAttribute("class", "cladogram-dot");
    g.appendChild(dot);

    const labelOffset = useAlignedLabel ? tipLabelX - x + 8 : 8;

    if (node.href) {
      const a = document.createElementNS(SVG_NS, "a");
      a.setAttributeNS(XLINK_NS, "href", node.href);
      a.setAttribute("href", node.href);
      addLabel(a, node.label, labelOffset);
      g.appendChild(a);
    } else {
      addLabel(g, node.label, labelOffset);
      if (node.clickable) {
        g.classList.add("is-clickable");
        g.setAttribute("tabindex", "0");
        g.setAttribute("role", "button");
        if (node.ariaLabel) g.setAttribute("aria-label", node.ariaLabel);
        g.addEventListener("click", () => onNodeClick(id));
        g.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onNodeClick(id);
          }
        });
      }
    }
    svg.appendChild(g);
  }

  drawSubtree(rootId);
  return svg;
}
