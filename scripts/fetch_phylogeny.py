#!/usr/bin/env python
"""Resolve the 584 species' gbif_name values against the Open Tree of Life
(OToL) and fetch the induced subtree over the resolved taxa.

Two-stage API, both POST-only (the service rejects GET outright):
  1. TNRS match_names -- name string -> zero, one, or several candidate OTT
     taxa. Restricted to context_name="Birds" so a same-spelled non-bird
     genus never intrudes.
  2. tree_of_life/induced_subtree -- the minimal subtree connecting a set of
     OTT ids within OToL's synthetic tree. Internal nodes here are whatever
     OToL's synthesis happened to produce between the requested tips; most
     are unnamed (mrca of two studies' worth of disagreement), a few carry a
     real taxon name -- both are recorded as-is in Step 2, not invented.

A name is classified resolved only when TNRS returns EXACTLY one distinct
ott_id (deduplicated -- see classify_matches). Everything else (ambiguous:
>1 candidate; unmatched: 0 candidates) was reported to and decided by a
human before this script applied any correction -- see the two curated
files below, not a guess made in code:

- `data/taxonomy_synonyms.csv` (pre-existing, built for BioCLIP naming
  gaps) is tried first for any name TNRS can't match directly: OToL's older
  taxonomy lags recent GBIF splits in exactly the same way BioCLIP's
  vocabulary does, and 2 of the 3 originally-unmatched species (Astur
  gentilis, Botaurus sturmii) already had a working synonym recorded there.
- `data/ott_taxonomy_synonyms.csv` (gbif_name,ott_name) covers OToL-only
  naming gaps not already covered by the BioCLIP file (currently just
  Curruca balearica -> Sylvia balearica).
- `data/ott_ambiguous_resolutions.csv` (gbif_name,ott_id,note) records the
  specific candidate a human picked for each name that had genuine
  ambiguity (>1 distinct OTT taxon), with the reasoning kept alongside it.

Every raw API response is cached under data/ (see *_CACHE_PATH below) so a
rerun with the same species list makes zero network calls. Pass --force to
refetch (e.g. after the species list or either curated file changes).
"""

import argparse
import csv
import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "nidatlas.db"
TNRS_CACHE_PATH = DATA_DIR / "opentree_tnrs_raw.json"
TNRS_RETRY_CACHE_PATH = DATA_DIR / "opentree_tnrs_retry_raw.json"
SUBTREE_CACHE_PATH = DATA_DIR / "opentree_induced_subtree_raw.json"
ABOUT_CACHE_PATH = DATA_DIR / "opentree_about_raw.json"
RESOLUTIONS_PATH = DATA_DIR / "opentree_resolutions.json"

BIOCLIP_SYNONYMS_PATH = DATA_DIR / "taxonomy_synonyms.csv"
OTT_SYNONYMS_PATH = DATA_DIR / "ott_taxonomy_synonyms.csv"
AMBIGUOUS_OVERRIDES_PATH = DATA_DIR / "ott_ambiguous_resolutions.csv"

API_ROOT = "https://api.opentreeoflife.org/v3"
TNRS_URL = f"{API_ROOT}/tnrs/match_names"
SUBTREE_URL = f"{API_ROOT}/tree_of_life/induced_subtree"
ABOUT_URL = f"{API_ROOT}/tree_of_life/about"

