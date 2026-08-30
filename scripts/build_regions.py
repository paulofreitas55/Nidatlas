#!/usr/bin/env python
"""Build the administrative regions layer: PT/ES NUTS3 districts/provinces,
decomposed into individual islands for the archipelagos.

Source: download
https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_01M_2024_4326_LEVL_3.geojson
(Eurostat GISCO, NUTS 2024 level 3, 1:1,000,000 -- the finest scale GISCO
publishes for NUTS, CC BY 4.0) and pass its path with --nuts3-path. Only
used for administrative naming/boundaries and for mainland district shapes
now (see ARCHIPELAGO_LAND_CLUSTERS below) -- 01M was worth using over the
coarser 03M this project started with regardless, since mainland district
boundaries still come from it directly, but note that even 01M measurably
under-resolves small islands (see the module docstring's Desertas/Selvagens
measurements), which is why island shapes no longer come from this file at
all.

NUTS3 already splits the Canary Islands into 7 units (one per island) and
the Balearics into 3 (Mallorca, Menorca, Eivissa y Formentera combined), but
Acores (PT200) and Madeira (PT300) are each a single NUTS3 unit. GISCO's own
geometry is used to split those (plus ES531, to split Ibiza from Formentera)
into disjoint parts clustered to the nearest of a small set of known island
reference points ONLY to figure out which named island each region_key
belongs to -- the ACTUAL RENDERED SHAPE for every island-kind region comes
from static/iberia.geojson instead (see ISLAND_LAND_SHAPES / the
--land-path argument), not from GISCO.

Why: GISCO's NUTS product is generalized for statistical-boundary use, not
coastline accuracy, and that generalization is severe for small islands --
diagnosed directly (not guessed) by intersecting GISCO's own polygon for
each Madeira-archipelago island against the OSM-derived land layer already
used for this app's basemap: Desertas overlapped real land only 15% of its
own area at 1:3,000,000 and just 31% at GISCO's finest available NUTS scale
(1:1,000,000); Selvagens 33% and 56% respectively. In practice this drew
"Desertas"/"Selvagens"/Porto Santo's satellite islet as blobs sitting mostly
over open water on the very same map that shows the precise coastline
underneath them -- clickable, correctly labeled, but visibly detached from
any land. static/iberia.geojson's OSM-derived land polygon already contains
each real island as its own accurate, correctly-positioned disjoint part
(confirmed: its Madeira/Porto Santo/Desertas-main-islet part areas match
GISCO's within rounding), so mainland-vs-generalized-boundary mismatch
simply cannot occur there -- GISCO is used only to decide which named
region a given land shape belongs to, via the same nearest-reference-point
clustering, applied to iberia.geojson's parts instead of GISCO's own.

Mainland district/province shapes still come from GISCO directly: an
island's outer coastline is the whole of its boundary, so a wholesale swap
to the matching land shape is clean and unambiguous. A mainland district's
boundary is mostly INTERNAL lines shared with neighbouring districts, which
only exist in GISCO's dataset at all -- there is no equivalent OSM shape to
swap in for those, so this round leaves mainland geometry untouched.

Naming limitation, disclosed rather than guessed around: GISCO's NAME_ENGL
field is not usefully populated at NUTS3 level for PT/ES (it just repeats
the country name), and there is no verified source here for cross-language
exonyms (e.g. a Portuguese translation of "Sevilla", a Spanish translation
of "Alto Minho"). Administrative unit names are therefore used as-is across
name_pt/name_es/name_en -- the same defensible approach already used for
island proper names in static/i18n.json (Flores, Corvo, Tenerife, ...),
extended here to district/province names, most of which are used unchanged
in English text anyway (Barcelona, Madrid, Zaragoza).
"""

import argparse
import json
from pathlib import Path

import shapely.geometry
from shapely.ops import unary_union

OUTPUT_PATH = Path("static/regions.geojson")
LAND_PATH = Path("static/iberia.geojson")

