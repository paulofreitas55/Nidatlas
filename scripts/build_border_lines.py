#!/usr/bin/env python
"""Extract the Portugal/Spain land border from Natural Earth's boundary lines.

Source: download
https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_0_boundary_lines_land.geojson
(public domain) and pass its path with --geojson-path.
"""

import argparse
import json
from pathlib import Path

import shapely.geometry
from shapely.ops import unary_union

OUTPUT_PATH = Path("static/pt_es_border.geojson")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--geojson-path",
        type=Path,
        required=True,
        help="Path to the downloaded ne_10m_admin_0_boundary_lines_land.geojson (see module docstring).",
    )
    return parser.parse_args()


def is_pt_es(properties: dict) -> bool:
    pair = {properties.get("ADM0_A3_L"), properties.get("ADM0_A3_R")}
    return pair == {"PRT", "ESP"}


def main() -> None:
    args = parse_args()
    geo = json.loads(args.geojson_path.read_text(encoding="utf-8"))

    segments = [
        shapely.geometry.shape(f["geometry"]) for f in geo["features"] if is_pt_es(f["properties"])
    ]
    if not segments:
        raise SystemExit("No Portugal/Spain boundary segments found -- check the input file.")
    merged = unary_union(segments)

    out = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Portugal-Spain border", "source": "Natural Earth", "license": "public domain"},
                "geometry": shapely.geometry.mapping(merged),
            }
        ],
    }
    OUTPUT_PATH.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"{len(segments)} segments merged -> {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
