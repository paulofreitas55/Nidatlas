#!/usr/bin/env python
"""Parse the Open Tree of Life induced subtree (fetched by
scripts/fetch_phylogeny.py) into data/nidatlas.db's phylo_nodes/phylo_closure
tables.

Storage choice: adjacency list (phylo_nodes.parent_id) PLUS a closure table
(phylo_closure: every ancestor/descendant pair, not just parent/child) --
not nested sets, not a materialized path. Chosen against the four query
patterns this feature needs to support:
  - most recent common ancestor of two species: one indexed self-join on
    phylo_closure (ancestors of A intersect ancestors of B, take the one
    with the greatest depth) -- plain SQL matching this project's existing
    style in src/queries.py. Nested sets need range-containment logic to
    find the smallest enclosing range; a materialized path needs
    longest-common-prefix logic (string ops, awkward in SQL); adjacency-list
    alone needs two recursive CTE ancestor-walks plus an app-side
    intersection. MRCA is the pattern that makes closure-table the clear
    winner, since it's explicitly one of the four required patterns.
  - all descendants of a node: one indexed range scan on phylo_closure
    (WHERE ancestor_id = ?), same cost class as nested sets, no recursion.
  - closest relatives of a species: parent lookup (phylo_nodes.parent_id)
    + phylo_closure descendants of that parent, filtered to tips.
  - render a subtree: phylo_nodes.parent_id gives real parent/child edges
    directly (a closure table alone can't reconstruct topology, hence
    keeping the adjacency columns too, not swapping them out).
The tree is tiny (~1,200 nodes) and, like grid_cells/regions, is rebuilt
from scratch by this script every run rather than mutated in place -- so
the closure table's usual weakness (expensive to keep in sync under
inserts/deletes) never applies; it's the same precompute-once,
serve-cheaply pattern this project already uses for regions.total_occurrences.

Internal nodes are frequently unnamed in OToL's own synthesis (a raw
"mrcaott<X>ott<Y>" label -- literally "most recent common ancestor of OTT
ids X and Y", not a real taxon) -- these are kept as their own rows with a
NULL name/ott_id/rank, not invented or collapsed, exactly as asked.
"""

import json
import re
import sqlite3
from pathlib import Path

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "nidatlas.db"
SUBTREE_CACHE_PATH = DATA_DIR / "opentree_induced_subtree_raw.json"
RESOLUTIONS_PATH = DATA_DIR / "opentree_resolutions.json"

# Every node label in this Newick is one of:
#   "mrcaott<int>ott<int>"                -- unnamed synthesis-only placeholder
#   "<Name_with_underscores>_ott<int>"    -- named (species tip, or a named
#                                             internal node OToL's taxonomy
#                                             happens to have a label for)
#   "'<Name (genus in <Clade>) ott<int>'" -- a homonym-disambiguated genus
#                                             name, single-quoted per standard
#                                             Newick quoting since it contains
#                                             spaces/parens (e.g. Passerina is
#                                             also a genus elsewhere in the
#                                             tree of life, so OToL labels
#                                             this one "Passerina (genus in
#                                             Opisthokonta)" to disambiguate)
# There are no branch lengths (no ':' anywhere).
MRCA_LABEL_RE = re.compile(r"^mrcaott\d+ott\d+$")
NAMED_LABEL_RE = re.compile(r"^(.+)[ _]ott(\d+)$")


def parse_newick(text: str) -> dict:
    text = text.strip()
    if not text.endswith(";"):
        raise ValueError("expected a Newick string ending in ';'")
    text = text[:-1]
    pos = 0

    def parse_label() -> str:
        nonlocal pos
        if pos < len(text) and text[pos] == "'":
            # Standard Newick quoting: a label containing '(', ')', ',' or
            # spaces is wrapped in single quotes, with '' as an escaped
            # literal quote inside (not observed in this Newick, but cheap
            # to handle correctly rather than assume it can't happen).
            pos += 1
            chars = []
            while True:
                if text[pos] == "'":
                    if pos + 1 < len(text) and text[pos + 1] == "'":
                        chars.append("'")
                        pos += 2
                        continue
                    pos += 1
                    break
                chars.append(text[pos])
                pos += 1
            return "".join(chars)
        start = pos
        while pos < len(text) and text[pos] not in "(),":
            pos += 1
        return text[start:pos]

    def parse_subtree() -> dict:
        nonlocal pos
        children = []
        if text[pos] == "(":
            pos += 1
            children.append(parse_subtree())
            while text[pos] == ",":
                pos += 1
                children.append(parse_subtree())
            if text[pos] != ")":
                raise ValueError(f"expected ')' at position {pos}, found {text[pos:pos + 20]!r}")
            pos += 1
        label = parse_label()
        return {"label": label, "children": children}

    root = parse_subtree()
    if pos != len(text):
        raise ValueError(f"trailing content after root: {text[pos:pos + 40]!r}")
    return root


