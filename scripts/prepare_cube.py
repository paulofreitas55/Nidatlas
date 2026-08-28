#!/usr/bin/env python
"""Filter and clean the GBIF occurrence cube for Iberian bird species."""

from pathlib import Path

import pandas as pd

DATA_DIR = Path("data")
CUBE_PATH = DATA_DIR / "0041616-260806074905277.csv"
SPECIES_LIST_PATH = DATA_DIR / "iberian_species.txt"
OUTPUT_PATH = DATA_DIR / "cube_clean.parquet"
MIN_YEAR = 1990
MAX_UNCERTAINTY_M = 10000

# category dtype for the repetitive taxonomic/spatial columns keeps a 12M-row
# cube in a few hundred MB instead of several GB of Python string objects.
DTYPES = {
    "kingdom": "category", "kingdomkey": "category",
    "phylum": "category", "phylumkey": "category",
    "class": "category", "classkey": "category",
    "order": "category", "orderkey": "category",
    "family": "category", "familykey": "category",
    "genus": "category", "genuskey": "category",
    "species": "category", "specieskey": "category",
    "yearmonth": "category", "mgrscellcode": "category",
    "familycount": "int32", "occurrences": "int32",
    "mincoordinateuncertaintyinmeters": "float32",
}


def load_gbif_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            names.add(line.split(",")[1])
    return names


def year_of(yearmonth: pd.Series) -> pd.Series:
    return yearmonth.astype(str).str.slice(0, 4).astype(int)


def main() -> None:
    df = pd.read_csv(CUBE_PATH, sep="\t", dtype=DTYPES)

    print("=== Structural report (raw cube) ===")
    print(df.dtypes)
    print(f"Rows: {len(df):,}")
    print(f"Distinct species: {df['species'].nunique():,}")
    print(f"Distinct grid cells: {df['mgrscellcode'].nunique():,}")
    years = year_of(df["yearmonth"])
    print(f"Year range: {years.min()}-{years.max()}")
    print(f"Total occurrence count: {df['occurrences'].sum():,}")

    iberian_names = load_gbif_names(SPECIES_LIST_PATH)
    rows_before, species_before = len(df), df["species"].nunique()
    df = df[df["species"].isin(iberian_names)]
    print(f"\nSpecies filter: dropped {rows_before - len(df):,} rows, "
          f"{species_before - df['species'].nunique():,} species not in {SPECIES_LIST_PATH}")

    rows_before = len(df)
    df = df[year_of(df["yearmonth"]) >= MIN_YEAR]
    print(f"Year filter: dropped {rows_before - len(df):,} rows with year < {MIN_YEAR}")

    # ">" is False for NaN, so this keeps null-uncertainty rows rather than dropping them.
    rows_before = len(df)
    df = df[~(df["mincoordinateuncertaintyinmeters"] > MAX_UNCERTAINTY_M)]
    print(f"Uncertainty filter: dropped {rows_before - len(df):,} rows with "
          f"mincoordinateuncertaintyinmeters > {MAX_UNCERTAINTY_M}")

    df.to_parquet(OUTPUT_PATH, index=False)

    print("\n=== Final summary ===")
    print(f"Rows: {len(df):,}")
    print(f"Species: {df['species'].nunique():,}")
    print(f"Cells: {df['mgrscellcode'].nunique():,}")
    print(f"Size on disk: {OUTPUT_PATH.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
