#!/usr/bin/env python
"""Build a clean list of Iberian bird species from a GBIF species-list CSV."""

import argparse
from pathlib import Path

import pandas as pd
from bioclip import TreeOfLifeClassifier

DATA_DIR = Path("data")
OUTPUT_PATH = DATA_DIR / "iberian_species.txt"
SYNONYMS_PATH = DATA_DIR / "taxonomy_synonyms.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the clean Iberian species list from a GBIF CSV export."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Path to the GBIF species-list CSV (default: the only .csv found in data/)",
    )
    parser.add_argument(
        "--min-occurrences",
        type=int,
        default=50,
        help="Drop species with fewer than N total occurrences (default: 50)",
    )
    return parser.parse_args()


def resolve_input_path(explicit: Path | None) -> Path:
    if explicit:
        return explicit
    candidates = sorted(p for p in DATA_DIR.glob("*.csv") if p != SYNONYMS_PATH)
    if len(candidates) != 1:
        raise SystemExit(
            f"Expected exactly one CSV in {DATA_DIR}/, found {len(candidates)}. "
            "Use --input to specify one."
        )
    return candidates[0]


def load_synonyms() -> dict[str, str]:
    if not SYNONYMS_PATH.is_file():
        return {}
    syn_df = pd.read_csv(SYNONYMS_PATH)
    return dict(zip(syn_df["gbif_name"], syn_df["bioclip_name"]))


def main() -> None:
    args = parse_args()
    input_path = resolve_input_path(args.input)

    df = pd.read_csv(input_path, sep="\t")
    species_df = df[df["taxonRank"] == "SPECIES"]

    grouped = species_df.groupby("acceptedScientificName").agg(
        species=("species", "first"),
        total_occurrences=("numberOfOccurrences", "sum"),
    )
    filtered = grouped[grouped["total_occurrences"] >= args.min_occurrences]
    gbif_names = sorted(filtered["species"].unique())

    synonyms = load_synonyms()
    resolved_map: dict[str, str] = {}
    for gbif_name in gbif_names:
        bioclip_name = synonyms.get(gbif_name, gbif_name)
        resolved_map.setdefault(bioclip_name, gbif_name)
    resolved = sorted(resolved_map.items())

    classifier = TreeOfLifeClassifier(device="cpu")
    bioclip_vocab = set(classifier.get_label_data()["species"])

    unknown = [bioclip_name for bioclip_name, _ in resolved if bioclip_name not in bioclip_vocab]
    if unknown:
        print(f"Warning: {len(unknown)} species not recognized by BioCLIP, excluded from output:")
        for name in unknown:
            print(f"  - {name}")
        resolved = [pair for pair in resolved if pair[0] not in unknown]

    lines = [f"{bioclip_name},{gbif_name}" for bioclip_name, gbif_name in resolved]
    OUTPUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(resolved)} species to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
