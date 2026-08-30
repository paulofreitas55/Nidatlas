#!/usr/bin/env python
"""Build the SQLite database from the cleaned cube and the species list."""

import re
import sqlite3
from pathlib import Path

import mgrs
import pandas as pd
from pyproj import Transformer

DATA_DIR = Path("data")
CUBE_PATH = DATA_DIR / "cube_clean.parquet"
SPECIES_LIST_PATH = DATA_DIR / "iberian_species.txt"
DB_PATH = DATA_DIR / "nidatlas.db"

CELL_SIZE_M = 10_000
# zone, latitude band, 100km-square ID (unused), 2 digits = 1 per axis at 10km precision
CELL_RE = re.compile(r"^(\d{1,2})([C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d{2})$")
MGRS = mgrs.MGRS()
# one WGS84<->UTM transformer pair per (zone, hemisphere) actually seen, built on demand
_utm_transformers: dict[tuple[int, str], tuple[Transformer, Transformer]] = {}


def utm_transformers(zone: int, hemisphere: str) -> tuple[Transformer, Transformer]:
    key = (zone, hemisphere)
    if key not in _utm_transformers:
        epsg = f"EPSG:{326 if hemisphere == 'N' else 327}{zone:02d}"
        _utm_transformers[key] = (
            Transformer.from_crs("EPSG:4326", epsg, always_xy=True),
            Transformer.from_crs(epsg, "EPSG:4326", always_xy=True),
        )
    return _utm_transformers[key]

