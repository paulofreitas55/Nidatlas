#!/usr/bin/env python
"""Query layer for the public atlas, over data/nidatlas.db. No web framework."""

import re
import sqlite3
from pathlib import Path

DB_PATH = Path("data") / "nidatlas.db"

MGRS_PREFIX_RE = re.compile(r"^[0-9A-Z]*$")


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _glob_prefix_pattern(mgrs_prefix: str) -> str:
    # GLOB (not LIKE) so SQLite can use the mgrs_cell-leading index as a real
    # range scan: LIKE is case-insensitive by default, which makes the planner
    # unable to trust a BINARY-collated index for it, so it falls back to a
    # full index scan. GLOB is always case-sensitive, which is exactly right
    # here since MGRS cell codes are always uppercase, and has no escape
    # syntax, so the prefix is restricted to MGRS's own alphabet instead.
    if not MGRS_PREFIX_RE.match(mgrs_prefix):
        raise ValueError(f"invalid mgrs_prefix: {mgrs_prefix!r} (expected digits/uppercase letters)")
    return mgrs_prefix + "*"


def all_species(conn: sqlite3.Connection) -> list[dict]:
    # Ordered by order/family/gbif_name so the frontend can group into
    # sections just by walking the list, with no client-side sort needed.
    # gbif_name is UNIQUE, so this sort key has no ties -- dex_number is a
    # stable 1..584 sequence over that same deterministic order.
    rows = conn.execute(
        """
        SELECT id, gbif_name, bioclip_name, family, "order", total_occurrences,
               common_name_pt, common_name_es, common_name_en,
               image_url, image_source, image_license, image_attribution, image_source_url,
               ROW_NUMBER() OVER (ORDER BY "order", family, gbif_name) AS dex_number
        FROM species
        ORDER BY "order", family, gbif_name
        """
    ).fetchall()
    return [
        {
            "id": sid,
            "gbif_name": gbif_name,
            "bioclip_name": bioclip_name,
            "family": family,
            "order": order,
            "total_occurrences": total_occurrences,
            "common_name_pt": common_name_pt,
            "common_name_es": common_name_es,
            "common_name_en": common_name_en,
            "image_url": image_url,
            "image_source": image_source,
            "image_license": image_license,
            "image_attribution": image_attribution,
            "image_source_url": image_source_url,
            "dex_number": dex_number,
        }
        for sid, gbif_name, bioclip_name, family, order, total_occurrences,
            common_name_pt, common_name_es, common_name_en,
            image_url, image_source, image_license, image_attribution, image_source_url, dex_number in rows
    ]


def species_ranking(conn: sqlite3.Connection) -> list[dict]:
    # Same RANK() OVER (ORDER BY total_occurrences DESC) as species_profile's
    # own global_rank -- so a species' position here always matches the "X of
    # 584" figure shown on its own page. RANK() (not ROW_NUMBER()) matters
    # specifically at the low end: several species are tied on a handful of
    # occurrences, and giving them the same rank number is the honest
    # representation -- a plain row-position count would imply a false
    # distinction between species with identical totals. gbif_name as the
    # secondary sort key only stabilises iteration order among ties (for
    # reproducible responses/tests); it plays no part in the rank itself.
    rows = conn.execute(
        """
        SELECT id, gbif_name, total_occurrences,
               common_name_pt, common_name_es, common_name_en,
               image_url, image_source, image_license, image_attribution, image_source_url,
               RANK() OVER (ORDER BY total_occurrences DESC) AS rank
        FROM species
        ORDER BY total_occurrences DESC, gbif_name
        """
    ).fetchall()
    return [
        {
            "id": sid,
            "gbif_name": gbif_name,
            "total_occurrences": total_occurrences,
            "common_name_pt": common_name_pt,
            "common_name_es": common_name_es,
            "common_name_en": common_name_en,
            "image_url": image_url,
            "image_source": image_source,
            "image_license": image_license,
            "image_attribution": image_attribution,
            "image_source_url": image_source_url,
            "rank": rank,
        }
        for sid, gbif_name, total_occurrences, common_name_pt, common_name_es, common_name_en,
            image_url, image_source, image_license, image_attribution, image_source_url, rank in rows
    ]


