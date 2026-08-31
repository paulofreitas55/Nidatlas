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

Candidate photos are screened before ranking: an observation annotated
"Alive or Dead: Dead" (iNaturalist's own structured controlled-term
annotation, controlled_attribute_id 17 / controlled_value_id 19 -- see
observation_is_suspect()), or whose description/tags match a taxidermy,
museum-specimen or toy/figurine keyword, or an identifiable-person keyword,
is excluded from consideration entirely, never merely deprioritized -- see
SUSPECT_KEYWORDS_RE and PEOPLE_KEYWORDS_RE. The people screen exists for a
reason distinct from the licence check above: CC0/CC-BY clears the PHOTO's
copyright, not the separate image/likeness rights of a person who happens
to be IN it, and this project has no consent from any such person to
publish their photo. Among the remaining candidates the pick is still
"most-faved first" as before, with identifications_count (more independent
agreement the ID is correct) as the tiebreaker ahead of the deterministic
id tiebreak. This was added after a manual review of the first 584-species
run found 40 species whose photo was a dead/taxidermied specimen or
contained a toy/figurine -- keyword and annotation screening catches most
of that class going forward, but is a heuristic, not a guarantee (both
screens are text-only, over description/tags most observations don't even
have), which is exactly what image_overrides.csv (see below) exists to
correct by hand on a case-by-case basis -- as happened for two species in
that same remediation whose photo showed a real live bird but also an
identifiable person's face (one a child's), caught only by looking at the
actual photo, not by this screen.

data/image_overrides.csv (gbif_name,source,inat_observation_id,
inat_photo_id,commons_filename,note) lets a human pin one specific photo
for one specific species, bypassing the entire search-and-rank cascade
above for that species. Highest precedence: a species listed there is
resolved directly from the named observation/photo or Commons file and
never enters the automated iNaturalist or Wikidata search at all. Not
pre-populated with guesses -- a human adds a row only after actually
looking at the candidate photo. See CLAUDE.md's "Species photo cascade"
design decision for why this exists alongside (not instead of) the
automated screening above.

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
INAT_OBSERVATION_OVERRIDE_CACHE_PATH = DATA_DIR / "inat_observation_overrides_raw.json"
WIKIDATA_CACHE_PATH = DATA_DIR / "wikidata_images_raw.json"
COMMONS_CACHE_PATH = DATA_DIR / "commons_metadata_raw.json"

BIOCLIP_SYNONYMS_PATH = DATA_DIR / "taxonomy_synonyms.csv"
IMAGE_SYNONYMS_PATH = DATA_DIR / "image_source_synonyms.csv"
IMAGE_OVERRIDES_PATH = DATA_DIR / "image_overrides.csv"

INAT_API = "https://api.inaturalist.org/v1"
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"

USER_AGENT = "Nidatlas/1.0 (bird atlas project; species image fetch script; contact: paulo.afonso.freitas.2003@gmail.com)"
# iNaturalist's own API recommended-practices guidance asks for <=60 requests/minute.
INAT_REQUEST_DELAY_SECONDS = 1.1
# "Most-faved" is picked among the top OBSERVATIONS_PAGE_SIZE results from
# iNaturalist's own order_by=votes ordering, not an exhaustive scan of every
# qualifying observation (some species have thousands) -- see
# pick_best_observation(). 100 (iNaturalist's per_page max is 200) leaves
# enough headroom that excluding suspect/dead/toy candidates (see
# observation_is_suspect()) still almost always leaves a real choice on the
# same single request, rather than needing a second page fetch.
OBSERVATIONS_PAGE_SIZE = 100
ALLOWED_LICENSE_CODES = {"cc0", "cc-by"}

HTML_TAG_RE = re.compile(r"<[^>]+>")

# iNaturalist's own controlled-term annotation for "Alive or Dead" (see
# GET /v1/controlled_terms) -- attribute id 17, value id 19 is "Dead".
# Structured and reliable where it's present, unlike free-text screening.
ALIVE_OR_DEAD_ATTRIBUTE_ID = 17
DEAD_VALUE_ID = 19

