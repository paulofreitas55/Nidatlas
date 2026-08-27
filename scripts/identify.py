#!/usr/bin/env python
"""Identify the top-5 most likely bird species in a photo using BioCLIP 2."""

import argparse
import sys
import time
from pathlib import Path

from bioclip import Rank, TreeOfLifeClassifier


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Identify a bird species from a photo.")
    parser.add_argument("image_path", help="Path to the image file")
    parser.add_argument(
        "--device",
        choices=["cpu", "cuda"],
        default="cpu",
        help="Inference device (default: cpu, matches production target)",
    )
    return parser.parse_args()


def print_table(predictions: list[dict]) -> None:
    header = f"{'#':<3} {'Scientific name':<30} {'Common name':<25} {'Score':>8}"
    print(header)
    print("-" * len(header))
    for rank, prediction in enumerate(predictions, start=1):
        scientific_name = prediction.get("species", "?")
        common_name = prediction.get("common_name") or "-"
        score = prediction.get("score", 0.0)
        print(f"{rank:<3} {scientific_name:<30} {common_name:<25} {score:>8.4f}")


def main() -> None:
    args = parse_args()

    if not Path(args.image_path).is_file():
        sys.exit(f"Error: image not found: {args.image_path}")

    classifier = TreeOfLifeClassifier(device=args.device)

    start = time.perf_counter()
    predictions = classifier.predict(args.image_path, Rank.SPECIES, k=5)
    elapsed = time.perf_counter() - start

    print_table(predictions)
    print(f"\nInference time: {elapsed:.3f}s (device={args.device})")


if __name__ == "__main__":
    main()
