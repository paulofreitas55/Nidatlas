#!/usr/bin/env python
"""Clip the OSM land layer to just Portugal + Spain, dropping France,
Andorra, Morocco/Western Sahara, Gibraltar and anything else that fell
inside the study bounding box scripts/build_land_polygons.py clipped to.

OSM's land-polygons dataset isn't split by country (mainland Iberia is
fused into one Eurasia+Africa+Asia landmass), so the country boundary has
to come from somewhere else: Natural Earth's admin-0 country polygons,
used here purely as a clip mask -- the output keeps OSM's fine coastline
detail everywhere except the new inland edges (the France/Andorra border),
which take Natural Earth's own boundary geometry, coarser but adequate for
an inland political line with no coastline detail to preserve.

Source for --admin0-path: download
https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_0_countries.geojson
(public domain).
"""

import argparse
import json
from pathlib import Path

import shapely.geometry
from shapely.ops import unary_union

OUTPUT_PATH = Path("static/iberia.geojson")
COUNTRIES = ("Portugal", "Spain")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=OUTPUT_PATH,
        help="Bbox-clipped land GeoJSON to clip further, from scripts/build_land_polygons.py "
        "(default: static/iberia.geojson -- pass an explicit path when iterating on --tolerance "
        "so each run starts from the same pre-country-clip source instead of the previous output).",
    )
    parser.add_argument(
        "--admin0-path",
        type=Path,
        required=True,
        help="Path to the downloaded ne_10m_admin_0_countries.geojson (see module docstring).",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.0006,
        help="Douglas-Peucker simplify tolerance in degrees, applied after clipping.",
    )
    return parser.parse_args()


def vertex_count(geom) -> int:
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    return sum(len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors) for p in polys)


def main() -> None:
    args = parse_args()

    land = shapely.geometry.shape(json.loads(args.input.read_text(encoding="utf-8"))["features"][0]["geometry"])
    print(f"input: {vertex_count(land):,} vertices")

    admin0 = json.loads(args.admin0_path.read_text(encoding="utf-8"))
    country_polys = [
        shapely.geometry.shape(f["geometry"]) for f in admin0["features"] if f["properties"].get("NAME") in COUNTRIES
    ]
    if len(country_polys) != len(COUNTRIES):
        raise SystemExit(f"expected {len(COUNTRIES)} country features, found {len(country_polys)}")
    mask = unary_union(country_polys)

    clipped = land.intersection(mask)
    print(f"clipped to {COUNTRIES}: {vertex_count(clipped):,} vertices")

    simplified = clipped.simplify(args.tolerance, preserve_topology=True)
    print(f"simplified (tolerance={args.tolerance}): {vertex_count(simplified):,} vertices")

    feature = {
        "type": "Feature",
        "properties": {"source": "OpenStreetMap contributors", "license": "ODbL 1.0"},
        "geometry": shapely.geometry.mapping(simplified),
    }
    out = {"type": "FeatureCollection", "features": [feature]}
    OUTPUT_PATH.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
