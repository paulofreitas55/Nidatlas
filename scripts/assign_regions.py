#!/usr/bin/env python
"""Assign every grid_cells row to the administrative region containing its
centroid, from static/regions.geojson (built by scripts/build_regions.py).

Populates the regions table and grid_cells.region_id/region_name. Safe to
re-run: regions are upserted by region_key (stable across reruns, unlike an
autoincrement id which would depend on insertion order) and grid_cells
assignments are freshly recomputed each time, not accumulated.
"""

import json
import math
import sqlite3
from pathlib import Path

import shapely.geometry
from shapely.ops import nearest_points
from shapely.strtree import STRtree

DB_PATH = Path("data") / "nidatlas.db"
REGIONS_PATH = Path("static") / "regions.geojson"

# Cells whose centroid falls in no region polygon (offshore/pelagic records,
# or points just outside a region boundary at the coastline's own
# resolution) get bucketed here rather than left NULL, so region_summary()
# and any GROUP BY region_id can treat every cell uniformly.
FALLBACK_REGION = {
    "region_key": "OFFSHORE",
    "name_pt": "Alto-mar",
    "name_es": "Alta mar",
    "name_en": "Open sea",
    "kind": "fallback",
    "source_nuts_id": None,
}

# NUTS3 at 1:1,000,000 (the finest scale GISCO publishes -- see
# scripts/build_regions.py's module docstring) is still far coarser than the
# OSM coastline the map itself uses, so a real coastal cell's centroid can
# legitimately fall just outside the simplified region polygon even though
# the cell is obviously part of that region. Diagnosed before choosing this
# value (see the chat history /
# commit message for the full breakdown): of the cells with no containing
# polygon, 93% of their combined occurrences sat within 5km of the nearest
# region, and 98.4% within 15km -- a steep, natural cliff, not a smooth
# falloff. Species-level sanity check confirms the split is real: within
# 15km the top offenders are common land/urban birds (Passer domesticus,
# Turdus merula, Columba livia) whose grid cell's centroid just missed the
# coarse polygon; beyond 15km the list is dominated by genuine pelagic
# seabirds (Procellariiformes shearwaters/petrels, Morus bassanus,
# Hydrobates pelagicus). 15km rescues 959 cells / 98.4% of the affected
# occurrences while leaving 5,014 cells (1.6% of the occurrences) as
# legitimately offshore.
RESCUE_THRESHOLD_KM = 15
KM_PER_DEGREE_LAT = 111.32