def species_profile(conn: sqlite3.Connection, species_id: int) -> dict:
    # Same dex_number definition (order/family/gbif_name, gbif_name UNIQUE so no
    # ties) as all_species(), so numbering matches between the atlas and this page.
    row = conn.execute(
        """
        WITH numbered AS (
            SELECT id, gbif_name, bioclip_name, genus, family, "order", total_occurrences,
                   common_name_pt, common_name_es, common_name_en,
                   image_url, image_source, image_license, image_attribution, image_source_url,
                   ROW_NUMBER() OVER (ORDER BY "order", family, gbif_name) AS dex_number
            FROM species
        )
        SELECT id, gbif_name, bioclip_name, genus, family, "order", total_occurrences,
               common_name_pt, common_name_es, common_name_en,
               image_url, image_source, image_license, image_attribution, image_source_url, dex_number
        FROM numbered WHERE id = ?
        """,
        (species_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no species with id {species_id}")
    (sid, gbif_name, bioclip_name, genus, family, order, total_occurrences,
        common_name_pt, common_name_es, common_name_en,
        image_url, image_source, image_license, image_attribution, image_source_url, dex_number) = row

    rank, percentile, total = conn.execute(
        """
        WITH ranked AS (
            SELECT id,
                   RANK() OVER (ORDER BY total_occurrences DESC) AS rank,
                   PERCENT_RANK() OVER (ORDER BY total_occurrences) AS percentile,
                   COUNT(*) OVER () AS total
            FROM species
        )
        SELECT rank, percentile, total FROM ranked WHERE id = ?
        """,
        (species_id,),
    ).fetchone()

    month_rows = conn.execute(
        "SELECT month, occurrences FROM species_month WHERE species_id = ?",
        (species_id,),
    ).fetchall()
    occurrences_by_month = {month: occ for month, occ in month_rows}
    annual_total = sum(occurrences_by_month.values())
    monthly_profile = [
        {
            "month": month,
            "occurrences": occurrences_by_month.get(month, 0),
            "share": (occurrences_by_month.get(month, 0) / annual_total) if annual_total else 0.0,
        }
        for month in range(1, 13)
    ]

    top_cells = [
        {"mgrs_cell": cell, "occurrences": occ, "centroid_lat": lat, "centroid_lon": lon}
        for cell, occ, lat, lon in conn.execute(
            """
            SELECT sc.mgrs_cell, sc.occurrences, gc.centroid_lat, gc.centroid_lon
            FROM species_cell sc
            JOIN grid_cells gc ON gc.mgrs_cell = sc.mgrs_cell
            WHERE sc.species_id = ?
            ORDER BY sc.occurrences DESC
            LIMIT 20
            """,
            (species_id,),
        ).fetchall()
    ]

    return {
        "id": sid,
        "gbif_name": gbif_name,
        "bioclip_name": bioclip_name,
        "genus": genus,
        "family": family,
        "order": order,
        "total_occurrences": total_occurrences,
        "common_name_pt": common_name_pt,
        "common_name_es": common_name_es,
        "common_name_en": common_name_en,
        "image_url": image_url,
        "image_source": image_source,
        "image_license": image_license,
        "image_attribution": image_attribution,
        "image_source_url": image_source_url,
        "dex_number": dex_number,
        "global_rank": {"rank": rank, "total": total, "percentile": round(percentile * 100, 1)},
        "monthly_profile": monthly_profile,
        "top_cells": top_cells,
    }


def species_cells(conn: sqlite3.Connection, species_id: int, month: int | None = None) -> list[dict]:
    exists = conn.execute("SELECT 1 FROM species WHERE id = ?", (species_id,)).fetchone()
    if exists is None:
        raise ValueError(f"no species with id {species_id}")

    if month is None:
        rows = conn.execute(
            """
            SELECT sc.mgrs_cell, gc.centroid_lat, gc.centroid_lon, sc.occurrences,
                   CAST(sc.occurrences AS REAL) / sc.family_occurrences AS share
            FROM species_cell sc
            JOIN grid_cells gc ON gc.mgrs_cell = sc.mgrs_cell
            WHERE sc.species_id = ?
            ORDER BY sc.occurrences DESC
            """,
            (species_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT scm.mgrs_cell, gc.centroid_lat, gc.centroid_lon, scm.occurrences,
                   CAST(scm.occurrences AS REAL) / scm.family_occurrences AS share
            FROM species_cell_month scm
            JOIN grid_cells gc ON gc.mgrs_cell = scm.mgrs_cell
            WHERE scm.species_id = ? AND scm.month = ?
            ORDER BY scm.occurrences DESC
            """,
            (species_id, month),
        ).fetchall()

    return [
        {"mgrs_cell": cell, "centroid_lat": lat, "centroid_lon": lon, "occurrences": occ, "share": share}
        for cell, lat, lon, occ, share in rows
    ]


def cell_summary(conn: sqlite3.Connection, mgrs_prefix: str) -> dict:
    pattern = _glob_prefix_pattern(mgrs_prefix)

    cell_count = conn.execute(
        "SELECT COUNT(*) FROM grid_cells WHERE mgrs_cell GLOB ?", (pattern,)
    ).fetchone()[0]

    # Sum occurrences and family_occurrences across every matching cell BEFORE
    # dividing, not average per-cell ratios -- a per-cell average would let a
    # single near-empty cell (e.g. 1 occurrence out of a family_occurrences of
    # 1) dominate with a spurious 100% share. share is kept as a
    # sampling-effort normaliser (see _build_species_rows) but no longer
    # drives the ranking -- see _rank_by_concentration.
    rows = conn.execute(
        """
        SELECT s.id, s.gbif_name, s.bioclip_name, s.family,
               SUM(sc.occurrences) AS occurrences,
               SUM(sc.family_occurrences) AS family_occurrences,
               CAST(SUM(sc.occurrences) AS REAL) / SUM(sc.family_occurrences) AS share
        FROM species_cell sc
        JOIN species s ON s.id = sc.species_id
        WHERE sc.mgrs_cell GLOB ?
        GROUP BY sc.species_id
        """,
        (pattern,),
    ).fetchall()

    global_by_species, global_total = _global_occurrences_by_species(conn, month=None)
    species_rows = _rank_by_concentration(rows, global_by_species, global_total)
    return _species_ranking_result(mgrs_prefix, cell_count, species_rows)


def cell_monthly(conn: sqlite3.Connection, mgrs_prefix: str, month: int) -> dict:
    pattern = _glob_prefix_pattern(mgrs_prefix)

    cell_count = conn.execute(
        "SELECT COUNT(*) FROM grid_cells WHERE mgrs_cell GLOB ?", (pattern,)
    ).fetchone()[0]

    # Same summed-then-divided totals as cell_summary, restricted to one month via species_cell_month.
    rows = conn.execute(
        """
        SELECT s.id, s.gbif_name, s.bioclip_name, s.family,
               SUM(scm.occurrences) AS occurrences,
               SUM(scm.family_occurrences) AS family_occurrences,
               CAST(SUM(scm.occurrences) AS REAL) / SUM(scm.family_occurrences) AS share
        FROM species_cell_month scm
        JOIN species s ON s.id = scm.species_id
        WHERE scm.mgrs_cell GLOB ? AND scm.month = ?
        GROUP BY scm.species_id
        """,
        (pattern, month),
    ).fetchall()

    global_by_species, global_total = _global_occurrences_by_species(conn, month=month)
    species_rows = _rank_by_concentration(rows, global_by_species, global_total)
    result = _species_ranking_result(mgrs_prefix, cell_count, species_rows)
    result["month"] = month
    return result


def list_regions(conn: sqlite3.Connection) -> list[dict]:
    # region_key is included so the frontend can join this list against
    # static/regions.geojson's own region_key property (the geometry's only
    # shared identifier with the DB) to look up each polygon's numeric id.
    # total_occurrences is regions.total_occurrences, precomputed by
    # scripts/assign_regions.py -- not summed here, since the live join
    # (species_cell through grid_cells) measured ~3.4s against the full
    # table, far too slow to run on every request this list-heavy page makes.
    #
    # "OFFSHORE" (kind='fallback', now user-facing as "Alto-mar"/"Open sea")
    # is included and just sorts last -- it's a real, browsable region on the
    # region map (its cells are shown directly rather than a polygon, since
    # it has none), not a hidden implementation detail the way it was before
    # the region map existed.
    rows = conn.execute(
        """
        SELECT r.id, r.region_key, r.name_pt, r.name_es, r.name_en, r.kind,
               r.total_occurrences, COUNT(gc.mgrs_cell) AS cell_count
        FROM regions r
        LEFT JOIN grid_cells gc ON gc.region_id = r.id
        GROUP BY r.id
        ORDER BY CASE r.kind
                     WHEN 'district_province' THEN 0
                     WHEN 'island' THEN 1
                     ELSE 2
                 END, r.name_en
        """
    ).fetchall()
    return [
        {
            "id": rid,
            "region_key": region_key,
            "name_pt": name_pt,
            "name_es": name_es,
            "name_en": name_en,
            "kind": kind,
            "total_occurrences": total_occurrences,
            "cell_count": cell_count,
        }
        for rid, region_key, name_pt, name_es, name_en, kind, total_occurrences, cell_count in rows
    ]


def region_cells(conn: sqlite3.Connection, region_id: int) -> list[dict]:
    # Per-cell totals (summed across every species) for one region -- used to
    # render the offshore/"Alto-mar" fallback on the region map, which has no
    # polygon of its own and so is drawn as its individual grid cells
    # instead, the same way a species' distribution is drawn on the species
    # map. Works for any region_id, not just the fallback one, in case a
    # future view wants a region's own cells rather than its polygon fill.
    exists = conn.execute("SELECT 1 FROM regions WHERE id = ?", (region_id,)).fetchone()
    if exists is None:
        raise ValueError(f"no region with id {region_id}")

    rows = conn.execute(
        """
        SELECT gc.mgrs_cell, gc.centroid_lat, gc.centroid_lon, SUM(sc.occurrences) AS occurrences
        FROM grid_cells gc
        JOIN species_cell sc ON sc.mgrs_cell = gc.mgrs_cell
        WHERE gc.region_id = ?
        GROUP BY gc.mgrs_cell
        ORDER BY occurrences DESC
        """,
        (region_id,),
    ).fetchall()
    return [
        {"mgrs_cell": cell, "centroid_lat": lat, "centroid_lon": lon, "occurrences": occ}
        for cell, lat, lon, occ in rows
    ]


def region_summary(conn: sqlite3.Connection, region_id: int, month: int | None = None) -> dict:
    region = conn.execute(
        "SELECT id, name_pt, name_es, name_en, kind FROM regions WHERE id = ?", (region_id,)
    ).fetchone()
    if region is None:
        raise ValueError(f"no region with id {region_id}")
    rid, name_pt, name_es, name_en, kind = region

    cell_count = conn.execute(
        "SELECT COUNT(*) FROM grid_cells WHERE region_id = ?", (region_id,)
    ).fetchone()[0]

    # Same summed-then-divided totals as cell_summary/cell_monthly (see that
    # function's comment for why sum-then-divide, not average-per-cell),
    # joined through grid_cells.region_id instead of filtering
    # species_cell(_month) by an mgrs_cell prefix.
    if month is None:
        rows = conn.execute(
            """
            SELECT s.id, s.gbif_name, s.bioclip_name, s.family,
                   SUM(sc.occurrences) AS occurrences,
                   SUM(sc.family_occurrences) AS family_occurrences,
                   CAST(SUM(sc.occurrences) AS REAL) / SUM(sc.family_occurrences) AS share
            FROM species_cell sc
            JOIN grid_cells gc ON gc.mgrs_cell = sc.mgrs_cell
            JOIN species s ON s.id = sc.species_id
            WHERE gc.region_id = ?
            GROUP BY sc.species_id
            """,
            (region_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT s.id, s.gbif_name, s.bioclip_name, s.family,
                   SUM(scm.occurrences) AS occurrences,
                   SUM(scm.family_occurrences) AS family_occurrences,
                   CAST(SUM(scm.occurrences) AS REAL) / SUM(scm.family_occurrences) AS share
            FROM species_cell_month scm
            JOIN grid_cells gc ON gc.mgrs_cell = scm.mgrs_cell
            JOIN species s ON s.id = scm.species_id
            WHERE gc.region_id = ? AND scm.month = ?
            GROUP BY scm.species_id
            """,
            (region_id, month),
        ).fetchall()

    global_by_species, global_total = _global_occurrences_by_species(conn, month=month)
    species_rows = _rank_by_concentration(rows, global_by_species, global_total)
    result = {
        "region_id": rid,
        "name_pt": name_pt,
        "name_es": name_es,
        "name_en": name_en,
        "kind": kind,
        "cell_count": cell_count,
        **_ranking_totals(species_rows),
    }
    if month is not None:
        result["month"] = month
    return result


def _build_species_rows(rows: list[tuple]) -> list[dict]:
    return [
        {
            "species_id": sid,
            "gbif_name": gbif_name,
            "bioclip_name": bioclip_name,
            "family": family,
            "occurrences": occurrences,
            "family_occurrences": family_occurrences,
            # share-of-family: kept as a sampling-effort normaliser (how much
            # of this family's activity in-scope this species accounts for),
            # its original purpose -- it no longer drives top/bottom ranking
            # (see _rank_by_concentration for why: a species that is the sole
            # member of a small family always scored share == 1.0 in every
            # region it appeared in at all, which made "most characteristic"
            # nearly identical everywhere).
            "share": share,
        }
        for sid, gbif_name, bioclip_name, family, occurrences, family_occurrences, share in rows
    ]


def _global_occurrences_by_species(conn: sqlite3.Connection, month: int | None) -> tuple[dict[int, int], int]:
    # Per-species and whole-Iberia occurrence totals, used as the denominator
    # for concentration's global_share (see _rank_by_concentration). Restricted
    # to `month` when a month filter is active, so a species that migrates
    # through everywhere in month M isn't read as "concentrated" in every
    # region that has it that month -- global_share is computed over the same
    # period as regional_share, not always the annual total, so seasonality
    # and genuine geographic concentration don't get conflated.
    if month is None:
        rows = conn.execute("SELECT id, total_occurrences FROM species").fetchall()
    else:
        rows = conn.execute(
            "SELECT species_id, occurrences FROM species_month WHERE month = ?", (month,)
        ).fetchall()
    by_species = dict(rows)
    return by_species, sum(by_species.values())


def _rank_by_concentration(
    rows: list[tuple], global_by_species: dict[int, int], global_total: int
) -> list[dict]:
    # concentration = regional_share / global_share, where regional_share is
    # a species' share of all occurrences in the current scope (a cell
    # prefix or a region, optionally one month) and global_share is that
    # species' share of all occurrences across the whole Iberia dataset (see
    # _global_occurrences_by_species). == 1 means exactly as prevalent here
    # as everywhere; > 1 means concentrated here; < 1 means underrepresented.
    #
    # This replaces share-of-family as the ranking driver (still computed
    # and kept on each row -- see _build_species_rows) because concentration
    # compares a species against its OWN distribution instead of its
    # family's, so the ranking actually differentiates regions rather than
    # surfacing the same monotypic-family species as "most characteristic"
    # everywhere.
    #
    species_rows = _build_species_rows(rows)
    regional_total = sum(r["occurrences"] for r in species_rows)
    for r in species_rows:
        regional_share = (r["occurrences"] / regional_total) if regional_total else 0.0
        global_occurrences = global_by_species.get(r["species_id"], 0)
        global_share = (global_occurrences / global_total) if global_total else 0.0
        r["concentration"] = (regional_share / global_share) if global_share else 0.0
    # Concentration DESC is the primary key; occurrences DESC then gbif_name
    # break ties deterministically (a handful of species can land on exactly
    # the same ratio, e.g. two species confined to a single shared cell).
    species_rows.sort(key=lambda r: (-r["concentration"], -r["occurrences"], r["gbif_name"]))
    return species_rows


# Minimum in-scope occurrences for a species to be eligible for the
# top/bottom-15 ranking lists (not a data-inclusion filter: total_occurrences
# and distinct_species below still count every species, this only gates list
# membership). Without it, a single-record vagrant with a huge global
# population reads as maximally "underrepresented" (concentration near
# zero), swamping the bottom list with statistical noise from a sample size
# of 1-2 rather than species genuinely uncommon in the region relative to
# their own normal range. Checked directly against the smallest regions in
# the dataset (Corvo: 177 species, 129 still clear this bar; Melilla: 144
# species, 82 clear it) to confirm even a 3-cell region has comfortably more
# than 15 eligible species on each side, so this never starves either list.
MIN_LIST_OCCURRENCES = 5


def _ranking_totals(species_rows: list[dict]) -> dict:
    # Shared by both cell- and region-level rankings: top/bottom 15 by
    # concentration (species_rows must already be sorted descending by it --
    # see _rank_by_concentration), plus the totals they're computed from.
    # Kept separate from the caller-specific identifying fields (mgrs_prefix
    # vs region_id/name) so cell_summary/cell_monthly and region_summary can
    # each wrap this with their own top-level shape instead of one function
    # guessing at both.
    #
    # total_occurrences/distinct_species intentionally come from the FULL,
    # unfiltered species_rows (every species genuinely present in-scope);
    # MIN_LIST_OCCURRENCES only gates which of them are eligible to appear
    # in top_species/bottom_species, applied to both ends symmetrically --
    # the same small-sample noise that makes a 1-occurrence vagrant look
    # maximally underrepresented can just as easily make one look spuriously
    # "characteristic" if its global total also happens to be tiny.
    eligible = [r for r in species_rows if r["occurrences"] >= MIN_LIST_OCCURRENCES]
    return {
        "total_occurrences": sum(r["occurrences"] for r in species_rows),
        "distinct_species": len(species_rows),
        "top_species": eligible[:15],
        "bottom_species": list(reversed(eligible[-15:])),
    }


def _species_ranking_result(mgrs_prefix: str, cell_count: int, species_rows: list[dict]) -> dict:
    return {
        "mgrs_prefix": mgrs_prefix,
        "cell_count": cell_count,
        **_ranking_totals(species_rows),
    }


def search_species(conn: sqlite3.Connection, text: str) -> list[dict]:
    # NULL LIKE pattern evaluates to NULL (falsy), so species missing a
    # vernacular name in a given language simply don't match on it -- no
    # special-casing needed for the columns fetch_vernacular_names.py leaves NULL.
    pattern = "%" + _escape_like(text) + "%"
    rows = conn.execute(
        """
        SELECT id, gbif_name, bioclip_name, common_name_pt, common_name_es, common_name_en
        FROM species
        WHERE gbif_name LIKE ? ESCAPE '\\'
           OR bioclip_name LIKE ? ESCAPE '\\'
           OR common_name_pt LIKE ? ESCAPE '\\'
           OR common_name_es LIKE ? ESCAPE '\\'
           OR common_name_en LIKE ? ESCAPE '\\'
        ORDER BY gbif_name
        """,
        (pattern, pattern, pattern, pattern, pattern),
    ).fetchall()
    return [
        {
            "id": sid,
            "gbif_name": g,
            "bioclip_name": b,
            "common_name_pt": pt,
            "common_name_es": es,
            "common_name_en": en,
        }
        for sid, g, b, pt, es, en in rows
    ]


# --- Phylogeny (data/nidatlas.db's phylo_nodes/phylo_closure -- see
# scripts/fetch_phylogeny.py and scripts/build_phylogeny_db.py for how these
# are populated, and that second script's module docstring for why an
# adjacency list + closure table was chosen over nested sets or a
# materialized path for exactly these four query patterns). ---
#
# Not every one of the 584 species has a phylo_nodes row: a handful resolve
# to a valid Open Tree taxon that isn't sampled by any input phylogeny, so
# they have no placement in this particular synthetic tree at all (see
# fetch_phylogeny.py's report for the current list). That's a real,
# disclosed data gap, not a bug -- these functions treat "species exists but
# has no tree placement" as a valid, non-error outcome (empty list / a
# clearly-labeled ValueError, never a silent guess at where it might go).


def phylo_species_node_id(conn: sqlite3.Connection, species_id: int) -> int | None:
    """The species' own tip node id, or None if it has no placement in this
    tree (see module note above) -- public since api.py's /relatives route
    needs it directly to build its response envelope, not just the other
    phylo_* functions that use it internally."""
    exists = conn.execute("SELECT 1 FROM species WHERE id = ?", (species_id,)).fetchone()
    if exists is None:
        raise ValueError(f"no species with id {species_id}")
    row = conn.execute("SELECT id FROM phylo_nodes WHERE species_id = ?", (species_id,)).fetchone()
    return row[0] if row else None


def phylo_tree_root(conn: sqlite3.Connection) -> dict:
    # The root's own id isn't stable across a rebuild (see CLAUDE.md), so a
    # caller (the tree view) must look it up fresh via this rather than
    # ever hardcoding one.
    nid, name, label = conn.execute(
        "SELECT id, name, ott_node_label FROM phylo_nodes WHERE parent_id IS NULL"
    ).fetchone()
    return {"node_id": nid, "name": name, "ott_node_label": label}


def phylo_closest_relatives(conn: sqlite3.Connection, species_id: int, limit: int = 10) -> list[dict]:
    # "Closest" = fewest edges apart in the tree topology (no branch lengths
    # exist to weigh this by evolutionary distance -- see the Newick this
    # was parsed from). For every other tip, the shortest path to it runs
    # through their most recent common ancestor with the query species,
    # found here without a separate MRCA step: `ancestors` lists every one
    # of the query species' own ancestors with its distance to it
    # (steps_up); joining each to ALL of that ancestor's descendants and
    # taking MIN(steps_up + c.depth) per candidate automatically selects the
    # true MRCA for that candidate, since routing through any shallower
    # shared ancestor only ever adds distance, never removes it.
    node_id = phylo_species_node_id(conn, species_id)
    if node_id is None:
        return []

    rows = conn.execute(
        """
        WITH ancestors AS (
            SELECT ancestor_id, depth AS steps_up FROM phylo_closure WHERE descendant_id = ?
        ),
        candidates AS (
            SELECT c.descendant_id AS relative_node_id, MIN(a.steps_up + c.depth) AS distance
            FROM ancestors a
            JOIN phylo_closure c ON c.ancestor_id = a.ancestor_id
            WHERE c.descendant_id != ?
            GROUP BY c.descendant_id
        )
        SELECT s.id, s.gbif_name, s.common_name_pt, s.common_name_es, s.common_name_en,
               candidates.distance, n.id
        FROM candidates
        JOIN phylo_nodes n ON n.id = candidates.relative_node_id
        JOIN species s ON s.id = n.species_id
        WHERE n.is_tip = 1 AND n.species_id IS NOT NULL
        ORDER BY candidates.distance ASC, s.gbif_name ASC
        LIMIT ?
        """,
        (node_id, node_id, limit),
    ).fetchall()
    return [
        {
            "species_id": sid, "gbif_name": g,
            "common_name_pt": pt, "common_name_es": es, "common_name_en": en,
            "distance": distance, "node_id": relative_node_id,
        }
        for sid, g, pt, es, en, distance, relative_node_id in rows
    ]


def phylo_mrca(conn: sqlite3.Connection, species_id_a: int, species_id_b: int) -> dict:
    # Most recent common ancestor = the deepest-from-root node that is an
    # ancestor of BOTH species (see build_phylogeny_db.py's module docstring
    # on why phylo_nodes.depth -- the node's OWN depth from root -- is what
    # this needs to sort by, not phylo_closure.depth, which only measures
    # distance to one side and would wrongly favor a shallow ancestor).
    node_a = phylo_species_node_id(conn, species_id_a)
    node_b = phylo_species_node_id(conn, species_id_b)
    if node_a is None:
        raise ValueError(f"species {species_id_a} has no placement in the phylogenetic tree")
    if node_b is None:
        raise ValueError(f"species {species_id_b} has no placement in the phylogenetic tree")

    row = conn.execute(
        """
        SELECT n.id, n.name, n.ott_node_label, n.rank, n.depth
        FROM phylo_closure ca
        JOIN phylo_closure cb ON ca.ancestor_id = cb.ancestor_id
        JOIN phylo_nodes n ON n.id = ca.ancestor_id
        WHERE ca.descendant_id = ? AND cb.descendant_id = ?
        ORDER BY n.depth DESC
        LIMIT 1
        """,
        (node_a, node_b),
    ).fetchone()
    nid, name, label, rank, depth = row
    return {"node_id": nid, "name": name, "ott_node_label": label, "rank": rank, "depth": depth}


def phylo_mrca_of_node_ids(conn: sqlite3.Connection, node_ids: list[int]) -> dict:
    # General N-way version of phylo_mrca, over already-known phylo_nodes
    # ids rather than species ids -- used by api.py to bound a species'
    # WHOLE shown neighbourhood (itself + every listed relative) in one
    # node, for fetching/linking that exact neighbourhood as a subtree.
    #
    # Taking the MRCA of just the species and its single FARTHEST-listed
    # relative is NOT a safe shortcut for this, and was a real bug caught
    # while building the frontend for this: "distance" in
    # phylo_closest_relatives is a raw node-hop count, not a branch-length
    # or any other ultrametric measure, and OToL's induced_subtree often
    # inserts long single-child synthesis chains (see
    # build_phylogeny_db.py's module docstring) that inflate hop-count on
    # one branch without inflating it on another. A relative with a
    # SMALLER hop-distance can therefore still sit via a shallower shared
    # ancestor than a "farther" one, i.e. OUTSIDE that farther relative's
    # MRCA with the species -- observed directly: for Turdus merula's top
    # 6 relatives, the MRCA with its farthest-ranked one (Turdus naumanni)
    # excluded two nearer-ranked ones (Turdus philomelos, Turdus
    # viscivorus) entirely. Only the true MRCA of the FULL set is
    # guaranteed to contain every member.
    distinct_ids = list(dict.fromkeys(node_ids))  # de-duplicate, preserve order (no set() -- irrelevant here, just clearer intent)
    placeholders = ",".join("?" * len(distinct_ids))
    row = conn.execute(
        f"""
        SELECT n.id, n.name, n.ott_node_label, n.rank, n.depth
        FROM phylo_closure c
        JOIN phylo_nodes n ON n.id = c.ancestor_id
        WHERE c.descendant_id IN ({placeholders})
        GROUP BY c.ancestor_id
        HAVING COUNT(DISTINCT c.descendant_id) = ?
        ORDER BY n.depth DESC
        LIMIT 1
        """,
        (*distinct_ids, len(distinct_ids)),
    ).fetchone()
    nid, name, label, rank, depth = row
    return {"node_id": nid, "name": name, "ott_node_label": label, "rank": rank, "depth": depth}


def phylo_descendant_species(conn: sqlite3.Connection, node_id: int) -> list[dict]:
    exists = conn.execute("SELECT 1 FROM phylo_nodes WHERE id = ?", (node_id,)).fetchone()
    if exists is None:
        raise ValueError(f"no phylo node with id {node_id}")

    rows = conn.execute(
        """
        SELECT s.id, s.gbif_name, s.common_name_pt, s.common_name_es, s.common_name_en
        FROM phylo_closure c
        JOIN phylo_nodes n ON n.id = c.descendant_id
        JOIN species s ON s.id = n.species_id
        WHERE c.ancestor_id = ? AND n.is_tip = 1 AND n.species_id IS NOT NULL
        ORDER BY s.gbif_name
        """,
        (node_id,),
    ).fetchall()
    return [
        {"species_id": sid, "gbif_name": g, "common_name_pt": pt, "common_name_es": es, "common_name_en": en}
        for sid, g, pt, es, en in rows
    ]


def phylo_subtree(conn: sqlite3.Connection, node_id: int) -> dict:
    # Flat list of every node in the subtree (root included, via
    # phylo_closure's own self-row at depth 0), each carrying its
    # parent_id -- enough for a caller to reconstruct the actual topology
    # for display without a second query per node.
    exists = conn.execute("SELECT 1 FROM phylo_nodes WHERE id = ?", (node_id,)).fetchone()
    if exists is None:
        raise ValueError(f"no phylo node with id {node_id}")

    # Vernacular names (not just gbif_name) are included here, not left for
    # the frontend to fetch separately, because tree.js needs to label every
    # tip with the active-language common name at render time -- fetching
    # 577 species profiles individually just to show the tree would be far
    # more expensive than joining them in once here.
    rows = conn.execute(
        """
        SELECT n.id, n.name, n.ott_node_label, n.ott_id, n.rank, n.parent_id, n.species_id, n.is_tip, n.depth,
               s.gbif_name, s.common_name_pt, s.common_name_es, s.common_name_en
        FROM phylo_closure c
        JOIN phylo_nodes n ON n.id = c.descendant_id
        LEFT JOIN species s ON s.id = n.species_id
        WHERE c.ancestor_id = ?
        ORDER BY n.depth, n.id
        """,
        (node_id,),
    ).fetchall()
    nodes = [
        {
            "id": nid, "name": name, "ott_node_label": label, "ott_id": ott_id, "rank": rank,
            "parent_id": parent_id, "species_id": species_id, "is_tip": bool(is_tip), "depth": depth,
            "gbif_name": gbif_name,
            "common_name_pt": pt, "common_name_es": es, "common_name_en": en,
        }
        for nid, name, label, ott_id, rank, parent_id, species_id, is_tip, depth, gbif_name, pt, es, en in rows
    ]
    return {"root_id": node_id, "node_count": len(nodes), "nodes": nodes}


def _print_species_profile(profile: dict) -> None:
    print(f"=== {profile['gbif_name']} ({profile['bioclip_name']}) ===")
    print(f"{profile['genus']} / {profile['family']} / {profile['order']}")
    print(f"Total occurrences: {profile['total_occurrences']:,}")
    rank = profile["global_rank"]
    print(f"Global rank: {rank['rank']} / 584 (percentile {rank['percentile']})")
    if profile["image_url"]:
        print(f"Photo: {profile['image_attribution']} [{profile['image_license']}] via {profile['image_source']}")
    else:
        print("Photo: none")
    print("Monthly profile (share of annual total):")
    for m in profile["monthly_profile"]:
        bar = "#" * round(m["share"] * 50)
        print(f"  {m['month']:>2}: {m['occurrences']:>6,}  {m['share']:>6.1%}  {bar}")
    print("Top cells:")
    for c in profile["top_cells"][:5]:
        print(f"  {c['mgrs_cell']}  {c['occurrences']:>6,}  ({c['centroid_lat']:.3f}, {c['centroid_lon']:.3f})")
    print(f"  ... ({len(profile['top_cells'])} total)")


def _print_cell_summary(summary: dict) -> None:
    label = f"prefix '{summary['mgrs_prefix']}'"
    if "month" in summary:
        label += f", month {summary['month']}"
    print(f"=== Cell summary: {label} ===")
    print(f"Cells: {summary['cell_count']:,}  Total occurrences: {summary['total_occurrences']:,}  "
          f"Distinct species: {summary['distinct_species']:,}")
    print("Top 15 by concentration (regional_share / global_share):")
    for r in summary["top_species"]:
        print(f"  {r['gbif_name']:<28} {r['family']:<16} {r['occurrences']:>7,}  "
              f"conc={r['concentration']:>7.2f}x  family_share={r['share']:>6.1%}")
    print("Bottom 15 by concentration (regional_share / global_share):")
    for r in summary["bottom_species"]:
        print(f"  {r['gbif_name']:<28} {r['family']:<16} {r['occurrences']:>7,}  "
              f"conc={r['concentration']:>7.2f}x  family_share={r['share']:>6.1%}")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)

    print(">>> search_species(conn, 'madeirensis')")
    results = search_species(conn, "madeirensis")
    for r in results:
        print(f"  id={r['id']:<4} {r['gbif_name']:<24} {r['bioclip_name']}")
    print()

    target_id = results[0]["id"]
    _print_species_profile(species_profile(conn, target_id))
    print()

    _print_cell_summary(cell_summary(conn, "28S"))
    print()

    print(">>> EXPLAIN QUERY PLAN for cell_monthly's species_cell_month scan")
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT species_id, occurrences FROM species_cell_month "
        "WHERE mgrs_cell GLOB '28S*' AND month = 1"
    ).fetchall()
    for row in plan:
        print("   ", row)
    print()

    _print_cell_summary(cell_monthly(conn, "28S", 1))
    print()
    _print_cell_summary(cell_monthly(conn, "28S", 5))

    conn.close()
