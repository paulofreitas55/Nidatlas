#!/usr/bin/env python
"""Query layer for the public atlas, over data/nidario.db. No web framework."""

import re
import sqlite3
from pathlib import Path

DB_PATH = Path("data") / "nidario.db"

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
            "dex_number": dex_number,
        }
        for sid, gbif_name, bioclip_name, family, order, total_occurrences,
            common_name_pt, common_name_es, common_name_en, dex_number in rows
    ]


def species_profile(conn: sqlite3.Connection, species_id: int) -> dict:
    row = conn.execute(
        "SELECT id, gbif_name, bioclip_name, genus, family, \"order\", total_occurrences "
        "FROM species WHERE id = ?",
        (species_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no species with id {species_id}")
    sid, gbif_name, bioclip_name, genus, family, order, total_occurrences = row

    rank, percentile = conn.execute(
        """
        WITH ranked AS (
            SELECT id,
                   RANK() OVER (ORDER BY total_occurrences DESC) AS rank,
                   PERCENT_RANK() OVER (ORDER BY total_occurrences) AS percentile
            FROM species
        )
        SELECT rank, percentile FROM ranked WHERE id = ?
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
        "global_rank": {"rank": rank, "percentile": round(percentile * 100, 1)},
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
    # 1) dominate the ranking with a spurious 100% share. Summing both sides
    # first answers "what share of all family activity in this whole prefix
    # does this species account for", which is the meaningful quantity.
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
        ORDER BY share DESC, occurrences DESC
        """,
        (pattern,),
    ).fetchall()

    return _species_ranking_result(mgrs_prefix, cell_count, rows)


def cell_monthly(conn: sqlite3.Connection, mgrs_prefix: str, month: int) -> dict:
    pattern = _glob_prefix_pattern(mgrs_prefix)

    cell_count = conn.execute(
        "SELECT COUNT(*) FROM grid_cells WHERE mgrs_cell GLOB ?", (pattern,)
    ).fetchone()[0]

    # Same summed-then-divided ranking as cell_summary, restricted to one month via species_cell_month.
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
        ORDER BY share DESC, occurrences DESC
        """,
        (pattern, month),
    ).fetchall()

    result = _species_ranking_result(mgrs_prefix, cell_count, rows)
    result["month"] = month
    return result


def _species_ranking_result(mgrs_prefix: str, cell_count: int, rows: list[tuple]) -> dict:
    species_rows = [
        {
            "species_id": sid,
            "gbif_name": gbif_name,
            "bioclip_name": bioclip_name,
            "family": family,
            "occurrences": occurrences,
            "family_occurrences": family_occurrences,
            "share": share,
        }
        for sid, gbif_name, bioclip_name, family, occurrences, family_occurrences, share in rows
    ]

    return {
        "mgrs_prefix": mgrs_prefix,
        "cell_count": cell_count,
        "total_occurrences": sum(r["occurrences"] for r in species_rows),
        "distinct_species": len(species_rows),
        "top_species": species_rows[:15],
        "bottom_species": list(reversed(species_rows[-15:])),
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


def _print_species_profile(profile: dict) -> None:
    print(f"=== {profile['gbif_name']} ({profile['bioclip_name']}) ===")
    print(f"{profile['genus']} / {profile['family']} / {profile['order']}")
    print(f"Total occurrences: {profile['total_occurrences']:,}")
    rank = profile["global_rank"]
    print(f"Global rank: {rank['rank']} / 584 (percentile {rank['percentile']})")
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
    print("Top 15 by share of family:")
    for r in summary["top_species"]:
        print(f"  {r['gbif_name']:<28} {r['family']:<16} {r['occurrences']:>7,} / "
              f"{r['family_occurrences']:>7,}  {r['share']:>6.1%}")
    print("Bottom 15 by share of family:")
    for r in summary["bottom_species"]:
        print(f"  {r['gbif_name']:<28} {r['family']:<16} {r['occurrences']:>7,} / "
              f"{r['family_occurrences']:>7,}  {r['share']:>6.1%}")


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
