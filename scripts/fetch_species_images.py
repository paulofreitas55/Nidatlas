#!/usr/bin/env python
"""Fetch one canonical, commercially-usable photo per species.

Cascade: iNaturalist first, Wikidata/Commons as fallback for whatever
iNaturalist misses. This order was decided (not defaulted to) after
measuring both sources against a 50-species sample spanning common
mainland birds, scarce mainland species and island endemics:
iNaturalist alone already reached 100% coverage there under a
commercially-usable licence, and its photos are field observations in
natural habitat -- visually consistent with each other and a better fit
for an occurrence atlas than the mixed studio/museum/captive photos
Wikidata's P18 often points to. Mixing the two 40/60 (Wikidata's own
measured usable-coverage share) would buy no extra coverage at the cost
of an inconsistent visual identity across the atlas. See CLAUDE.md's
"Species photo cascade" design decision for the full writeup.

CC-BY-SA is excluded from BOTH sources, not just treated as lower
priority: viral copyleft is incompatible with a possible future
commercial deployment. This is a real, measured cost, not a free
choice -- in the same 50-species sample, 59% of every Wikidata/Commons
bird image was CC-BY-SA, the single largest licence bucket there (bigger
than CC-BY, far bigger than CC0). Excluding it is why Wikidata's own
usable-coverage looks so much lower (40%) than its raw P18 coverage
(98%). See ATTRIBUTIONS.md for the same figures recorded against the
site's actual licence policy.

Hotlinking, not downloading: image_url always points at iNaturalist's own
S3-backed CDN or Wikimedia's upload.wikimedia.org, never a local copy.
Neither source's terms prohibit this:
- Wikimedia Commons explicitly allows hotlinking to upload.wikimedia.org
  (Commons:Reusing content outside Wikimedia/technical) -- it just doesn't
  RECOMMEND it, purely because a file can later be renamed, replaced or
  deleted upstream with no notice to a hotlinker. That's a reliability
  tradeoff (a species could silently lose its photo on a future page load),
  not a licensing one, and it's accepted here rather than mirrored, in
  line with "does not download" below.
- iNaturalist's API guidance instead warns against heavy *downloading*
  (bulk media fetches over 5GB/hour or 24GB/day risk a permanent API
  block) -- hotlinking each photo through their own CDN on every page view
  is the lighter-weight, policy-aligned choice, not a workaround of it.
Whichever source is used, the photo's licence still requires attribution
regardless of hotlink vs. local copy -- image_attribution is built at
fetch time in ONE consistent format for both sources ("Photo: {name}, some
rights reserved (CC BY)" / "Photo: {name}, no rights reserved (CC0)" -- see
build_display_attribution) so the frontend needs no per-source branching to
display it. This is NOT iNaturalist's own raw attribution string: that one
omits the photographer entirely for a CC0 photo ("no rights reserved", no
name), so the name is instead read from the observation's own uploader
(user.name, falling back to their login) for both licences.

Name matching is exact, never fuzzy -- an iNaturalist taxon result must
have its OWN scientific name equal to the candidate string queried
(case-sensitive, and further filtered to iconic_taxon_name == "Aves" as a
cheap safety net against cross-kingdom homonyms), and a Wikidata item's
P225 must equal it too. Anything that doesn't match exactly is reported
as unmatched, not guessed at -- resolved instead the same way BioCLIP's
and Open Tree of Life's own naming gaps were (see fetch_phylogeny.py):
- data/taxonomy_synonyms.csv (already existed for BioCLIP) is tried first
  for any name that doesn't match directly, on the theory that a naming
  gap recent enough to trip up BioCLIP's vocabulary is likely to trip up
  iNaturalist's/Wikidata's current taxonomy too.
- data/image_source_synonyms.csv (gbif_name,inaturalist_name,wikidata_name,
  note) covers whatever gap remains, source by source -- iNaturalist and
  Wikidata can each lag GBIF differently, so a single shared override
  column isn't always enough. Not pre-populated with guesses; a human adds
  a row only after actually checking what the source currently calls that
  species.

Every API response is cached under data/ (see *_CACHE_PATH below), keyed by
the exact query string/id used, so a rerun only fetches whatever a NEW
candidate name (e.g. a synonym just added) hasn't been tried yet --
already-tried names/ids are never re-requested. Pass --force to ignore the
existing cache entirely and refetch everything.

The iNaturalist OBSERVATIONS cache specifically is NOT the raw API
response -- see _slim_observation()'s own comment for why: caching it
verbatim at per_page=50 across 584 species produced a 2.5GB file and a
run that got slower with every checkpoint (measured directly, not a
theoretical concern), because each embedded observation carries a full
nested taxon/user/identifications/comments payload this script never
reads. Only the handful of fields inat_pick_best_photo() actually uses
are kept. The taxa-search and Wikidata/Commons caches ARE the untouched
raw response -- their payloads are small enough that this isn't a
concern there.
"""