def parse_label_metadata(label: str) -> tuple[str | None, int | None]:
    """Returns (name, ott_id) for a Newick label -- (None, None) for an
    unnamed mrca placeholder, (name, ott_id) for anything OToL did name."""
    if MRCA_LABEL_RE.match(label):
        return None, None
    m = NAMED_LABEL_RE.match(label)
    if not m:
        raise ValueError(f"unrecognized node label format: {label!r}")
    return m.group(1).replace("_", " "), int(m.group(2))


def flatten_tree(root: dict, species_id_by_ott_id: dict[int, int]) -> list[dict]:
    """Pre-order walk assigning stable sequential ids and parent links.
    Root gets id 1; a node's id is always greater than its parent's (an
    invariant the closure-building step below relies on for a single
    forward pass instead of needing a second traversal)."""
    nodes: list[dict] = []
    depth_by_id: dict[int, int] = {}

    def visit(node: dict, parent_id: int | None) -> None:
        name, ott_id = parse_label_metadata(node["label"])
        is_tip = len(node["children"]) == 0
        node_id = len(nodes) + 1
        depth = 0 if parent_id is None else depth_by_id[parent_id] + 1
        depth_by_id[node_id] = depth
        nodes.append({
            "id": node_id,
            "ott_node_label": node["label"],
            "name": name,
            "ott_id": ott_id,
            "rank": "species" if (is_tip and ott_id in species_id_by_ott_id) else None,
            "parent_id": parent_id,
            "species_id": species_id_by_ott_id.get(ott_id) if is_tip else None,
            "is_tip": is_tip,
            "depth": depth,
        })
        for child in node["children"]:
            visit(child, node_id)

    visit(root, None)
    return nodes


def build_closure(nodes: list[dict]) -> list[tuple[int, int, int]]:
    """Every (ancestor_id, descendant_id, depth) pair, built in one forward
    pass: since flatten_tree numbers nodes in pre-order, a node's ancestors
    all already have their own ancestor lists computed by the time it's
    visited, so each node just extends its parent's list by itself."""
    ancestors_of: dict[int, list[tuple[int, int]]] = {}  # node id -> [(ancestor_id, depth), ...]
    closure: list[tuple[int, int, int]] = []
    for node in nodes:
        parent_ancestors = ancestors_of.get(node["parent_id"], [])
        own_ancestors = [(a, d + 1) for a, d in parent_ancestors] + [(node["id"], 0)]
        ancestors_of[node["id"]] = own_ancestors
        for ancestor_id, depth in own_ancestors:
            closure.append((ancestor_id, node["id"], depth))
    return closure


