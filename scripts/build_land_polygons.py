#!/usr/bin/env python
"""Clip and simplify OSM land-polygons to the Iberia + Macaronesia study area.

Source: download and unzip
https://osmdata.openstreetmap.de/download/land-polygons-complete-4326.zip
(~920MB; OSM coastline-derived, ODbL 1.0, (c) OpenStreetMap contributors)
and pass the path to the extracted .shp with --shp-path. Unlike the Natural
Earth admin-0 boundaries this replaces, this dataset is not split by country
-- mainland Iberia is fused into one enormous Eurasia+Africa+Asia polygon, so
every candidate record needs a real bbox clip, not just a bbox-overlap filter.
"""

import argparse
import time
from pathlib import Path

import shapefile
import shapely.geometry
from shapely.geometry import box
from shapely.ops import unary_union

# West, south, east, north -- generous padding around the mainland, Azores,
# Madeira and Canaries (including the previously-missing Desertas, Selvagens
# and La Graciosa islets), computed from the archipelago extents already
# established in static/species.js's REGIONS plus margin.
STUDY_BBOX = (-32.0, 26.5, 5.0, 44.5)

OUTPUT_PATH = Path("static/iberia.geojson")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shp-path",
        type=Path,
        required=True,
        help="Path to the extracted land_polygons.shp (see module docstring for the download URL).",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.0006,
        help="Douglas-Peucker simplify tolerance in degrees (~65m at this latitude).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after N candidate records (debug).")
    return parser.parse_args()


def bbox_overlaps(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def main() -> None:
    args = parse_args()
    study_box = box(*STUDY_BBOX)

    sf = shapefile.Reader(str(args.shp_path))
    total = len(sf)
    print(f"Scanning {total:,} records for bbox overlap with {STUDY_BBOX} ...")

    pieces = []
    start = time.monotonic()
    scanned = 0
    candidates = 0
    for shape in sf.iterShapes():
        scanned += 1
        if scanned % 100_000 == 0:
            elapsed = time.monotonic() - start
            print(f"  scanned {scanned:,}/{total:,} ({elapsed:.0f}s), candidates so far: {candidates}")
        if not shape.points:
            continue
        if not bbox_overlaps(shape.bbox, STUDY_BBOX):
            continue
        geom = shapely.geometry.shape(shape.__geo_interface__)
        clipped = geom.intersection(study_box)
        if clipped.is_empty:
            continue
        pieces.append(clipped)
        candidates += 1
        if args.limit and candidates >= args.limit:
            break

    elapsed = time.monotonic() - start
    print(f"Scan complete in {elapsed:.0f}s. {candidates} candidate polygons intersect the study area.")

    print("Unioning candidates ...")
    merged = unary_union(pieces)
    print(f"Simplifying with tolerance={args.tolerance} deg ...")
    simplified = merged.simplify(args.tolerance, preserve_topology=True)

    feature = {
        "type": "Feature",
        "properties": {
            "source": "OpenStreetMap contributors",
            "license": "ODbL 1.0",
        },
        "geometry": shapely.geometry.mapping(simplified),
    }
    out = {"type": "FeatureCollection", "features": [feature]}

    import json

    OUTPUT_PATH.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")

    n_polygons = len(simplified.geoms) if simplified.geom_type == "MultiPolygon" else 1
    n_points = sum(
        len(poly.exterior.coords) + sum(len(r.coords) for r in poly.interiors)
        for poly in (simplified.geoms if simplified.geom_type == "MultiPolygon" else [simplified])
    )
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1e6:.2f} MB)")
    print(f"Polygons: {n_polygons}, total vertices: {n_points:,}")


if __name__ == "__main__":
    main()