import argparse
import csv
import html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Photographer names from iNaturalist/Commons attribution can be in any
# script (Cyrillic, CJK, ...); Windows' default console codepage (cp1252)
# can't encode most of those and raises UnicodeEncodeError on a bare
# print(), not just mojibake -- would otherwise abort a multi-minute,
# rate-limited run partway through. See CLAUDE.md's Windows-environment note.
sys.stdout.reconfigure(encoding="utf-8")

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "nidatlas.db"

INAT_TAXA_CACHE_PATH = DATA_DIR / "inat_taxa_raw.json"
INAT_OBSERVATIONS_CACHE_PATH = DATA_DIR / "inat_observations_raw.json"
WIKIDATA_CACHE_PATH = DATA_DIR / "wikidata_images_raw.json"
COMMONS_CACHE_PATH = DATA_DIR / "commons_metadata_raw.json"

BIOCLIP_SYNONYMS_PATH = DATA_DIR / "taxonomy_synonyms.csv"
IMAGE_SYNONYMS_PATH = DATA_DIR / "image_source_synonyms.csv"

INAT_API = "https://api.inaturalist.org/v1"
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"

USER_AGENT = "Nidatlas/1.0 (bird atlas project; species image fetch script; contact: paulo.afonso.freitas.2003@gmail.com)"
# iNaturalist's own API recommended-practices guidance asks for <=60 requests/minute.
INAT_REQUEST_DELAY_SECONDS = 1.1
# "Most-faved" is picked among the top OBSERVATIONS_PAGE_SIZE results from
# iNaturalist's own order_by=votes ordering, not an exhaustive scan of every
# qualifying observation (some species have thousands) -- see
# pick_best_observation(). 50 is generous enough to almost always contain
# the true best while keeping each request's response small.
OBSERVATIONS_PAGE_SIZE = 50
ALLOWED_LICENSE_CODES = {"cc0", "cc-by"}

HTML_TAG_RE = re.compile(r"<[^>]+>")


MAX_RETRIES = 4