def ensure_schema(conn: sqlite3.Connection) -> None:
    # CREATE TABLE IF NOT EXISTS, not a hard requirement that
    # build_database.py was just rerun: build_database.py's SCHEMA is the
    # canonical definition (so a fresh rebuild already has these tables),
    # but build_database.py wipes and rebuilds the ENTIRE database from the
    # raw cube every time, which would also throw away assign_regions.py's
    # region assignments -- same tradeoff already documented in CLAUDE.md
    # for assign_regions.py's own ensure_schema(), extended here to a third
    # script for the same reason: staying safely re-runnable against a
    # database that already has real data, without forcing a full pipeline
    # rebuild just to add two new tables. Must be kept in sync BY HAND with
    # build_database.py's SCHEMA string if either changes.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS phylo_nodes (
            id INTEGER PRIMARY KEY,
            ott_node_label TEXT NOT NULL UNIQUE,
            name TEXT,
            ott_id INTEGER,
            rank TEXT,
            parent_id INTEGER REFERENCES phylo_nodes(id),
            species_id INTEGER REFERENCES species(id),
            is_tip INTEGER NOT NULL,
            depth INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS phylo_closure (
            ancestor_id INTEGER NOT NULL REFERENCES phylo_nodes(id),
            descendant_id INTEGER NOT NULL REFERENCES phylo_nodes(id),
            depth INTEGER NOT NULL,
            PRIMARY KEY (ancestor_id, descendant_id)
        );
        CREATE INDEX IF NOT EXISTS idx_phylo_closure_descendant ON phylo_closure(descendant_id);
        CREATE INDEX IF NOT EXISTS idx_phylo_nodes_parent ON phylo_nodes(parent_id);
        CREATE INDEX IF NOT EXISTS idx_phylo_nodes_species ON phylo_nodes(species_id);
        """
    )
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(phylo_nodes)")}
    if "depth" not in existing_cols:
        conn.execute("ALTER TABLE phylo_nodes ADD COLUMN depth INTEGER NOT NULL DEFAULT 0")


def main() -> None:
    resolutions = json.loads(RESOLUTIONS_PATH.read_text(encoding="utf-8"))
    species_id_by_ott_id = {info["ott_id"]: info["species_id"] for info in resolutions["resolved"].values()}

    subtree_cache = json.loads(SUBTREE_CACHE_PATH.read_text(encoding="utf-8"))
    newick = subtree_cache["subtree"]["newick"]

    print(f"Parsing Newick ({len(newick):,} chars) ...")
    root = parse_newick(newick)
    nodes = flatten_tree(root, species_id_by_ott_id)
    closure = build_closure(nodes)

    n_tips = sum(1 for n in nodes if n["is_tip"])
    n_linked = sum(1 for n in nodes if n["species_id"] is not None)
    print(f"Parsed {len(nodes):,} nodes ({n_tips:,} tips, {len(nodes) - n_tips:,} internal); "
          f"{n_linked:,} tips linked to a species row.")
    expected_linked = sum(1 for info in resolutions["resolved"].values() if info["in_tree"])
    if n_linked != expected_linked:
        raise SystemExit(
            f"Linked-tip count ({n_linked}) doesn't match fetch_phylogeny.py's own in_tree "
            f"count ({expected_linked}) -- the cached subtree and resolutions.json are out of "
            "sync, rerun scripts/fetch_phylogeny.py before this script."
        )

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)

    # Safe to rerun: this script owns these two tables entirely and always
    # repopulates them from scratch, same convention as assign_regions.py's
    # "upserts/recomputes, doesn't accumulate" for regions/grid_cells.
    conn.execute("DELETE FROM phylo_closure")
    conn.execute("DELETE FROM phylo_nodes")
    conn.executemany(
        """INSERT INTO phylo_nodes (id, ott_node_label, name, ott_id, rank, parent_id, species_id, is_tip, depth)
           VALUES (:id, :ott_node_label, :name, :ott_id, :rank, :parent_id, :species_id, :is_tip, :depth)""",
        nodes,
    )
    conn.executemany(
        "INSERT INTO phylo_closure (ancestor_id, descendant_id, depth) VALUES (?, ?, ?)",
        closure,
    )
    conn.commit()

    root_node = nodes[0]
    max_depth = max(n["depth"] for n in nodes)
    print(f"\nRoot: {root_node['name'] or root_node['ott_node_label']} (id={root_node['id']})")
    print(f"Max depth (root to deepest tip): {max_depth}")
    print(f"Closure table rows: {len(closure):,}")
    print(f"Wrote phylo_nodes ({len(nodes):,} rows) and phylo_closure ({len(closure):,} rows) to {DB_PATH}")

    unlinked_species = [
        name for name, info in resolutions["resolved"].items() if not info["in_tree"]
    ]
    if unlinked_species:
        print(f"\n{len(unlinked_species)} resolved species have no tip in this tree "
              "(pruned/unsampled at OToL's synthesis stage -- see fetch_phylogeny.py's report):")
        for name in sorted(unlinked_species):
            print(f"  - {name}")

    conn.close()


if __name__ == "__main__":
    main()