# Web-delivery simplification for the map view (region choropleth at country/
# archipelago zoom, not surveying use), applied to every feature's final
# geometry regardless of whether it came from GISCO (mainland) or
# static/iberia.geojson (islands). Both a Douglas-Peucker pass and rounding
# coordinates to 5 decimal places (~1.1m at these latitudes, far below
# anything visible at any zoom level this map uses) measurably shrink the
# file; coordinate rounding is the bigger of the two (the source data ships
# ~15 significant digits per number, sub-millimeter, meaningless at this
# scale). Island shapes now carry real OSM coastline detail instead of
# GISCO's coarser generalization, so the output is larger than it used to be
# (~0.6 MB vs ~0.2 MB) -- an intentional tradeoff for actually matching the
# coastline the choropleth is drawn over, and still small next to
# static/iberia.geojson itself.
#
# Kept tight (not the more aggressive 0.001 this used to be) specifically
# for mainland districts: GISCO's own NUTS3 polygons don't share exact
# boundary vertices between adjacent units even unsimplified -- diagnosed
# directly, 176 adjacent-district pairs have a small real gap between them
# in the raw source itself, visible as tiny slivers of bare basemap between
# same-colored neighbours. Simplifying each district independently (Shapely
# has no idea two features share an edge) makes this measurably worse, not
# better: the largest such gap grows 10x between this tolerance and the
# more aggressive one previously used here, for a mainland vertex-count
# saving of well under 15% between the two -- a bad trade. This tolerance
# doesn't eliminate the gaps (they predate any simplification this script
# does), just avoids visibly widening them for little file-size benefit.
SIMPLIFY_TOLERANCE_DEG = 0.0002
COORD_DECIMALS = 5

# Drops MultiPolygon parts that are cartographic noise, not real relative
# significance. Diagnosed directly (not guessed): many small, otherwise
# unrelated NUTS3 units share a near-identical ~1.6-2.4 km2 floor size for
# their smallest disjoint parts (e.g. A Coruna, Murcia, Guadalajara, Eivissa
# all have a part in that exact range despite having nothing else in
# common) -- confirmed against the raw, undownloaded-by-this-script GISCO
# source (not something SIMPLIFY_TOLERANCE_DEG/COORD_DECIMALS above
# introduce) that this is GISCO's own 1:3,000,000 generalization: a real
# but far smaller islet/rock generalized up to a minimum resolvable blob at
# that scale. A genuinely meaningful disjoint part -- a real named
# satellite island (Desertas, Selvagens, Porto Santo), a real
# administrative exclave (Treviño/Burgos) -- is always at least ~1% of its
# OWN feature's total area in the data actually observed here, with a clean
# gap between the largest dropped candidate (0.46%) and the smallest kept
# one (1.15%); 0.5% sits in the middle of that gap, not tuned to a specific
# case. The largest part is always kept regardless, so a feature can never
# be filtered down to nothing.
#
# Also applied to island shapes now sourced from static/iberia.geojson (see
# build_island_land_shapes) for the same reason, one level down: clustering
# every nearby OSM land part to its nearest named island can sweep up
# genuinely tiny, unrelated rock fragments alongside the real island, and
# this same threshold cleans those up too.
MIN_PART_AREA_SHARE = 0.005


def drop_generalization_slivers(geom):
    if geom.geom_type != "MultiPolygon" or len(geom.geoms) <= 1:
        return geom
    parts = sorted(geom.geoms, key=lambda p: p.area, reverse=True)
    total = geom.area
    kept = [parts[0]] + [p for p in parts[1:] if p.area / total >= MIN_PART_AREA_SHARE]
    return kept[0] if len(kept) == 1 else shapely.geometry.MultiPolygon(kept)

# NUTS3 codes to decompose into individual islands, and each island's
# reference centroid (lat, lon) to cluster the source MultiPolygon's
# disjoint parts against -- taken from the actual observed part centroids
# (each Acores part IS already a single whole island; Madeira/Balearics
# reference points are hand-picked centers for the known island groups).
ISLAND_DECOMPOSITIONS = {
    "PT200": {  # Acores -- 9 parts, each already exactly one island
        "Sao Miguel": (37.80, -25.48),
        "Santa Maria": (36.97, -25.10),
        "Terceira": (38.72, -27.21),
        "Graciosa": (39.05, -28.01),
        "Sao Jorge": (38.64, -28.03),
        "Pico": (38.47, -28.33),
        "Faial": (38.58, -28.70),
        "Flores": (39.44, -31.20),
        "Corvo": (39.70, -31.11),
    },
    "PT300": {  # Madeira -- 8 parts cluster into 4 named groups
        "Madeira": (32.75, -17.00),
        "Porto Santo": (33.02, -16.36),
        "Desertas": (32.46, -16.50),
        "Selvagens": (30.09, -15.95),
    },
    "ES531": {  # Eivissa y Formentera -- 7 parts cluster into Ibiza/Formentera
        "Ibiza": (38.98, 1.41),
        "Formentera": (38.69, 1.46),
    },
}