def distance_km(point: shapely.geometry.Point, polygon) -> float:
    # Flat-earth approximation scaled by the point's own latitude for
    # longitude compensation -- accurate enough at the km scale relevant
    # here (same technique used in this project's earlier coastline/offshore
    # diagnostics), not true geodesic distance.
    p1, p2 = nearest_points(point, polygon)
    lat_rad = math.radians(point.y)
    dlat = (p2.y - p1.y) * KM_PER_DEGREE_LAT
    dlon = (p2.x - p1.x) * KM_PER_DEGREE_LAT * math.cos(lat_rad)
    return math.hypot(dlat, dlon)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS regions (
            id INTEGER PRIMARY KEY,
            region_key TEXT NOT NULL UNIQUE,
            name_pt TEXT NOT NULL,
            name_es TEXT NOT NULL,
            name_en TEXT NOT NULL,
            kind TEXT NOT NULL,
            source_nuts_id TEXT,
            total_occurrences INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    existing_region_cols = {row[1] for row in conn.execute("PRAGMA table_info(regions)")}
    if "total_occurrences" not in existing_region_cols:
        conn.execute("ALTER TABLE regions ADD COLUMN total_occurrences INTEGER NOT NULL DEFAULT 0")

    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(grid_cells)")}
    if "region_id" not in existing_cols:
        conn.execute("ALTER TABLE grid_cells ADD COLUMN region_id INTEGER REFERENCES regions(id)")
    if "region_name" not in existing_cols:
        conn.execute("ALTER TABLE grid_cells ADD COLUMN region_name TEXT")


def load_regions() -> list[dict]:
    geo = json.loads(REGIONS_PATH.read_text(encoding="utf-8"))
    regions = []
    for f in geo["features"]:
        p = f["properties"]
        regions.append({**p, "geometry": shapely.geometry.shape(f["geometry"])})
    return regions


def upsert_regions(conn: sqlite3.Connection, regions: list[dict]) -> dict[str, int]:
    region_id_by_key = {}
    for r in regions:
        conn.execute(
            """
            INSERT INTO regions (region_key, name_pt, name_es, name_en, kind, source_nuts_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(region_key) DO UPDATE SET
                name_pt=excluded.name_pt, name_es=excluded.name_es, name_en=excluded.name_en,
                kind=excluded.kind, source_nuts_id=excluded.source_nuts_id
            """,
            (r["region_key"], r["name_pt"], r["name_es"], r["name_en"], r["kind"], r["source_nuts_id"]),
        )
    for key, rid in conn.execute("SELECT region_key, id FROM regions"):
        region_id_by_key[key] = rid
    return region_id_by_key


def main() -> None:
    regions = load_regions()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = OFF")  # ALTER TABLE ADD COLUMN with a REFERENCES needs this off first
    ensure_schema(conn)

    all_regions = regions + [{**FALLBACK_REGION, "geometry": None}]
    region_id_by_key = upsert_regions(conn, all_regions)
    fallback_id = region_id_by_key[FALLBACK_REGION["region_key"]]

    polygons = [r["geometry"] for r in regions]
    tree = STRtree(polygons)
    key_by_index = [r["region_key"] for r in regions]
    name_by_key = {r["region_key"]: r["name_en"] for r in regions}
    name_by_key[FALLBACK_REGION["region_key"]] = FALLBACK_REGION["name_en"]

    cells = conn.execute("SELECT mgrs_cell, centroid_lat, centroid_lon FROM grid_cells").fetchall()
    print(f"Assigning {len(cells):,} cells to {len(regions)} regions ...")

    per_region_count: dict[str, int] = {}
    unmatched: list[tuple[str, float, float]] = []
    updates = []
    for mgrs_cell, lat, lon in cells:
        point = shapely.geometry.Point(lon, lat)
        candidate_indices = tree.query(point)
        matched_key = None
        for idx in candidate_indices:
            if polygons[idx].contains(point):
                matched_key = key_by_index[idx]
                break
        if matched_key is None:
            unmatched.append((mgrs_cell, lat, lon))
            continue
        per_region_count[matched_key] = per_region_count.get(matched_key, 0) + 1
        updates.append((region_id_by_key[matched_key], name_by_key[matched_key], mgrs_cell))

    # Second pass, only for cells with no containing polygon: snap to the
    # nearest region if it's within RESCUE_THRESHOLD_KM (see that constant's
    # comment for why 15km and how it was chosen), otherwise the cell stays
    # in the offshore fallback.
    rescued = 0
    still_offshore = 0
    for mgrs_cell, lat, lon in unmatched:
        point = shapely.geometry.Point(lon, lat)
        nearest_idx = tree.nearest(point)
        d = distance_km(point, polygons[nearest_idx])
        if d <= RESCUE_THRESHOLD_KM:
            matched_key = key_by_index[nearest_idx]
            rescued += 1
        else:
            matched_key = FALLBACK_REGION["region_key"]
            still_offshore += 1
        per_region_count[matched_key] = per_region_count.get(matched_key, 0) + 1
        updates.append((region_id_by_key[matched_key], name_by_key[matched_key], mgrs_cell))

    conn.executemany("UPDATE grid_cells SET region_id = ?, region_name = ? WHERE mgrs_cell = ?", updates)
    conn.commit()

    # Precomputed here (once, in this batch script), not at request time --
    # the equivalent live join (species_cell through grid_cells, grouped by
    # region) measured ~3.4s against the full 793k-row species_cell table,
    # far too slow for an endpoint the region map calls on every load. Same
    # precompute-then-serve-cheaply pattern as species.total_occurrences in
    # build_database.py.
    occurrences_by_region_id = dict(
        conn.execute(
            """
            SELECT gc.region_id, SUM(sc.occurrences)
            FROM species_cell sc
            JOIN grid_cells gc ON gc.mgrs_cell = sc.mgrs_cell
            GROUP BY gc.region_id
            """
        ).fetchall()
    )
    conn.executemany(
        "UPDATE regions SET total_occurrences = ? WHERE id = ?",
        [(occurrences_by_region_id.get(rid, 0), rid) for rid in region_id_by_key.values()],
    )
    conn.commit()

    print("\n=== Cells per region ===")
    for key, count in sorted(per_region_count.items(), key=lambda kv: -kv[1]):
        print(f"  {name_by_key[key]:<40} {count:>6,}")

    offshore = per_region_count.get(FALLBACK_REGION["region_key"], 0)
    print(f"\nRegions with at least one cell: {len(per_region_count)} of {len(regions) + 1}")
    print(f"Rescued within {RESCUE_THRESHOLD_KM}km of a region boundary: {rescued:,}")
    print(f"Offshore/pelagic fallback (>{RESCUE_THRESHOLD_KM}km from any region): "
          f"{offshore:,} of {len(cells):,} cells ({offshore / len(cells):.2%})")

    conn.close()


if __name__ == "__main__":
    main()
