#!/usr/bin/env python
"""Fetch Portuguese, Spanish and English vernacular names from GBIF per species.

Our species table only stores the clean scientific name (gbif_name), not a
GBIF usageKey, so each species is first resolved via the species match API,
then its vernacular names are fetched by that resolved key.
"""

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "nidatlas.db"
CACHE_PATH = DATA_DIR / "vernacular_cache.json"

GBIF_API = "https://api.gbif.org/v1"
REQUEST_DELAY_SECONDS = 0.3
PAGE_LIMIT = 300
# GBIF's vernacularNames.language uses ISO 639-3 (three-letter) codes, not ISO 639-1.
LANGUAGES = {"pt": "por", "es": "spa", "en": "eng"}
USER_AGENT = "Nidatlas/1.0 (bird atlas project; vernacular-name fetch script)"


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def load_cache() -> dict:
    if CACHE_PATH.is_file():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_all_vernacular_names(usage_key: int) -> list[dict]:
    results = []
    offset = 0
    while True:
        url = f"{GBIF_API}/species/{usage_key}/vernacularNames?limit={PAGE_LIMIT}&offset={offset}"
        data = _get_json(url)
        results.extend(data["results"])
        time.sleep(REQUEST_DELAY_SECONDS)
        if data.get("endOfRecords", True):
            break
        offset += PAGE_LIMIT
    return results


def fetch_species_entry(gbif_name: str) -> dict:
    # kingdom=Animalia disambiguates genus homonyms shared with plants/fungi
    # (e.g. Chloris is both a finch genus and a grass genus) -- safe for every
    # row here since this whole table is birds.
    match_url = f"{GBIF_API}/species/match?name={urllib.parse.quote(gbif_name)}&kingdom=Animalia"
    match = _get_json(match_url)
    time.sleep(REQUEST_DELAY_SECONDS)

    usage_key = match.get("usageKey")
    vernacular_names = fetch_all_vernacular_names(usage_key) if usage_key else []
    return {"match": match, "vernacular_names": vernacular_names}


def pick_name(vernacular_names: list[dict], language: str) -> str | None:
    candidates = [v for v in vernacular_names if v.get("language") == language and v.get("vernacularName")]
    if not candidates:
        return None
    preferred = [v for v in candidates if v.get("preferred") is True]
    return (preferred[0] if preferred else candidates[0])["vernacularName"]


def ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(species)")}
    for column in ("common_name_pt", "common_name_es", "common_name_en"):
        if column not in existing:
            conn.execute(f"ALTER TABLE species ADD COLUMN {column} TEXT")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    ensure_columns(conn)

    species = conn.execute("SELECT id, gbif_name FROM species ORDER BY id").fetchall()
    cache = load_cache()

    coverage = {"pt": 0, "es": 0, "en": 0}
    no_name_species = []
    fetched_since_save = 0

    for species_id, gbif_name in species:
        if gbif_name not in cache:
            print(f"Fetching {gbif_name}...")
            try:
                cache[gbif_name] = fetch_species_entry(gbif_name)
            except (urllib.error.URLError, TimeoutError) as exc:
                print(f"  Warning: failed to fetch {gbif_name}: {exc}")
                cache[gbif_name] = {"match": None, "vernacular_names": []}
            fetched_since_save += 1
            if fetched_since_save >= 25:
                save_cache(cache)
                fetched_since_save = 0

        vernacular_names = cache[gbif_name].get("vernacular_names", [])
        names = {code: pick_name(vernacular_names, lang) for code, lang in LANGUAGES.items()}

        conn.execute(
            "UPDATE species SET common_name_pt = ?, common_name_es = ?, common_name_en = ? WHERE id = ?",
            (names["pt"], names["es"], names["en"], species_id),
        )

        for code in LANGUAGES:
            if names[code]:
                coverage[code] += 1
        if not any(names.values()):
            no_name_species.append(gbif_name)

    conn.commit()
    save_cache(cache)
    conn.close()

    total = len(species)
    print()
    print("=== Coverage ===")
    for code in ("pt", "es", "en"):
        print(f"{code}: {coverage[code]}/{total}")

    print()
    print(f"=== Species with no name in any language ({len(no_name_species)}) ===")
    for name in no_name_species:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