SCHEMA = """
CREATE TABLE species (
    id INTEGER PRIMARY KEY,
    gbif_name TEXT NOT NULL UNIQUE,
    bioclip_name TEXT NOT NULL,
    genus TEXT NOT NULL,
    family TEXT NOT NULL,
    "order" TEXT NOT NULL,
    total_occurrences INTEGER NOT NULL,
    -- Populated later by scripts/fetch_vernacular_names.py, not by this script.
    common_name_pt TEXT,
    common_name_es TEXT,
    common_name_en TEXT
);

-- Administrative regions (PT/ES NUTS3 districts/provinces, decomposed into
-- individual islands for the archipelagos -- see scripts/build_regions.py).
-- Populated later by scripts/assign_regions.py, not by this script (it needs
-- static/regions.geojson, built separately from a downloaded GISCO NUTS3
-- file) -- defined here, ahead of grid_cells, only so grid_cells.region_id
-- has something to reference.
CREATE TABLE regions (
    id INTEGER PRIMARY KEY,
    region_key TEXT NOT NULL UNIQUE,
    name_pt TEXT NOT NULL,
    name_es TEXT NOT NULL,
    name_en TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_nuts_id TEXT,
    -- Precomputed by scripts/assign_regions.py, not by this script (see its
    -- comment for why: the live join is too slow to run per API request).
    total_occurrences INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE grid_cells (
    mgrs_cell TEXT PRIMARY KEY,
    centroid_lat REAL NOT NULL,
    centroid_lon REAL NOT NULL,
    -- Populated later by scripts/assign_regions.py, same as regions above.
    region_id INTEGER REFERENCES regions(id),
    region_name TEXT
);

CREATE TABLE species_cell (
    species_id INTEGER NOT NULL REFERENCES species(id),
    mgrs_cell TEXT NOT NULL REFERENCES grid_cells(mgrs_cell),
    occurrences INTEGER NOT NULL,
    family_occurrences INTEGER NOT NULL,
    PRIMARY KEY (species_id, mgrs_cell)
);

CREATE TABLE species_cell_month (
    species_id INTEGER NOT NULL REFERENCES species(id),
    mgrs_cell TEXT NOT NULL REFERENCES grid_cells(mgrs_cell),
    month INTEGER NOT NULL,
    occurrences INTEGER NOT NULL,
    family_occurrences INTEGER NOT NULL,
    PRIMARY KEY (mgrs_cell, month, species_id)
);

CREATE TABLE species_month (
    species_id INTEGER NOT NULL REFERENCES species(id),
    month INTEGER NOT NULL,
    occurrences INTEGER NOT NULL,
    PRIMARY KEY (species_id, month)
);

CREATE TABLE species_year (
    species_id INTEGER NOT NULL REFERENCES species(id),
    year INTEGER NOT NULL,
    occurrences INTEGER NOT NULL,
    PRIMARY KEY (species_id, year)
);

-- Phylogenetic tree from the Open Tree of Life (see scripts/fetch_phylogeny.py
-- and scripts/build_phylogeny_db.py). Populated later, same reason as
-- regions/grid_cells above: defined here so nothing else has to guess at
-- the shape, but this script has no OToL data of its own to load into it.
-- One row per Newick node (leaf or internal) in the induced subtree.
-- ott_node_label is OToL's own raw label as it appears in the Newick
-- (e.g. "mrcaott817627ott3599091" for an unnamed synthesis node,
-- "Certhia_ott467879" for a named one) -- kept verbatim so nothing is
-- invented for the ~half of internal nodes OToL itself never named.
CREATE TABLE phylo_nodes (
    id INTEGER PRIMARY KEY,
    ott_node_label TEXT NOT NULL UNIQUE,
    name TEXT,
    ott_id INTEGER,
    rank TEXT,
    parent_id INTEGER REFERENCES phylo_nodes(id),
    species_id INTEGER REFERENCES species(id),
    is_tip INTEGER NOT NULL,
    -- This node's OWN depth from the root (root = 0), NOT to be confused
    -- with phylo_closure.depth below (which is the distance from a given
    -- ANCESTOR down to a given DESCENDANT). MRCA-of-two-nodes needs this
    -- column: among nodes that are a common ancestor of both, the most
    -- recent one is the one with the GREATEST depth here (deepest from
    -- root = most specific), not the smallest phylo_closure.depth (which
    -- only measures distance to one side and would wrongly favor whichever
    -- candidate happens to sit right above one of the two query nodes).
    depth INTEGER NOT NULL
);

-- Closure (every ancestor/descendant pair, not just parent/child) over
-- phylo_nodes -- see build_phylogeny_db.py's module docstring for why this
-- shape was chosen over nested sets or a materialized path.
CREATE TABLE phylo_closure (
    ancestor_id INTEGER NOT NULL REFERENCES phylo_nodes(id),
    descendant_id INTEGER NOT NULL REFERENCES phylo_nodes(id),
    depth INTEGER NOT NULL,
    PRIMARY KEY (ancestor_id, descendant_id)
);

-- phylo_closure's own PRIMARY KEY already indexes (ancestor_id, descendant_id)
-- for "descendants of this node" -- this covers the reverse ("ancestors of
-- this node", i.e. MRCA lookups) and phylo_nodes.parent_id covers "children
-- of this node" (subtree rendering), neither of which the PK helps with.
CREATE INDEX idx_phylo_closure_descendant ON phylo_closure(descendant_id);
CREATE INDEX idx_phylo_nodes_parent ON phylo_nodes(parent_id);
CREATE INDEX idx_phylo_nodes_species ON phylo_nodes(species_id);
"""


def load_species_list() -> pd.DataFrame:
    rows = []
    for line in SPECIES_LIST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            bioclip_name, gbif_name = line.split(",", 1)
            rows.append((gbif_name, bioclip_name))
    return pd.DataFrame(rows, columns=["gbif_name", "bioclip_name"])


def cell_centroid_lonlat(mgrs_cell: str) -> tuple[float, float]:
    match = CELL_RE.match(mgrs_cell)
    zone, band, _square, _digits = match.groups()
    zone = int(zone)
    hemisphere = "N" if band >= "N" else "S"
    to_utm, to_wgs84 = utm_transformers(zone, hemisphere)

    # mgrs.toLatLon() returns the cell's SW corner; reproject through the
    # cell's own UTM zone to offset by +5000m (half a 10km cell) on each axis
    # and get the true centroid, rather than approximating in lat/lon degrees.
    sw_lat, sw_lon = MGRS.toLatLon(mgrs_cell)
    easting, northing = to_utm.transform(sw_lon, sw_lat)
    return to_wgs84.transform(easting + CELL_SIZE_M / 2, northing + CELL_SIZE_M / 2)