def _get_json(url: str, headers: dict | None = None) -> dict:
    # A ~40-minute unattended run over two flaky-by-nature APIs WILL see an
    # occasional transient hiccup (read timeout, connection reset, a 5xx) --
    # the first real run died to exactly one of these around species
    # #200/584 with no retry logic at all, losing the rest of that run.
    # Retried with exponential backoff; a 429 (rate limited) backs off
    # longer since that one's expected to need real time to clear. Any
    # other HTTP error (4xx) is a real problem with the request itself, not
    # a transient one -- raised immediately, not retried.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                last_error = exc
                time.sleep(10 * (attempt + 1))
                continue
            if 500 <= exc.code < 600:
                last_error = exc
                time.sleep(2**attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            time.sleep(2**attempt)
    raise last_error


def load_json_cache(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_json_cache(path: Path, cache: dict) -> None:
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def load_csv_map(path: Path, key_col: str, val_col: str) -> dict[str, str]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {row[key_col]: row[val_col] for row in csv.DictReader(f) if row.get(val_col)}


def load_image_synonyms() -> dict[str, dict]:
    if not IMAGE_SYNONYMS_PATH.is_file():
        return {}
    with IMAGE_SYNONYMS_PATH.open(encoding="utf-8", newline="") as f:
        return {row["gbif_name"]: row for row in csv.DictReader(f)}


def candidate_names(gbif_name: str, bioclip_by_gbif: dict, synonym_col: str, image_synonyms: dict) -> list[str]:
    names = [gbif_name]
    bioclip_name = bioclip_by_gbif.get(gbif_name)
    if bioclip_name and bioclip_name not in names:
        names.append(bioclip_name)
    override = image_synonyms.get(gbif_name, {}).get(synonym_col)
    if override and override not in names:
        names.append(override)
    return names


# --- iNaturalist ---

def inat_search_taxon(name: str, cache: dict) -> dict:
    # /v1/taxa/autocomplete, not the general /v1/taxa search: the latter's
    # relevance ranking is tuned for free-text/vernacular search and can
    # bury an exact scientific-name match outside the first page entirely
    # -- measured directly on the real species list, not a theoretical
    # concern: a plain /v1/taxa?q=Bubo+bubo returned 10 unrelated results
    # (matched via Hungarian vernacular names) with "Bubo bubo" itself
    # nowhere in them, even though the taxon exists, is active, and IS the
    # top result from /v1/taxa/autocomplete for the same query. Same fix
    # resolved "Alle alle". autocomplete supports the same rank/is_active/
    # per_page parameters and returns the same taxon object shape.
    #
    # rank=species,subspecies (not species alone): a small, deliberate
    # widening for cases like Anas carolinensis (GBIF's Green-winged Teal,
    # full species) vs iNaturalist's current active taxonomy, which places
    # it as Anas crecca carolinensis -- a SUBSPECIES of Anas crecca, not a
    # species of its own. That's a real rank disagreement between the two
    # taxonomies, not a name to fuzz-match around. This can't introduce a
    # false match for an ordinary species-rank query: exact-name equality
    # in inat_exact_bird_match still applies, and a species-rank binomial
    # query string can never equal a subspecies' trinomial `name` field.
    if name not in cache:
        url = f"{INAT_API}/taxa/autocomplete?q={urllib.parse.quote(name)}&rank=species,subspecies&is_active=true&per_page=10"
        cache[name] = _get_json(url)
        time.sleep(INAT_REQUEST_DELAY_SECONDS)
    return cache[name]


def inat_exact_bird_match(name: str, taxa_response: dict) -> dict | None:
    matches = [
        r for r in taxa_response.get("results", [])
        if r.get("name") == name and r.get("iconic_taxon_name") == "Aves"
    ]
    return matches[0] if len(matches) == 1 else None


def _slim_observation(obs: dict) -> dict:
    # iNaturalist's raw observation objects are enormous -- each embeds a
    # full nested taxon (with its own ancestor chain and a wikipedia_summary
    # paragraph), user, identifications, comments, quality_metrics, etc.
    # None of that is ever used by inat_pick_best_photo(); caching it
    # verbatim at per_page=50 across 584 species produced a 2.5GB cache file
    # (measured directly -- the run was killed after ~6 hours of steadily
    # slowing checkpoint saves, since re-serializing an ever-growing
    # multi-GB dict every 20 fetches is O(n) per save = O(n^2) overall).
    # Keeping only the handful of fields actually read below cuts a typical
    # entry from megabytes to well under a kilobyte.
    #
    # photographer comes from the OBSERVATION's user, not the photo's own
    # "attribution" string -- that string omits the photographer entirely
    # for a CC0 photo ("no rights reserved", no name), so it can't be used
    # to build "Photo: {name}, ..." for both licences uniformly. Same
    # name-or-login fallback iNaturalist's own attribution string uses.
    user = obs.get("user") or {}
    return {
        "id": obs["id"],
        "faves_count": obs.get("faves_count", 0),
        "photographer": user.get("name") or user.get("login") or "unknown photographer",
        "observation_photos": [
            {
                "photo": {
                    "id": op["photo"]["id"],
                    "url": op["photo"]["url"],
                    "license_code": op["photo"].get("license_code"),
                }
            }
            for op in obs.get("observation_photos", [])
        ],
    }


def inat_fetch_observations(taxon_id: int, cache: dict) -> dict:
    key = str(taxon_id)
    if key not in cache:
        url = (
            f"{INAT_API}/observations?taxon_id={taxon_id}"
            f"&photo_license={','.join(ALLOWED_LICENSE_CODES)}&photos=true&quality_grade=research"
            f"&per_page={OBSERVATIONS_PAGE_SIZE}&order_by=votes&order=desc"
        )
        raw = _get_json(url)
        cache[key] = {
            "total_results": raw.get("total_results", 0),
            "results": [_slim_observation(o) for o in raw.get("results", [])],
        }
        time.sleep(INAT_REQUEST_DELAY_SECONDS)
    return cache[key]


def inat_pick_best_photo(observations_response: dict) -> dict | None:
    """Most-faved observation among the top page of results (see
    OBSERVATIONS_PAGE_SIZE), ties broken by the lower (older) observation id
    for a deterministic pick -- not a quality judgement, just a tiebreaker.
    Returns {"observation_id", "photo", "photographer"} for the first photo
    on that observation whose OWN licence is in ALLOWED_LICENSE_CODES (the
    photo_license filter guarantees at least one such photo exists
    somewhere on SOME returned observation, not necessarily every photo on
    the one we picked)."""
    results = observations_response.get("results", [])
    if not results:
        return None
    best = max(results, key=lambda o: (o.get("faves_count", 0), -o["id"]))
    for obs_photo in best.get("observation_photos", []):
        photo = obs_photo["photo"]
        if photo.get("license_code") in ALLOWED_LICENSE_CODES:
            return {"observation_id": best["id"], "photo": photo, "photographer": best.get("photographer")}
    return None


def inat_image_url(photo: dict) -> str:
    # iNaturalist's own documented convention: substitute the size name in
    # the URL. "large" balances quality against not hotlinking a full
    # multi-MB original for what's ultimately a card/species-page image.
    return photo["url"].replace("square", "large")


# --- Wikidata / Commons ---

def fetch_wikidata_images(names: list[str], cache: dict) -> dict[str, str | None]:
    missing = [n for n in names if n not in cache]
    if missing:
        values = " ".join(f'"{n}"' for n in missing)
        query = f"""
        SELECT ?name ?image WHERE {{
          VALUES ?name {{ {values} }}
          ?item wdt:P225 ?name .
          OPTIONAL {{ ?item wdt:P18 ?image }}
        }}
        """
        url = WIKIDATA_SPARQL_URL + "?query=" + urllib.parse.quote(query) + "&format=json"
        data = _get_json(url, headers={"Accept": "application/sparql-results+json"})
        for name in missing:
            cache[name] = None
        for row in data["results"]["bindings"]:
            name = row["name"]["value"]
            image = row.get("image", {}).get("value")
            if image and not cache.get(name):
                cache[name] = image
        time.sleep(0.5)
    return {n: cache.get(n) for n in names}


def commons_filename_from_url(image_url: str) -> str:
    tail = image_url.rsplit("/", 1)[-1]
    return urllib.parse.unquote(tail)


def fetch_commons_metadata(filenames: list[str], cache: dict) -> dict[str, dict]:
    missing = [f for f in filenames if f not in cache]
    for i in range(0, len(missing), 50):
        batch = missing[i : i + 50]
        titles = "|".join(f"File:{f}" for f in batch)
        url = (
            COMMONS_API_URL + "?action=query&titles=" + urllib.parse.quote(titles)
            + "&prop=imageinfo&iiprop=extmetadata|url&iiurlwidth=1024&format=json"
        )
        data = _get_json(url)
        pages = data.get("query", {}).get("pages", {})
        for _, page in pages.items():
            title = page.get("title", "")
            fname = title.split("File:", 1)[-1] if "File:" in title else title
            info = page.get("imageinfo", [{}])[0] if page.get("imageinfo") else {}
            meta = info.get("extmetadata", {})
            cache[fname] = {
                "license_short": meta.get("LicenseShortName", {}).get("value"),
                "artist": meta.get("Artist", {}).get("value"),
                "thumburl": info.get("thumburl"),
                "url": info.get("url"),
            }
        time.sleep(0.3)
    return {f: cache.get(f, {}) for f in filenames}


def normalize_commons_license(license_short: str | None) -> str | None:
    if not license_short:
        return None
    upper = license_short.upper()
    if "CC0" in upper or "PUBLIC DOMAIN" in upper or upper.startswith("PD"):
        return "cc0"
    if "CC BY" in upper and not any(tag in upper for tag in ("SA", "NC", "ND")):
        return "cc-by"
    return None


def strip_html(text: str | None) -> str:
    if not text:
        return "unknown photographer"
    return html.unescape(HTML_TAG_RE.sub("", text)).strip() or "unknown photographer"


def build_display_attribution(photographer: str, license_code: str) -> str:
    # ONE format, built the same way regardless of source, so the frontend
    # never needs per-source branching. Deliberately NOT iNaturalist's own
    # raw photo["attribution"] string -- that string omits the photographer
    # entirely for a CC0 photo ("no rights reserved", no name), which reads
    # as anonymous even though the observation's uploader is right there.
    if license_code == "cc0":
        return f"Photo: {photographer}, no rights reserved (CC0)"
    return f"Photo: {photographer}, some rights reserved (CC BY)"


def ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(species)")}
    for column in ("image_url", "image_source", "image_license", "image_attribution", "image_source_url"):
        if column not in existing:
            conn.execute(f"ALTER TABLE species ADD COLUMN {column} TEXT")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="ignore cached API responses and refetch everything")
    args = parser.parse_args()

    if args.force:
        for path in (INAT_TAXA_CACHE_PATH, INAT_OBSERVATIONS_CACHE_PATH, WIKIDATA_CACHE_PATH, COMMONS_CACHE_PATH):
            path.unlink(missing_ok=True)

    conn = sqlite3.connect(DB_PATH)
    ensure_columns(conn)
    species = conn.execute("SELECT id, gbif_name FROM species ORDER BY id").fetchall()

    bioclip_by_gbif = load_csv_map(BIOCLIP_SYNONYMS_PATH, "gbif_name", "bioclip_name")
    image_synonyms = load_image_synonyms()

    inat_taxa_cache = load_json_cache(INAT_TAXA_CACHE_PATH)
    inat_obs_cache = load_json_cache(INAT_OBSERVATIONS_CACHE_PATH)

    results: dict[str, dict | None] = {}
    inat_unmatched: list[str] = []  # no exact-name taxon at all -- naming gap
    inat_no_licensed_photo: list[str] = []  # taxon found, but no cc0/cc-by research-grade photo

    print(f"=== Step 1: iNaturalist ({len(species)} species) ===")
    fetch_count = 0
    for idx, (_, gbif_name) in enumerate(species):
        taxon = None
        for candidate in candidate_names(gbif_name, bioclip_by_gbif, "inaturalist_name", image_synonyms):
            taxa_response = inat_search_taxon(candidate, inat_taxa_cache)
            fetch_count += 1
            taxon = inat_exact_bird_match(candidate, taxa_response)
            if taxon:
                break

        if taxon is None:
            inat_unmatched.append(gbif_name)
            results[gbif_name] = None
        else:
            obs_response = inat_fetch_observations(taxon["id"], inat_obs_cache)
            fetch_count += 1
            best = inat_pick_best_photo(obs_response)
            if best is None:
                inat_no_licensed_photo.append(gbif_name)
                results[gbif_name] = None
            else:
                photo = best["photo"]
                results[gbif_name] = {
                    "image_url": inat_image_url(photo),
                    "image_source": "inaturalist",
                    "image_license": photo["license_code"],
                    "image_attribution": build_display_attribution(best["photographer"], photo["license_code"]),
                    "image_source_url": f"https://www.inaturalist.org/observations/{best['observation_id']}",
                }

        if fetch_count and fetch_count % 20 == 0:
            save_json_cache(INAT_TAXA_CACHE_PATH, inat_taxa_cache)
            save_json_cache(INAT_OBSERVATIONS_CACHE_PATH, inat_obs_cache)
        if (idx + 1) % 50 == 0:
            print(f"  ...{idx + 1}/{len(species)}")

    save_json_cache(INAT_TAXA_CACHE_PATH, inat_taxa_cache)
    save_json_cache(INAT_OBSERVATIONS_CACHE_PATH, inat_obs_cache)

    inat_covered = sum(1 for r in results.values() if r)
    print(f"iNaturalist coverage: {inat_covered}/{len(species)}")
    print(f"  unmatched by name: {len(inat_unmatched)}")
    print(f"  matched but no cc0/cc-by research-grade photo: {len(inat_no_licensed_photo)}")

    # --- Step 2: Wikidata/Commons fallback, only for species iNaturalist missed ---
    needs_fallback = [name for name, r in results.items() if r is None]
    print(f"\n=== Step 2: Wikidata/Commons fallback ({len(needs_fallback)} species) ===")

    wikidata_cache = load_json_cache(WIKIDATA_CACHE_PATH)
    commons_cache = load_json_cache(COMMONS_CACHE_PATH)

    candidates_by_gbif = {
        name: candidate_names(name, bioclip_by_gbif, "wikidata_name", image_synonyms) for name in needs_fallback
    }
    all_query_names = sorted({n for names in candidates_by_gbif.values() for n in names})
    wd_images = fetch_wikidata_images(all_query_names, wikidata_cache)
    save_json_cache(WIKIDATA_CACHE_PATH, wikidata_cache)

    wd_unmatched: list[str] = []
    filenames_needed: dict[str, str] = {}
    for name, candidates in candidates_by_gbif.items():
        image_url = next((wd_images.get(c) for c in candidates if wd_images.get(c)), None)
        if image_url:
            filenames_needed[name] = commons_filename_from_url(image_url)
        else:
            wd_unmatched.append(name)

    commons_meta = fetch_commons_metadata(sorted(set(filenames_needed.values())), commons_cache)
    save_json_cache(COMMONS_CACHE_PATH, commons_cache)

    wd_no_license: list[str] = []
    for name, fname in filenames_needed.items():
        meta = commons_meta.get(fname, {})
        license_code = normalize_commons_license(meta.get("license_short"))
        if license_code is None:
            wd_no_license.append(name)
            continue
        photographer = strip_html(meta.get("artist"))
        results[name] = {
            "image_url": meta.get("thumburl") or meta.get("url"),
            "image_source": "wikidata",
            "image_license": license_code,
            "image_attribution": build_display_attribution(photographer, license_code),
            "image_source_url": f"https://commons.wikimedia.org/wiki/File:{urllib.parse.quote(fname)}",
        }

    wd_covered = len(filenames_needed) - len(wd_no_license)
    print(f"Wikidata/Commons coverage of the fallback set: {wd_covered}/{len(needs_fallback)}")
    print(f"  unmatched by name: {len(wd_unmatched)}")
    print(f"  matched but no CC0/CC-BY licence: {len(wd_no_license)}")

    # --- Write to DB ---
    for sid, gbif_name in species:
        r = results.get(gbif_name)
        if r:
            conn.execute(
                """UPDATE species SET image_url = ?, image_source = ?, image_license = ?,
                   image_attribution = ?, image_source_url = ? WHERE id = ?""",
                (r["image_url"], r["image_source"], r["image_license"], r["image_attribution"], r["image_source_url"], sid),
            )
    conn.commit()
    conn.close()

    # --- Final report ---
    total = len(species)
    final_covered = sum(1 for r in results.values() if r)
    by_source = {"inaturalist": 0, "wikidata": 0}
    by_license = {"cc0": 0, "cc-by": 0}
    for r in results.values():
        if r:
            by_source[r["image_source"]] += 1
            by_license[r["image_license"]] += 1

    print("\n=== Final coverage ===")
    print(f"Total: {final_covered}/{total} ({final_covered / total * 100:.0f}%)")
    print(f"  from iNaturalist: {by_source['inaturalist']}")
    print(f"  from Wikidata/Commons (fallback): {by_source['wikidata']}")
    print(f"  licence CC0: {by_license['cc0']}, CC-BY: {by_license['cc-by']}")

    never_matched = sorted(set(inat_unmatched) & set(wd_unmatched))
    still_uncovered = sorted(name for name, r in results.items() if r is None)
    print(f"\n=== Species with NO image from either source ({len(still_uncovered)}) ===")
    for name in still_uncovered:
        reason = "unmatched by name in BOTH sources" if name in never_matched else "matched, but no usable licence in either source"
        print(f"  - {name}  [{reason}]")

    if never_matched:
        print(
            f"\n{len(never_matched)} of those are pure naming gaps (no exact scientific-name match in "
            f"either source's own taxonomy) -- add rows to {IMAGE_SYNONYMS_PATH} "
            "(gbif_name,inaturalist_name,wikidata_name,note) once you've checked what each source "
            "currently calls that species, then rerun."
        )


if __name__ == "__main__":
    main()