# Native-language names for the decomposed islands (name_pt/es/en all use
# the same native form, per the module docstring's naming-limitation note).
ISLAND_NATIVE_NAMES = {
    "Sao Miguel": "São Miguel", "Santa Maria": "Santa Maria", "Terceira": "Terceira",
    "Graciosa": "Graciosa", "Sao Jorge": "São Jorge", "Pico": "Pico", "Faial": "Faial",
    "Flores": "Flores", "Corvo": "Corvo", "Madeira": "Madeira", "Porto Santo": "Porto Santo",
    "Desertas": "Desertas", "Selvagens": "Selvagens", "Ibiza": "Eivissa", "Formentera": "Formentera",
}

ISLAND_KIND_NUTS_IDS = {"ES531", "ES532", "ES533", "ES703", "ES704", "ES705", "ES706", "ES707", "ES708", "ES709"}

# Every island-kind region's rendered shape now comes from static/iberia.geojson
# (see module docstring), matched by the same nearest-reference-point
# clustering ISLAND_DECOMPOSITIONS already uses for GISCO's own geometry --
# applied here to iberia.geojson's disjoint land parts instead. Grouped by
# archipelago so each cluster only searches land parts in its own generous
# bounding box (well clear of any other archipelago), not the whole
# 2,900+-part land layer, and so two nearby-but-different archipelagos can
# never cross-contaminate each other's islands.
#
# Islands already living in ISLAND_DECOMPOSITIONS (Azores/Madeira/Ibiza &
# Formentera) are reused as-is, keyed by their display name (resolved to a
# region_key via region_key(parent_nuts_id, name), same as before). Mallorca,
# Menorca and the 7 Canary Islands are each already their own standalone
# NUTS3 unit needing no further decomposition -- keyed directly by NUTS_ID
# here instead, with reference centroids taken from GISCO's own polygon
# centroid for that unit (there was no need to hand-pick these the way the
# Madeira/Ibiza-Formentera groups were, since GISCO already draws them as
# separate units).
ARCHIPELAGO_LAND_CLUSTERS = {
    "azores": {
        "bbox": (-32.0, 36.0, -24.0, 40.0),
        "islands": ISLAND_DECOMPOSITIONS["PT200"],
    },
    "madeira": {
        "bbox": (-17.6, 29.7, -15.5, 33.3),
        "islands": ISLAND_DECOMPOSITIONS["PT300"],
    },
    "balearics": {
        "bbox": (0.9, 38.4, 4.6, 40.3),
        "islands": {
            **ISLAND_DECOMPOSITIONS["ES531"],
            "ES532": (39.6119, 2.9564),  # Mallorca
            "ES533": (39.9600, 4.0736),  # Menorca
        },
    },
    "canaries": {
        "bbox": (-18.5, 27.4, -13.0, 29.5),
        "islands": {
            "ES703": (27.7465, -18.0067),  # El Hierro
            "ES704": (28.4059, -14.0365),  # Fuerteventura
            "ES705": (27.9544, -15.5935),  # Gran Canaria
            "ES706": (28.1174, -17.2327),  # La Gomera
            "ES707": (28.6900, -17.8580),  # La Palma
            "ES708": (29.0366, -13.6364),  # Lanzarote
            "ES709": (28.2908, -16.5568),  # Tenerife
        },
    },
}


def cluster_land_parts(parts, islands: dict[str, tuple[float, float]]) -> dict[str, "shapely.geometry.base.BaseGeometry"]:
    # Same nearest-reference-point clustering as GISCO-part decomposition
    # used before, just applied to land-layer parts. A part with no land at
    # all near it (shouldn't happen for a real island already inside the
    # cluster's own bounding box) simply doesn't get assigned to anything.
    grouped: dict[str, list] = {name: [] for name in islands}
    for part in parts:
        c = part.centroid
        nearest = min(islands, key=lambda name: (islands[name][0] - c.y) ** 2 + (islands[name][1] - c.x) ** 2)
        grouped[nearest].append(part)
    return {name: unary_union(pieces) for name, pieces in grouped.items() if pieces}


def build_island_land_shapes(land_path: Path) -> dict[str, "shapely.geometry.base.BaseGeometry"]:
    land = shapely.geometry.shape(json.loads(land_path.read_text(encoding="utf-8"))["features"][0]["geometry"])
    all_parts = list(land.geoms) if land.geom_type == "MultiPolygon" else [land]

    shapes: dict[str, "shapely.geometry.base.BaseGeometry"] = {}
    for cluster in ARCHIPELAGO_LAND_CLUSTERS.values():
        west, south, east, north = cluster["bbox"]
        parts_in_bbox = [p for p in all_parts if west <= p.centroid.x <= east and south <= p.centroid.y <= north]
        shapes.update(cluster_land_parts(parts_in_bbox, cluster["islands"]))
    return shapes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nuts3-path", type=Path, required=True, help="Downloaded NUTS3 GeoJSON (see module docstring).")
    parser.add_argument(
        "--land-path", type=Path, default=LAND_PATH,
        help=f"OSM-derived land polygon to source island shapes from (default: {LAND_PATH}, "
        "already built by scripts/build_land_polygons.py + clip_land_to_countries.py).",
    )
    return parser.parse_args()