def main() -> None:
    cube = pd.read_parquet(CUBE_PATH)
    species_list = load_species_list()

    species_agg = cube.groupby("species", observed=True).agg(
        genus=("genus", "first"),
        family=("family", "first"),
        order=("order", "first"),
        total_occurrences=("occurrences", "sum"),
    ).reset_index()
    species_df = species_list.merge(species_agg, left_on="gbif_name", right_on="species", how="inner")
    unmatched = len(species_list) - len(species_df)
    if unmatched:
        print(f"Warning: {unmatched} species in {SPECIES_LIST_PATH} have no rows in {CUBE_PATH}")
    species_df = species_df[["gbif_name", "bioclip_name", "genus", "family", "order", "total_occurrences"]]
    species_df = species_df.sort_values("gbif_name").reset_index(drop=True)
    species_df.insert(0, "id", range(1, len(species_df) + 1))
    species_id_by_name = dict(zip(species_df["gbif_name"], species_df["id"]))

    mgrs_cells = sorted(cube["mgrscellcode"].dropna().unique())
    grid_cells_df = pd.DataFrame(
        [(cell, *cell_centroid_lonlat(cell)[::-1]) for cell in mgrs_cells],
        columns=["mgrs_cell", "centroid_lat", "centroid_lon"],
    )

    no_cell = cube["mgrscellcode"].isna()
    if no_cell.any():
        print(f"Note: {no_cell.sum():,} rows ({cube.loc[no_cell, 'occurrences'].sum():,} occurrences) "
              "have no mgrscellcode and are excluded from species_cell/grid_cells, but still "
              "counted in species.total_occurrences, species_month, and species_year")

    cube = cube.assign(species_id=cube["species"].map(species_id_by_name))

    species_cell_df = cube.groupby(["species_id", "mgrscellcode"], observed=True).agg(
        occurrences=("occurrences", "sum"),
        family_occurrences=("familycount", "sum"),
    ).reset_index().rename(columns={"mgrscellcode": "mgrs_cell"})

    year_month = cube["yearmonth"].astype(str)
    cube = cube.assign(month=year_month.str.slice(5, 7).astype(int))

    species_cell_month_df = cube.dropna(subset=["mgrscellcode"]).groupby(
        ["species_id", "mgrscellcode", "month"], observed=True
    ).agg(
        occurrences=("occurrences", "sum"),
        family_occurrences=("familycount", "sum"),
    ).reset_index().rename(columns={"mgrscellcode": "mgrs_cell"})

    species_month_df = cube.groupby(
        ["species_id", "month"], observed=True
    ).agg(occurrences=("occurrences", "sum")).reset_index()

    species_year_df = cube.assign(year=year_month.str.slice(0, 4).astype(int)).groupby(
        ["species_id", "year"], observed=True
    ).agg(occurrences=("occurrences", "sum")).reset_index()

    DB_PATH.unlink(missing_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)

    species_df[["id", "gbif_name", "bioclip_name", "genus", "family", "order", "total_occurrences"]].to_sql(
        "species", conn, if_exists="append", index=False
    )
    grid_cells_df.to_sql("grid_cells", conn, if_exists="append", index=False)
    species_cell_df.to_sql("species_cell", conn, if_exists="append", index=False)
    species_cell_month_df.to_sql("species_cell_month", conn, if_exists="append", index=False)
    species_month_df.to_sql("species_month", conn, if_exists="append", index=False)
    species_year_df.to_sql("species_year", conn, if_exists="append", index=False)
    conn.commit()

    print("=== Row counts ===")
    for table in ["species", "grid_cells", "species_cell", "species_cell_month", "species_month", "species_year"]:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {count:,}")
    conn.close()

    print(f"\nFile size: {DB_PATH.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