# TNRS's own docs don't publish a hard per-call name limit, but batching
# keeps any one request small enough to retry cheaply on a transient
# network error without re-sending everything already resolved.
BATCH_SIZE = 200
USER_AGENT = "Nidatlas/1.0 (bird atlas project; phylogeny fetch script)"
REQUEST_DELAY_SECONDS = 0.5


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def load_species_rows(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    return conn.execute(
        "SELECT id, gbif_name, bioclip_name FROM species ORDER BY gbif_name"
    ).fetchall()


def load_csv_map(path: Path, key_col: str, val_col: str) -> dict[str, str]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {row[key_col]: row[val_col] for row in csv.DictReader(f)}


def load_ambiguous_overrides() -> dict[str, int]:
    if not AMBIGUOUS_OVERRIDES_PATH.is_file():
        return {}
    with AMBIGUOUS_OVERRIDES_PATH.open(encoding="utf-8", newline="") as f:
        return {row["gbif_name"]: int(row["ott_id"]) for row in csv.DictReader(f)}


def fetch_tnrs(names: list[str], cache_path: Path, force: bool) -> dict:
    if cache_path.is_file() and not force:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    if not names:
        return {"results": [], "unmatched_names": []}

    all_results: list[dict] = []
    unmatched_names: list[str] = []
    for i in range(0, len(names), BATCH_SIZE):
        batch = names[i:i + BATCH_SIZE]
        print(f"  TNRS batch {i // BATCH_SIZE + 1}: {len(batch)} names ...")
        data = _post_json(TNRS_URL, {
            "names": batch,
            "context_name": "Birds",
            "do_approximate_matching": True,
        })
        all_results.extend(data["results"])
        unmatched_names.extend(data.get("unmatched_names", []))
        if i + BATCH_SIZE < len(names):
            time.sleep(REQUEST_DELAY_SECONDS)

    combined = {"results": all_results, "unmatched_names": unmatched_names}
    cache_path.write_text(json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")
    return combined


def classify_matches(tnrs_response: dict) -> tuple[dict[str, dict], dict[str, list[dict]], list[str]]:
    """Returns (resolved: name -> taxon dict, ambiguous: name -> candidate list, unmatched: [name]).

    TNRS returns one "match" entry per (name, nomenclatural-path) hit, which
    can list the SAME ott_id twice for one name -- e.g. Phoeniconaias minor
    matches once directly and once via its own name appearing in its own
    synonym list. That's a quirk of the taxonomy, not real ambiguity, so
    candidates are deduplicated by ott_id before deciding resolved vs.
    ambiguous: only >1 DISTINCT ott_id counts as genuine ambiguity.
    """
    resolved: dict[str, dict] = {}
    ambiguous: dict[str, list[dict]] = {}
    unmatched: list[str] = []
    for result in tnrs_response["results"]:
        name = result["name"]
        taxa_by_ott_id = {m["taxon"]["ott_id"]: m["taxon"] for m in result["matches"]}
        if len(taxa_by_ott_id) == 0:
            unmatched.append(name)
        elif len(taxa_by_ott_id) == 1:
            resolved[name] = next(iter(taxa_by_ott_id.values()))
        else:
            ambiguous[name] = list(taxa_by_ott_id.values())
    return resolved, ambiguous, unmatched


def apply_ambiguous_overrides(
    ambiguous: dict[str, list[dict]], overrides: dict[str, int]
) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Splits `ambiguous` into (resolved via a human-picked ott_id, still unresolved)."""
    resolved: dict[str, dict] = {}
    still_ambiguous: dict[str, list[dict]] = {}
    for name, candidates in ambiguous.items():
        picked_ott_id = overrides.get(name)
        if picked_ott_id is None:
            still_ambiguous[name] = candidates
            continue
        match = next((c for c in candidates if c["ott_id"] == picked_ott_id), None)
        if match is None:
            raise SystemExit(
                f"{AMBIGUOUS_OVERRIDES_PATH}: ott_id {picked_ott_id} for {name!r} is not among "
                f"TNRS's own candidates ({[c['ott_id'] for c in candidates]}) -- stale override?"
            )
        resolved[name] = match
    return resolved, still_ambiguous


def retry_unmatched(
    unmatched: list[str],
    bioclip_by_gbif: dict[str, str],
    ott_synonyms: dict[str, str],
    force: bool,
) -> tuple[dict[str, dict], dict[str, list[dict]], list[str]]:
    """For each unmatched name, tries exactly one fallback name (bioclip_name if it
    differs from gbif_name, else data/ott_taxonomy_synonyms.csv's mapping) against
    TNRS. Returns (resolved: original_name -> taxon, ambiguous, still_unmatched) --
    a fallback can itself come back ambiguous, which is reported like any other
    ambiguity rather than silently picked.
    """
    fallback_by_original: dict[str, str] = {}
    for name in unmatched:
        bioclip_name = bioclip_by_gbif.get(name)
        if bioclip_name and bioclip_name != name:
            fallback_by_original[name] = bioclip_name
        elif name in ott_synonyms:
            fallback_by_original[name] = ott_synonyms[name]

    fallback_names = sorted(set(fallback_by_original.values()))
    tnrs_response = fetch_tnrs(fallback_names, TNRS_RETRY_CACHE_PATH, force)
    resolved_fallback, ambiguous_fallback, unmatched_fallback = classify_matches(tnrs_response)

    resolved: dict[str, dict] = {}
    ambiguous: dict[str, list[dict]] = {}
    still_unmatched: list[str] = []
    for name in unmatched:
        fallback = fallback_by_original.get(name)
        if fallback is None:
            still_unmatched.append(name)
        elif fallback in resolved_fallback:
            resolved[name] = resolved_fallback[fallback]
        elif fallback in ambiguous_fallback:
            ambiguous[name] = ambiguous_fallback[fallback]
        else:
            still_unmatched.append(name)
    return resolved, ambiguous, still_unmatched


def fetch_induced_subtree(ott_ids: list[int], force: bool) -> tuple[dict, list[int]]:
    """Returns (response, pruned_ott_ids). `induced_subtree` handles most
    unsampled-but-valid ids gracefully via its own "broken" fallback (see
    module docstring), but an id OToL's synthesis has fully PRUNED (not just
    unsampled -- e.g. suppressed via a "hidden" taxonomy flag, as happened
    for Emberiza rustica/ott7068450 here) makes the endpoint reject the
    WHOLE request with a 400 instead of a partial result. The error body
    names exactly which id(s) are unknown to it, so this retries with those
    removed rather than guessing which one broke it -- looped in case more
    than one such id is present at once.
    """
    if SUBTREE_CACHE_PATH.is_file() and not force:
        cached = json.loads(SUBTREE_CACHE_PATH.read_text(encoding="utf-8"))
        return cached["subtree"], cached["pruned_ott_ids"]

    remaining = list(ott_ids)
    pruned: list[int] = []
    while True:
        print(f"  Fetching induced subtree for {len(remaining)} OTT ids ...")
        try:
            data = _post_json(SUBTREE_URL, {"ott_ids": remaining})
            break
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            body = json.loads(e.read().decode("utf-8"))
            unknown = body.get("unknown") or {}
            newly_pruned = [int(k.removeprefix("ott")) for k in unknown if unknown[k] == "pruned_ott_id"]
            if not newly_pruned:
                raise SystemExit(f"induced_subtree 400 with no recognized 'unknown' ids to drop: {body}")
            print(f"    {len(newly_pruned)} id(s) fully pruned from synthesis, retrying without them: {newly_pruned}")
            pruned.extend(newly_pruned)
            remaining = [i for i in remaining if i not in newly_pruned]

    SUBTREE_CACHE_PATH.write_text(
        json.dumps({"subtree": data, "pruned_ott_ids": pruned}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return data, pruned


def fetch_about(force: bool) -> dict:
    if ABOUT_CACHE_PATH.is_file() and not force:
        return json.loads(ABOUT_CACHE_PATH.read_text(encoding="utf-8"))
    data = _post_json(ABOUT_URL, {})
    ABOUT_CACHE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Refetch even if a cached response exists.")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    species_rows = load_species_rows(conn)
    conn.close()
    names = [gbif_name for _, gbif_name, _ in species_rows]
    id_by_name = {gbif_name: sid for sid, gbif_name, _ in species_rows}
    bioclip_by_gbif = {gbif_name: bioclip_name for _, gbif_name, bioclip_name in species_rows}
    print(f"Loaded {len(names)} species names from {DB_PATH}")

    print("\n=== Stage 1: TNRS match_names (context: Birds) ===")
    tnrs_response = fetch_tnrs(names, TNRS_CACHE_PATH, args.force)
    resolved, ambiguous, unmatched = classify_matches(tnrs_response)
    resolution_method = {name: "direct" for name in resolved}

    print("\n=== Applying curated decisions for ambiguous/unmatched names ===")
    overrides = load_ambiguous_overrides()
    resolved_from_overrides, ambiguous = apply_ambiguous_overrides(ambiguous, overrides)
    resolved.update(resolved_from_overrides)
    resolution_method.update({name: "ambiguous_override" for name in resolved_from_overrides})
    print(f"  Applied {len(resolved_from_overrides)} override(s) from {AMBIGUOUS_OVERRIDES_PATH}")

    bioclip_synonyms = load_csv_map(BIOCLIP_SYNONYMS_PATH, "gbif_name", "bioclip_name")
    ott_synonyms = load_csv_map(OTT_SYNONYMS_PATH, "gbif_name", "ott_name")
    resolved_from_retry, ambiguous_from_retry, unmatched = retry_unmatched(
        unmatched, bioclip_synonyms, ott_synonyms, args.force
    )
    resolved.update(resolved_from_retry)
    resolution_method.update({name: "synonym_retry" for name in resolved_from_retry})
    ambiguous.update(ambiguous_from_retry)
    print(f"  Resolved {len(resolved_from_retry)} previously-unmatched name(s) via "
          f"{BIOCLIP_SYNONYMS_PATH.name}/{OTT_SYNONYMS_PATH.name}")

    print("\n=== Stage 2: induced_subtree over resolved OTT ids ===")
    about = fetch_about(args.force)
    ott_ids = sorted(t["ott_id"] for t in resolved.values())
    subtree, pruned_ott_ids = fetch_induced_subtree(ott_ids, args.force)
    newick = subtree["newick"]
    pruned_set = set(pruned_ott_ids)
    in_tree = {
        name: (t["ott_id"] not in pruned_set) and (f"ott{t['ott_id']}" in newick)
        for name, t in resolved.items()
    }

    resolutions = {
        "resolved": {
            name: {
                "species_id": id_by_name[name], "ott_id": t["ott_id"], "unique_name": t["unique_name"],
                "method": resolution_method[name], "in_tree": in_tree[name],
                "pruned_from_synthesis": t["ott_id"] in pruned_set,
            }
            for name, t in resolved.items()
        },
        "ambiguous": {
            name: [{"ott_id": t["ott_id"], "unique_name": t["unique_name"], "rank": t["rank"]} for t in cands]
            for name, cands in ambiguous.items()
        },
        "unmatched": unmatched,
    }
    RESOLUTIONS_PATH.write_text(json.dumps(resolutions, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== Report ===")
    print(f"Resolved:  {len(resolved)} / {len(names)}  "
          f"({sum(1 for m in resolution_method.values() if m == 'direct')} direct, "
          f"{sum(1 for m in resolution_method.values() if m == 'ambiguous_override')} via curated override, "
          f"{sum(1 for m in resolution_method.values() if m == 'synonym_retry')} via synonym retry)")
    print(f"Still ambiguous (no override on file): {len(ambiguous)}")
    for name, cands in sorted(ambiguous.items()):
        cand_str = ", ".join(f"{c['unique_name']} (ott{c['ott_id']})" for c in cands)
        print(f"  - {name}: {cand_str}")
    print(f"Still unmatched (no synonym on file, or synonym also failed): {len(unmatched)}")
    for name in sorted(unmatched):
        print(f"  - {name}")

    name_by_ott_id = {t["ott_id"]: name for name, t in resolved.items()}
    if pruned_ott_ids:
        print(f"\nFully pruned from synthesis {about.get('synth_id')} ({len(pruned_ott_ids)} species -- "
              "the id is suppressed from OToL's own tree, e.g. via a \"hidden\" taxonomy flag, so "
              "induced_subtree rejects it outright rather than folding it into an ancestor):")
        for ott_id in sorted(pruned_ott_ids):
            print(f"  - {name_by_ott_id.get(ott_id, '?')} (ott{ott_id})")

    broken = subtree.get("broken") or {}
    if broken:
        print(f"\nResolved but NOT present as their own tip in synthesis {about.get('synth_id')} "
              f"({len(broken)} species -- a real, disclosed OToL taxonomy/synthesis gap, not a fetch bug: "
              "these ids exist in OToL's taxonomy but no input phylogeny sampled them, so induced_subtree "
              "folds them into an ancestral placeholder node instead of giving them a separate leaf):")
        for ott_id_str, placeholder_node in sorted(broken.items()):
            ott_id = int(ott_id_str.removeprefix("ott"))
            print(f"  - {name_by_ott_id.get(ott_id, '?')} (ott{ott_id}) -> folded into {placeholder_node}")

    placed = [name for name, present in in_tree.items() if present]
    print(f"\nOToL synthesis used: {about.get('synth_id')} (taxonomy {about.get('taxonomy_version')})")
    print(f"Induced subtree Newick cached at {SUBTREE_CACHE_PATH} ({len(newick):,} chars); "
          f"{len(placed)} of {len(resolved)} resolved species have an actual node in it "
          f"(the {len(resolved) - len(placed)} missing are exactly the 'folded into' list above).")
    print(f"Full resolution breakdown written to {RESOLUTIONS_PATH}")


if __name__ == "__main__":
    main()