# Free-text screen for what the structured annotation can't catch: taxidermy/
# museum-specimen photos that were never annotated, and toy/figurine/replica
# photos (not a real animal at all, so "Alive or Dead" doesn't apply to them
# in the first place). Word-boundary, case-insensitive, checked against each
# observation's description + tags. A heuristic, not a guarantee -- see
# image_overrides.csv for the manual escape hatch when this still misses one.
SUSPECT_KEYWORDS_RE = re.compile(
    r"\b("
    r"taxidermy|museum|specimen|mounted?|skeleton|skin|preserved|cadaver|corpse|"
    r"carcass|roadkill|road[- ]?kill|window[- ]?strike|window[- ]?collision|"
    r"found dead|pinned|study skin|"
    r"toy|toys|figurine|plush|decoy|statue|replica|"
    r"artificial|sculpture|carving|ornament|souvenir|"
    r"rubber duck|plastic bird|origami|illustration|painting|drawing|cartoon"
    r")\b",
    re.IGNORECASE,
)

# Screens for an identifiable PERSON in frame, not for licence/copyright --
# CC0/CC-BY only clear the photo's own copyright, they say nothing about the
# separate image rights of a person who happens to be IN the photo, and this
# project has no consent from any such person to publish their likeness
# (this really happened: the 2026-08 photo remediation's automated re-picks
# for Columba livia and Sitta europaea both put an identifiable person's --
# in one case a child's -- face front and center; see image_overrides.csv
# for the fix). Same limitation as SUSPECT_KEYWORDS_RE above: this is a
# text-only heuristic over description/tags, not actual image content, and
# most observations have neither -- it will catch an observer who mentions
# a person in their own text, nothing more. A clean pass here is NOT proof
# the photo has no one in it; manually look at every candidate before
# trusting it, same as for the dead-specimen/toy screen.
PEOPLE_KEYWORDS_RE = re.compile(
    r"\b(person|people|human|man|men|woman|women|child|children|kid|kids|boy|girl|selfie|portrait|family)\b",
    re.IGNORECASE,
)


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
    # entry from megabytes to well under a kilobyte -- including the fields
    # observation_is_suspect() needs (description/tags/is_dead), which are
    # themselves short strings/a bool, not the full nested payload they're
    # derived from.
    #
    # photographer comes from the OBSERVATION's user, not the photo's own
    # "attribution" string -- that string omits the photographer entirely
    # for a CC0 photo ("no rights reserved", no name), so it can't be used
    # to build "Photo: {name}, ..." for both licences uniformly. Same
    # name-or-login fallback iNaturalist's own attribution string uses.
    user = obs.get("user") or {}
    is_dead = any(
        a.get("controlled_attribute_id") == ALIVE_OR_DEAD_ATTRIBUTE_ID
        and a.get("controlled_value_id") == DEAD_VALUE_ID
        for a in obs.get("annotations", [])
    )
    return {
        "id": obs["id"],
        "faves_count": obs.get("faves_count", 0),
        "identifications_count": obs.get("identifications_count", 0),
        "is_dead": is_dead,
        "description": (obs.get("description") or "")[:1000],
        "tags": obs.get("tags", []),
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


def observation_is_suspect(obs: dict) -> bool:
    """True if this observation looks like a dead/taxidermied specimen, a
    toy/figurine, or has an identifiable person in frame, rather than a
    clean live-bird-in-the-field photo -- see SUSPECT_KEYWORDS_RE,
    ALIVE_OR_DEAD_ATTRIBUTE_ID and PEOPLE_KEYWORDS_RE above for what's
    checked and why. A heuristic: absence of a signal is not proof the
    photo is fine, just that nothing here caught it -- always look at the
    actual photo before trusting an automated pick (see PEOPLE_KEYWORDS_RE's
    own comment for a real case this text-only screen could not catch)."""
    if obs.get("is_dead"):
        return True
    text = obs.get("description") or ""
    tags = obs.get("tags") or []
    haystack = text + " " + " ".join(tags)
    return bool(SUSPECT_KEYWORDS_RE.search(haystack) or PEOPLE_KEYWORDS_RE.search(haystack))


def inat_pick_best_photo(observations_response: dict, exclude_observation_ids: frozenset[int] = frozenset()) -> dict | None:
    """Most-faved observation among the top page of results (see
    OBSERVATIONS_PAGE_SIZE), after dropping any observation in
    exclude_observation_ids and any that observation_is_suspect() flags as a
    likely dead specimen or toy/figurine. Ties on faves_count are broken by
    identifications_count (more independent agreement the ID is correct),
    then by the lower (older) observation id for a fully deterministic pick.
    Returns {"observation_id", "photo", "photographer"} for the first photo
    on that observation whose OWN licence is in ALLOWED_LICENSE_CODES (the
    photo_license filter guarantees at least one such photo exists
    somewhere on SOME returned observation, not necessarily every photo on
    the one we picked)."""
    results = [
        o for o in observations_response.get("results", [])
        if o["id"] not in exclude_observation_ids and not observation_is_suspect(o)
    ]
    if not results:
        return None
    best = max(results, key=lambda o: (o.get("faves_count", 0), o.get("identifications_count", 0), -o["id"]))
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


def inat_fetch_observation_by_id(observation_id: int, cache: dict) -> dict | None:
    """Used only by manual overrides (see resolve_inat_override) -- fetches
    one specific observation by id, not a taxon search. No suspect
    screening here: a human picked this observation deliberately, an
    override is the explicit "trust me" escape hatch from that screening."""
    key = str(observation_id)
    if key not in cache:
        url = f"{INAT_API}/observations/{observation_id}"
        raw = _get_json(url)
        results = raw.get("results", [])
        cache[key] = _slim_observation(results[0]) if results else None
        time.sleep(INAT_REQUEST_DELAY_SECONDS)
    return cache[key]


# --- Manual overrides ---

def load_image_overrides() -> dict[str, dict]:
    if not IMAGE_OVERRIDES_PATH.is_file():
        return {}
    with IMAGE_OVERRIDES_PATH.open(encoding="utf-8", newline="") as f:
        return {row["gbif_name"]: row for row in csv.DictReader(f) if row.get("gbif_name")}


def resolve_inat_override(row: dict, cache: dict) -> dict | None:
    observation_id = int(row["inat_observation_id"])
    obs = inat_fetch_observation_by_id(observation_id, cache)
    if obs is None:
        return None
    photo_id = row.get("inat_photo_id", "").strip()
    candidates = obs.get("observation_photos", [])
    if photo_id:
        candidates = [op for op in candidates if str(op["photo"]["id"]) == photo_id]
    for obs_photo in candidates:
        photo = obs_photo["photo"]
        if photo.get("license_code") in ALLOWED_LICENSE_CODES:
            return {
                "image_url": inat_image_url(photo),
                "image_source": "inaturalist",
                "image_license": photo["license_code"],
                "image_attribution": build_display_attribution(obs.get("photographer"), photo["license_code"]),
                "image_source_url": f"https://www.inaturalist.org/observations/{observation_id}",
            }
    return None


def resolve_commons_override(row: dict, cache: dict) -> dict | None:
    fname = row["commons_filename"].strip()
    meta = fetch_commons_metadata([fname], cache).get(fname, {})
    license_code = normalize_commons_license(meta.get("license_short"))
    if license_code is None:
        return None
    return {
        "image_url": meta.get("thumburl") or meta.get("url"),
        "image_source": "wikidata",
        "image_license": license_code,
        "image_attribution": build_display_attribution(strip_html(meta.get("artist")), license_code),
        "image_source_url": f"https://commons.wikimedia.org/wiki/File:{urllib.parse.quote(fname)}",
    }


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


def load_refetch_list(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def observation_id_from_url(source_url: str) -> int:
    return int(source_url.rstrip("/").rsplit("/", 1)[-1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="ignore cached API responses and refetch everything")
    parser.add_argument(
        "--refetch-species-file",
        type=Path,
        default=None,
        help=(
            "path to a text file of gbif_name values (one per line, '#' comments allowed) to "
            "reprocess -- every other species is left untouched. Each named species' current "
            "iNaturalist observation (if any) is excluded from re-selection, so a different photo "
            "is picked, and its cached observations entry is refetched live so the new "
            "specimen/toy screening (see observation_is_suspect) actually applies to it."
        ),
    )
    args = parser.parse_args()

    if args.force:
        for path in (
            INAT_TAXA_CACHE_PATH, INAT_OBSERVATIONS_CACHE_PATH, INAT_OBSERVATION_OVERRIDE_CACHE_PATH,
            WIKIDATA_CACHE_PATH, COMMONS_CACHE_PATH,
        ):
            path.unlink(missing_ok=True)

    conn = sqlite3.connect(DB_PATH)
    ensure_columns(conn)
    all_species = conn.execute("SELECT id, gbif_name FROM species ORDER BY id").fetchall()

    refetch_exclude: dict[str, int] = {}
    if args.refetch_species_file:
        refetch_names = set(load_refetch_list(args.refetch_species_file))
        species = [(sid, name) for sid, name in all_species if name in refetch_names]
        missing = refetch_names - {name for _, name in species}
        if missing:
            print(f"WARNING: {len(missing)} name(s) in {args.refetch_species_file} not found in species table: {sorted(missing)}")
        current_rows = conn.execute(
            "SELECT gbif_name, image_source, image_source_url FROM species WHERE gbif_name IN ({})".format(
                ",".join("?" * len(species))
            ),
            [name for _, name in species],
        ).fetchall()
        for name, source, source_url in current_rows:
            if source == "inaturalist" and source_url:
                refetch_exclude[name] = observation_id_from_url(source_url)
    else:
        species = all_species

    bioclip_by_gbif = load_csv_map(BIOCLIP_SYNONYMS_PATH, "gbif_name", "bioclip_name")
    image_synonyms = load_image_synonyms()
    image_overrides = load_image_overrides()

    inat_taxa_cache = load_json_cache(INAT_TAXA_CACHE_PATH)
    inat_obs_cache = load_json_cache(INAT_OBSERVATIONS_CACHE_PATH)
    inat_override_cache = load_json_cache(INAT_OBSERVATION_OVERRIDE_CACHE_PATH)
    commons_cache = load_json_cache(COMMONS_CACHE_PATH)

    results: dict[str, dict | None] = {}
    inat_unmatched: list[str] = []  # no exact-name taxon at all -- naming gap
    inat_no_licensed_photo: list[str] = []  # taxon found, but no cc0/cc-by research-grade photo

    # --- Step 0: manual overrides -- highest precedence, resolved before (and
    # instead of) the automated search below for whichever species have one ---
    species_names = {name for _, name in species}
    relevant_overrides = {name: row for name, row in image_overrides.items() if name in species_names}
    override_resolved: set[str] = set()
    if relevant_overrides:
        print(f"=== Step 0: manual overrides ({len(relevant_overrides)}) ===")
        for gbif_name, row in relevant_overrides.items():
            source = (row.get("source") or "").strip().lower()
            if source == "inaturalist":
                r = resolve_inat_override(row, inat_override_cache)
            elif source == "commons":
                r = resolve_commons_override(row, commons_cache)
            else:
                print(f"  ERROR: {gbif_name}: unknown override source {row.get('source')!r} (expected inaturalist/commons)")
                continue
            if r is None:
                print(f"  ERROR: {gbif_name}: override row in {IMAGE_OVERRIDES_PATH} could not be resolved -- check the observation/photo/file id")
                continue
            results[gbif_name] = r
            override_resolved.add(gbif_name)
            print(f"  {gbif_name}: pinned to {r['image_source_url']}")
        save_json_cache(INAT_OBSERVATION_OVERRIDE_CACHE_PATH, inat_override_cache)
        save_json_cache(COMMONS_CACHE_PATH, commons_cache)

    print(f"\n=== Step 1: iNaturalist ({len(species)} species) ===")
    fetch_count = 0
    for idx, (_, gbif_name) in enumerate(species):
        if gbif_name in override_resolved:
            continue

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
            if args.refetch_species_file:
                # old cache entries predate description/tags/is_dead -- a
                # cached hit would silently skip the new specimen/toy screen
                inat_obs_cache.pop(str(taxon["id"]), None)
            obs_response = inat_fetch_observations(taxon["id"], inat_obs_cache)
            fetch_count += 1
            exclude_ids = frozenset({refetch_exclude[gbif_name]}) if gbif_name in refetch_exclude else frozenset()
            best = inat_pick_best_photo(obs_response, exclude_ids)
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
        # A --refetch-species-file run is small (well under 50) and each
        # species can take a real, variable amount of wall time -- some
        # extremely common species (Columba livia, Motacilla alba, ...)
        # make iNaturalist's own order_by=votes query genuinely slow
        # server-side -- so print every species in that mode instead of
        # going silent for the whole run; a full 584-species run keeps the
        # coarser every-50 cadence.
        progress_step = 1 if args.refetch_species_file else 50
        if (idx + 1) % progress_step == 0 or (idx + 1) == len(species):
            print(f"  ...{idx + 1}/{len(species)}  ({gbif_name})")

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

    if args.refetch_species_file:
        print(f"\n=== Refetched species: new photo for review ({len(species)}) ===")
        for _, gbif_name in species:
            r = results.get(gbif_name)
            if r is None:
                print(f"  - {gbif_name}: NO REPLACEMENT FOUND -- see errors/uncovered list above")
                continue
            previous = (
                f" (previously observation {refetch_exclude[gbif_name]})"
                if gbif_name in refetch_exclude
                else " (no previous iNaturalist observation to exclude)"
            )
            print(f"  - {gbif_name}: {r['image_url']}{previous}")
            print(f"      observation: {r['image_source_url']}")


if __name__ == "__main__":
    main()