def region_key(nuts_id: str, island_name: str | None = None) -> str:
    if island_name is None:
        return nuts_id
    return f"{nuts_id}-{island_name.upper().replace(' ', '_')}"


def round_coords(obj, ndigits: int = COORD_DECIMALS):
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, (list, tuple)):
        return [round_coords(x, ndigits) for x in obj]
    return obj


def simplify_for_web(geom):
    simplified = geom.simplify(SIMPLIFY_TOLERANCE_DEG, preserve_topology=True)
    mapped = shapely.geometry.mapping(simplified)
    return {**mapped, "coordinates": round_coords(mapped["coordinates"])}


def main() -> None:
    args = parse_args()
    geo = json.loads(args.nuts3_path.read_text(encoding="utf-8"))
    island_land_shapes = build_island_land_shapes(args.land_path)

    features = []
    unmatched_islands = []
    for f in geo["features"]:
        p = f["properties"]
        if p["CNTR_CODE"] not in ("PT", "ES"):
            continue
        nuts_id = p["NUTS_ID"]
        geom = shapely.geometry.shape(f["geometry"])

        if nuts_id in ISLAND_DECOMPOSITIONS:
            # Shape comes from island_land_shapes (keyed by display name),
            # not from decomposing this unit's own GISCO geometry -- GISCO
            # here is consulted only for which named islands exist under
            # this NUTS3 code at all (its keys), not for their shapes.
            for island_name in ISLAND_DECOMPOSITIONS[nuts_id]:
                island_geom = island_land_shapes.get(island_name)
                if island_geom is None:
                    unmatched_islands.append(island_name)
                    continue
                native = ISLAND_NATIVE_NAMES[island_name]
                features.append({
                    "type": "Feature",
                    "properties": {
                        "region_key": region_key(nuts_id, island_name),
                        "name_pt": native, "name_es": native, "name_en": native,
                        "kind": "island",
                        "source_nuts_id": nuts_id,
                    },
                    "geometry": simplify_for_web(drop_generalization_slivers(island_geom)),
                })
        else:
            name = p["NUTS_NAME"]
            kind = "island" if nuts_id in ISLAND_KIND_NUTS_IDS else "district_province"
            # Standalone island NUTS3 units (Mallorca, Menorca, the 7 Canary
            # Islands) also get their shape from island_land_shapes, keyed
            # by NUTS_ID this time (see ARCHIPELAGO_LAND_CLUSTERS) rather
            # than a display name -- everything else (mainland
            # districts/provinces) keeps GISCO's own geometry unchanged.
            island_geom = island_land_shapes.get(nuts_id) if kind == "island" else None
            if kind == "island" and island_geom is None:
                unmatched_islands.append(nuts_id)
            rendered_geom = island_geom if island_geom is not None else geom
            features.append({
                "type": "Feature",
                "properties": {
                    "region_key": region_key(nuts_id),
                    "name_pt": name, "name_es": name, "name_en": name,
                    "kind": kind,
                    "source_nuts_id": nuts_id,
                },
                "geometry": simplify_for_web(drop_generalization_slivers(rendered_geom)),
            })

    if unmatched_islands:
        # Fail loudly rather than silently falling back to GISCO's own
        # (misaligned) shape for just some islands -- every reference point
        # in ARCHIPELAGO_LAND_CLUSTERS is a real, present-day island, so a
        # miss here means either the bbox needs widening or iberia.geojson
        # doesn't cover that location, both worth knowing about immediately.
        raise SystemExit(f"No land shape found for: {unmatched_islands} -- check ARCHIPELAGO_LAND_CLUSTERS' bboxes/points")

    out = {"type": "FeatureCollection", "features": features}
    OUTPUT_PATH.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")

    n_islands = sum(1 for f in features if f["properties"]["kind"] == "island")
    n_districts = len(features) - n_islands
    print(f"Wrote {OUTPUT_PATH}: {len(features)} regions ({n_districts} districts/provinces, {n_islands} islands)")
    print(f"File size: {OUTPUT_PATH.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
