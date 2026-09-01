# Nidatlas — onboarding for a fresh session

Read this before touching anything. It's the accumulated context from every
prior session — the "why" behind decisions that aren't obvious from the code
alone. If you're about to change something described here, read the
reasoning first; if you still think it's wrong, say so and explain why
before changing it (see Conventions below).

## What this is

Nidatlas is a web atlas of the birds of the Iberian Peninsula (Portugal and
Spain, including the Azores, Madeira, Canary Islands and Balearics), built
on GBIF occurrence data. It lets people browse an illustrated species list,
see a species' seasonal and geographic distribution down to a 10km grid
cell, and explore an administrative-region choropleth map showing which
species are most and least characteristic of a given district, province or
island. A BioCLIP 2-based photo identification feature lets a visitor
upload or capture a photo and get the model's top-5 guesses among Iberia's
584 species. It's an optional add-on, not a hard dependency of the rest of
the app (see the `identify` sections below and this file's "IDENTIFY
feature isolation" design decision) — a deployment can still run without
the ~1.7GB of model weights and PyTorch if it doesn't want that feature.

## Architecture

**Data pipeline** (offline, run via `scripts/`, output checked into `data/`
and `static/` — see "Rebuilding from scratch" below for the exact order):
GBIF gives you two raw downloads — a species-list CSV and an occurrence
"cube" CSV (species × year-month × MGRS 10km cell). These get filtered and
reshaped by a chain of scripts into a single SQLite database
(`data/nidatlas.db`) with pre-aggregated tables, plus a handful of GeoJSON
files for the map basemap and administrative regions.

**Query layer** (`src/queries.py`): every read the app can make against
`data/nidatlas.db`, as plain functions taking a `sqlite3.Connection` and
returning plain dicts/lists — no ORM, no web framework import. This module
also runs standalone (`python src/queries.py`) as a smoke test/demo that
prints a few real queries to the console.

**API** (`src/api.py`): a thin FastAPI layer over `queries.py`. Each route
handler is a few lines: call the matching query function, translate
`ValueError` (not-found) into a 404. Also serves the static frontend
directly (`StaticFiles` mounted at `/`, with explicit routes for `/`,
`/map`, `/tree`, `/rank` and (when enabled) `/identify`, since a
same-directory HTML file under a different name needs an explicit route —
`StaticFiles(html=True)` only auto-serves `index.html`). Runs a `no-store`
`Cache-Control` middleware on every response — this is a local/dev-oriented
choice (see the comment in `api.py`), revisit before a real public
deployment where you'd want real caching back. `GET /api/config` reports
`{"identify_enabled": bool}` so the static frontend can decide at runtime
whether to show the IDENTIFY nav link/page (see below). `POST /api/identify`
(only registered when `ENABLE_IDENTIFY` is set — see "IDENTIFY feature
isolation" in Design decisions) accepts a multipart image upload, enforces
a max file size and an allowed-MIME-type list, rate-limits by client IP (a
simple in-process sliding-window counter — no new dependency; see the
comment above it in `api.py`), and delegates to `src/identification.py`.

**Identification** (`src/identification.py`): the web-facing counterpart to
`scripts/identify.py`'s CLI, both restricted to Iberia's 584 species (see
"BioCLIP's label space restricted to Iberian species" below). Classifies an
in-memory image (never written to disk) and returns up to 5 candidates with
confidence scores; `api.py` then maps each candidate's `bioclip_name` back
to a species row via `queries.species_by_bioclip_names`. Deliberately keeps
every `torch`/`bioclip`/PIL import lazy, inside the functions that actually
need them, so importing this module — which `api.py` does unconditionally —
never requires those packages to be installed; see "IDENTIFY feature
isolation" in Design decisions for the full reasoning and how this combines
with `ENABLE_IDENTIFY` end to end.

**Frontend** (`static/`, no build step, no framework, no bundler — plain
`<script>` tags): six pages sharing common CSS/JS.
- `atlas.html` + `app.js` — the landing page (served at `/`). The full
  584-species card grid, grouped by order/family, with search.
- `map.html` + `map.css` + `map.js` — served at `/map`. The 97-region
  choropleth, shaded by total occurrences, with an archipelago panel
  selector and a side panel showing per-region species rankings.
- `species.html` + `species.js` + `species.css` — one species' profile:
  identity, seasonality chart (pure SVG), its own distribution map, and a
  "Position in the tree of life" section (see `phylogeny` below).
- `tree.html` + `tree.css` + `tree.js` — served at `/tree`. The ENTIRE
  phylogenetic tree (all 577 placed species) drawn as one continuous,
  vertically-scrolling rectangular cladogram, plus zoom controls and a
  species search box (see `phylogeny` below) — rendering all ~1,160 visible
  nodes at once turned out to be cheap (see the render-time note below), so
  there's no level-by-level navigation to maintain.
- `rank.html` + `rank.css` + `rank.js` — served at `/rank`. Two lists (most-
  and least-recorded species, top/bottom 50 by default, expandable to the
  full 584) ranked strictly by raw `total_occurrences` — a deliberately
  different axis from the map's per-region concentration ranking and the
  atlas's taxonomic ordering, with an explicit on-page note (`rank.explainer`
  in i18n.json) so a record count is never mistaken for actual abundance.
  Backed by `GET /api/species/ranking`, which reuses `species_profile`'s own
  `RANK() OVER (ORDER BY total_occurrences DESC)` so a species' position here
  always agrees with the "X of 584" figure on its own page — see
  `species_ranking` in `src/queries.py`. The full-584 view (toggled via
  "View full ranking") always opens scrolled to rank #1, not wherever the
  toggle button happened to sit on the page, and has its own name/vernacular
  search box (`rank-full-search`) that filters that one list in place —
  separate from the top/bottom-50 default view, which has no search of its
  own since 50 rows needs no filtering aid.
- `identify.html` + `identify.css` + `identify.js` — served at `/identify`,
  but only when the backend has `ENABLE_IDENTIFY` set (see "IDENTIFY
  feature isolation" in Design decisions); the nav link to it
  (`.nav-identify-link`, present but `hidden` in every page's markup) is
  un-hidden by `applyFeatureFlags()` in `lang.js` (called from every page's
  own `init()`) only after `GET /api/config` confirms the feature is on.
  Upload a photo or take one with the device camera (two plain
  `<input type=file>` elements, one with `capture="environment"` as a
  camera hint — no `getUserMedia`/live video preview, kept deliberately
  simple) and POST it to `/api/identify` — no location or other personal
  data is collected. An earlier draft accepted an optional browser
  geolocation, opt-in via a button, but the server discarded it unread
  (`identification.classify_image_bytes()` never took a location argument)
  — a real privacy exposure for zero benefit, so it was removed rather than
  wired up or kept dormant. Mobile-first CSS (this is the view most likely
  used outdoors, camera in hand). Renders
  results one of two ways depending on the response's `confident` flag,
  never both: a real candidate list (thumbnail via the shared
  `buildPhotoThumb`/`buildPhotoCredit` from `lang.js`, a confidence bar,
  link to the species page) when the top score cleared
  `identification.CONFIDENCE_THRESHOLD` server-side, or a plain-text,
  visually de-emphasized, collapsed-by-default list behind a "show
  low-confidence guesses anyway" `<details>` otherwise — see "IDENTIFY
  confidence threshold" in Design decisions for why presenting a
  low-confidence result as if it were a real answer would be dishonest, and
  why a wide empirical gap makes that distinction safe to draw
  automatically instead of leaving it to the user to judge from the number
  alone. A persistent disclaimer (never conditionally hidden) states both
  that results come from an AI model and may be wrong, and that the model
  can only name one of the 584 Iberian species even when the photo is of
  something else entirely.
- `cladogram.js` — the shared rectangular-cladogram SVG renderer behind both
  `tree.js` and `species.js`'s tree section: given a small node graph (id,
  children, label, tip/clickable/muted flags), it lays out and draws it,
  nothing more. Deliberately knows nothing about phylogeny, the API, or
  i18n — each caller decides which nodes to include and what a click means.
  No graph library; pure SVG built with `document.createElementNS`.
- `common.js` — shared between `map.js` and `species.js`: panel-map
  construction, the archipelago panel-group framing, the warm log-binned
  color scale, the legend, GBIF-search-link building. Factored out once both
  pages needed the same Leaflet setup rather than duplicating it.
- `lang.js` — the pt/es/en language switch, `t()`/`tPlural()` translation
  lookup, and the declarative `data-i18n*` attribute binder. Loaded by every
  page before its own script.
- `style.css` — shared header, footer, card, map-panel, and cladogram
  (`.cladogram-*`) styles across all six pages. `species.css`/`map.css`/
  `tree.css`/`rank.css`/`identify.css` hold only what's specific to that one
  page — check `style.css` first before assuming a rule needs to be added
  per-page.
- `i18n.json` — every UI string in the app, keyed by dotted key
  (`"page.title": {"en": ..., "pt": ..., "es": ...}`). See Conventions.

How they connect: every page fetches its data from the FastAPI JSON
endpoints (`/api/...`) plus a couple of static GeoJSON files
(`/iberia.geojson` for the land basemap, `/regions.geojson` for the
administrative-region choropleth, `/pt_es_border.geojson` for the
Portugal/Spain border line) and `/taxa_labels.json` (order/family
descriptions shown in the atlas). None of the frontend talks to SQLite
directly — everything goes through the API.

**Phylogeny frontend, specifically:** `tree.js` fetches the tree once
(`GET /api/phylo/root` to discover the root id — never hardcoded, see the
`phylo_nodes` schema row below — then one `GET /api/phylo/{root}/subtree`,
~2,600 raw nodes) and renders the WHOLE thing in a single pass, fully
expanded, rather than navigating into it level by level. It still collapses
single-child chains before drawing (`collapseToBranch` in `tree.js`) —
OToL's synthesis frequently inserts long single-child `mrcaottXottY` runs
with no real branching (observed directly: some species sit under 30+
nested wrappers before the next actual split, and the measured maximum
*effective* — i.e. real-branch-point — depth is 27, against a raw depth of
55), so without collapsing those the tree would be far taller and far less
legible than the branching structure actually warrants. What's left after
collapsing (~1,160 nodes: 577 resolved tips + a handful of unresolved ones +
their internal branch points) renders in ~20ms in a headless-Chrome
measurement — comfortably fast enough that no virtualization was needed.
Two features layer on top of that single render: a zoom control that only
rescales the already-drawn SVG's `width`/`height` attributes against its
fixed `viewBox` (cheap — the browser scales the painted output like an
`<img>`, no re-layout), and a search box that scrolls to and highlights a
matching tip via `scrollIntoView` + a CSS class, auto-expanding any manually
collapsed ancestor first (`expandAncestorsOf`) so the target is guaranteed
to actually be in the DOM. A clade can still be collapsed by clicking it
(`toggleCollapse`) for working with one large group at a time; the tree
simply starts fully expanded rather than starting collapsed and requiring
navigation in. A named internal node shows its name; an unnamed one shows
no placeholder text at all, on the view that the branch point itself is
already the information — `species.js`'s smaller neighbourhood view (below)
follows the same rule for consistency, even though its subtree is only
3-6 nodes. Tip labels align in a flush right-hand column regardless of how deep a
given branch runs (`renderCladogram`'s `alignTips` option in
`cladogram.js`), each connected to its real (topologically correct) branch
position by a dashed guide line — the standard published-cladogram
convention — which `species.js`'s neighbourhood view leaves off since its
depth range is already small enough not to need it.

`species.js`'s "Position in the tree of life" section fetches only the
species' own small neighbourhood: `GET /api/species/{id}/relatives` names
`clade_node_id`, the *smallest clade containing the species and every
listed relative*, which `GET /api/phylo/{clade_node_id}/subtree` then
renders. Getting that bounding node right took a real fix — see Design
decisions. Its "open in full tree" link (`tree.html?node=<clade_node_id>`)
reuses the exact same reveal-and-scroll path the search box uses, just
triggered from a URL parameter read once at load instead of user input.

## Database schema (`data/nidatlas.db`)

| Table | Columns | Purpose |
|---|---|---|
| `species` | `id`, `gbif_name` (UNIQUE), `bioclip_name`, `genus`, `family`, `"order"`, `total_occurrences`, `common_name_pt/es/en`, `image_url`, `image_source`, `image_license`, `image_attribution`, `image_source_url` | One row per Iberian bird species (584 currently). `gbif_name` is GBIF's accepted scientific name; `bioclip_name` is what BioCLIP 2's vocabulary calls it (differs when `data/taxonomy_synonyms.csv` maps one to the other). `common_name_*` are populated later by `fetch_vernacular_names.py`, nullable until then. `image_*` are populated later by `fetch_species_images.py`, nullable until then (currently 584/584 populated, but the columns stay nullable since a future added species isn't guaranteed a match from either source) — `image_url` is always a hotlinked third-party URL, never a locally-stored file (see Design decisions); `image_source` is `"inaturalist"` or `"wikidata"`; `image_license` is `"cc0"` or `"cc-by"` (never anything else — see the CC-BY-SA exclusion decision); `image_attribution` is a ready-to-display credit string in one consistent format regardless of source; `image_source_url` links to the original observation (iNaturalist) or file page (Commons) for provenance. |
| `regions` | `id`, `region_key` (UNIQUE), `name_pt/es/en`, `kind` (`district_province`/`island`/`fallback`), `source_nuts_id`, `total_occurrences` | Administrative regions: 73 mainland districts/provinces + 24 individually-named archipelago islands + 1 fallback ("Alto-mar"/"Open sea" — cells too far from any real region). `region_key` is stable across rebuilds (used for upserts); `id` is not, so never hardcode a region `id` anywhere. `total_occurrences` is precomputed by `assign_regions.py`, not computed live (see Design decisions). |
| `grid_cells` | `mgrs_cell` (PK), `centroid_lat`, `centroid_lon`, `region_id`, `region_name` | One row per MGRS 10km cell that has any occurrence data. `region_id`/`region_name` are populated by `assign_regions.py`, not `build_database.py` — a fresh DB has these columns but they're empty until you run that script. |
| `species_cell` | `species_id`, `mgrs_cell`, `occurrences`, `family_occurrences` | Species × cell occurrence counts, annual total. `family_occurrences` is the same cell's total across every species in that family — the denominator for the (now-secondary) share-of-family metric. |
| `species_cell_month` | `species_id`, `mgrs_cell`, `month`, `occurrences`, `family_occurrences` | Same as `species_cell` but split by calendar month (1-12), for the month-filtered distribution views. |
| `species_month` | `species_id`, `month`, `occurrences` | Species' total occurrences per calendar month, independent of location — drives the seasonality chart on the species page. |
| `species_year` | `species_id`, `year`, `occurrences` | Species' total occurrences per calendar year. Not currently used by any endpoint — kept for a future time-trend view. |
| `phylo_nodes` | `id`, `ott_node_label` (UNIQUE), `name`, `ott_id`, `rank`, `parent_id`, `species_id`, `is_tip`, `depth` | One row per node (leaf or internal) in the Open Tree of Life induced subtree over the atlas's species — see `fetch_phylogeny.py`/`build_phylogeny_db.py`. `ott_node_label` is OToL's own raw Newick label, kept verbatim (`mrcaott<X>ott<Y>` for an unnamed synthesis placeholder, `<Name>_ott<id>` for a named one) since roughly 5/6 of internal nodes have no real taxon name to give. `species_id` is set only on tips that resolve to one of the 584 species; **not every species has a row here** — a handful resolve to a valid OTT taxon that OToL's synthesis doesn't sample, so they simply have no placement (see `fetch_phylogeny.py`'s report for the current list). `depth` is this node's own distance from the tree root (root = 0) — see Design decisions for why this, not `phylo_closure.depth`, is what an MRCA query sorts by. `id` is a rebuild-dependent sequential id (like `regions.id`), not stable across a refetch — never hardcode one. |
| `phylo_closure` | `ancestor_id`, `descendant_id`, `depth` | Every ancestor/descendant pair in `phylo_nodes` (not just parent/child) — `depth` here is the number of edges from `ancestor_id` down to `descendant_id`, a different quantity from `phylo_nodes.depth` above. See Design decisions for why a closure table over nested sets or a materialized path. |

All the `_month`/`_year` tables and `species_cell`/`species_cell_month` are
**pre-aggregated from the raw cube**, not raw occurrence records — see
Design decisions below for why.

## Scripts, in rebuild order

Everything under `scripts/` except `identify.py` is part of the offline
build pipeline, run once (or whenever the source data changes), not at
request time. `data/` itself is gitignored — every file in it, including
`nidatlas.db`, is a local build artifact, not checked into git.

1. **`build_species_list.py`** — reads a GBIF species-list CSV export
   (`data/*.csv`, tab-delimited despite the extension), filters to
   `taxonRank == SPECIES` rows meeting `--min-occurrences` (default 50),
   resolves each name through `data/taxonomy_synonyms.csv` to whatever name
   BioCLIP 2 actually recognizes, drops anything still unrecognized by
   BioCLIP's own vocabulary, writes `data/iberian_species.txt`
   (`bioclip_name,gbif_name` per line). Needs `pybioclip` installed (it
   loads the model just to read its label vocabulary).
2. **`prepare_cube.py`** — reads the *other* GBIF download (the occurrence
   cube CSV), filters to species in `iberian_species.txt`, year ≥ 1990,
   coordinate uncertainty ≤ 10km, writes `data/cube_clean.parquet`.
3. **`build_database.py`** — reads `cube_clean.parquet` +
   `iberian_species.txt`, computes each MGRS cell's true centroid (via
   `pyproj`, reprojecting through the cell's own UTM zone — `mgrs.toLatLon`
   only gives the SW corner), wipes and rebuilds `data/nidatlas.db` from the
   `SCHEMA` string in this file, aggregates and loads every table except
   `regions`/`grid_cells.region_*` (empty until step 6).
4. **`fetch_vernacular_names.py`** — for each species, resolves a GBIF
   `usageKey` then fetches PT/ES/EN vernacular names from the live GBIF
   species API, updates `species.common_name_*`. Caches every API response
   in `data/vernacular_cache.json`, so reruns after the first are instant
   and make zero new network calls. Safe to rerun any time.
5. **Boundary/basemap geometry** (independent of steps 1-4; step 6 needs
   `iberia.geojson` already built):
   - **`build_land_polygons.py`** — downloads OSM's `land-polygons` dataset,
     clips to a generous Iberia+Macaronesia bounding box, unions and
     simplifies, writes `static/iberia.geojson`.
   - **`clip_land_to_countries.py`** — re-clips that same file to just
     Portugal+Spain using Natural Earth's admin-0 country polygons as a
     mask (OSM's land layer isn't split by country), overwrites
     `static/iberia.geojson` in place.
   - **`build_border_lines.py`** — extracts the Portugal/Spain land border
     from Natural Earth's boundary-lines dataset, writes
     `static/pt_es_border.geojson` (drawn on the species map).
6. **`build_regions.py`** — downloads GISCO NUTS3 (1:1,000,000), decomposes
   the Azores/Madeira/Ibiza-Formentera NUTS3 units into their individual
   named islands, and for every island-kind region sources its *rendered
   shape* from `static/iberia.geojson` rather than GISCO (see Design
   decisions) — GISCO is used only for naming and for locating which island
   is which. Mainland district/province shapes still come from GISCO
   directly. Writes `static/regions.geojson`. **Requires
   `static/iberia.geojson` to already exist** (step 5).
7. **`assign_regions.py`** — reads `static/regions.geojson` and
   `data/nidatlas.db`'s `grid_cells`, does point-in-polygon assignment with
   a 15km nearest-region rescue pass for cells that fall just outside a
   region polygon (see Design decisions), populates `regions` and
   `grid_cells.region_id/region_name`, precomputes
   `regions.total_occurrences`. **Requires both step 3 (DB exists) and step
   6 (`regions.geojson` exists).** Safe to rerun any time; upserts by
   `region_key`, doesn't accumulate.
8. **`fetch_phylogeny.py`** — resolves every `species.gbif_name` against the
   Open Tree of Life (TNRS `match_names`, restricted to `context_name:
   "Birds"`), then fetches the induced subtree (Newick) over the resolved
   OTT ids. A name only counts as resolved when TNRS returns exactly one
   distinct OTT taxon; genuinely ambiguous or unmatched names are reported,
   never guessed at, and corrected only via two small curated files this
   script reads (not invents): `data/taxonomy_synonyms.csv` (already
   existed for BioCLIP; OToL's taxonomy lags recent GBIF splits in the same
   way BioCLIP's vocabulary does, so it doubles as a first retry) and the
   OToL-only `data/ott_taxonomy_synonyms.csv` / `data/ott_ambiguous_resolutions.csv`
   for gaps BioCLIP's file doesn't cover. All raw API responses are cached
   under `data/opentree_*.json`, so a rerun with the same species list makes
   zero network calls. Independent of steps 1-7 except needing `species` to
   already exist (step 3). Safe to rerun any time; pass `--force` to refetch.
9. **`build_phylogeny_db.py`** — parses that cached Newick and loads
   `phylo_nodes`/`phylo_closure` (see Database schema above). **Requires
   step 8's cached `data/opentree_induced_subtree_raw.json` and
   `data/opentree_resolutions.json` to exist.** Safe to rerun any time;
   always wipes and repopulates both tables from scratch rather than
   accumulating.
10. **`fetch_species_images.py`** — fetches one canonical, commercially-usable
    photo per species: manual overrides first (`data/image_overrides.csv`,
    highest precedence — see below), then iNaturalist (`photo_license=cc0,
    cc-by`, research-grade, most-faved observation among those that pass
    `observation_is_suspect()`'s dead-specimen/taxidermy/toy/identifiable-
    person screen — see Design decisions), Wikidata/Commons (P18, CC0/CC-BY
    only) as a fallback for whatever iNaturalist misses. Currently 584/584
    species covered, entirely from iNaturalist (the Wikidata/Commons
    fallback is exercised, but wasn't actually needed on the real run). A
    manual review after that initial run found 40 species whose photo was a
    dead/taxidermied specimen or contained a toy/figurine, plus 2 more
    (caught in a later visual-review pass) whose photo showed a real bird
    but also an identifiable person's face — see "Candidate-photo
    screening" in Design decisions for the fix and
    `data/image_overrides.csv` below for the manual escape hatch used
    alongside it. Populates `species.image_*` (see
    Database schema above). Independent of steps 5-9 except needing
    `species` to already exist (step 3). Every API response is cached under
    `data/` (`inat_taxa_raw.json`, `inat_observations_raw.json` — filtered
    before caching, NOT the raw response, see Design decisions —
    `inat_observation_overrides_raw.json`, `wikidata_images_raw.json`,
    `commons_metadata_raw.json`), keyed by the exact query used, so a rerun
    only fetches names/ids not already tried — safe to rerun any time; pass
    `--force` to refetch everything, or `--refetch-species-file <path>` (one
    `gbif_name` per line) to reprocess just a named subset, excluding each
    one's current iNaturalist observation so a genuinely different photo is
    picked (used for the 40-species remediation above; see that flag's own
    `--help` text). See Design decisions for the cascade order, the CC-BY-SA
    exclusion, the hotlinking decision, and two real bugs caught building
    this (a search-endpoint false negative, and the observations-cache size
    blowup).

    **`data/image_overrides.csv`** (`gbif_name,source,inat_observation_id,
    inat_photo_id,commons_filename,note`) lets a human pin one specific
    photo for one specific species, bypassing the entire cascade above for
    that species — highest precedence, resolved before (and instead of) the
    automated search. `source` is `inaturalist` (needs
    `inat_observation_id`; `inat_photo_id` is optional — omit it to use that
    observation's first licensed photo) or `commons` (needs
    `commons_filename`, e.g. `Some_bird.jpg`, no `File:` prefix). Tracked in
    git despite living under `data/` (see Conventions' curated-files list) —
    not pre-populated with guesses, a human adds a row only after actually
    checking the candidate photo. Currently empty (header row only); use it
    for whichever of the 40 remediated species (or any future one) still
    doesn't look right after review.

**`identify.py`** is not part of this pipeline — it's a standalone CLI
(`python scripts/identify.py <image> [--species-list data/iberian_species.txt]`)
that runs BioCLIP 2 on one photo and prints the top-5 predictions. Not
called by anything else in the repo.

## Key design decisions (and why)

- **MGRS grid, not the EEA reference grid.** The EEA grid (ETRS89-based)
  doesn't cover the Macaronesian islands properly — most Azores/Madeira/
  Canaries records got no cell at all, silently losing island endemics like
  `Regulus madeirensis` and `Pyrrhula murina` from every spatial view. MGRS
  has global coverage (it's a military/NATO grid, not EU-specific), so it
  covers the islands and open-ocean pelagic records too. This was a full
  cube regeneration, not a small tweak — if you're ever asked to touch grid
  logic, check whether Macaronesia coverage is at risk before changing it.
- **Aggregated tables, not raw occurrence rows.** `species_cell`,
  `species_cell_month`, `species_month`, `species_year` all store
  pre-summed counts, not one row per GBIF occurrence record. The raw cube
  has millions of rows; the aggregated DB has ~4.6M across all tables
  combined and stays well clear of the constraints a free/low-cost hosting
  tier imposes (Azure deployment is a planned next step — see Current
  state). The tradeoff: you cannot recover per-occurrence detail (exact
  coordinates, observer, date) from this DB — if a future feature needs
  that, it has to go back to `cube_clean.parquet` or the original GBIF
  download.
- **Concentration ratio, not share-of-family, for "most/least
  characteristic species" rankings.** Share-of-family (a species'
  occurrences ÷ its family's total occurrences in the same scope) made any
  species that's the sole representative of a small family score 100% in
  *every* region it appeared in at all — so the ranking was nearly
  identical everywhere, dominated by taxonomic accidents rather than real
  geography. Concentration compares a species against **its own**
  distribution instead: `regional_share / global_share`, where both are
  that species' share of all occurrences in-scope vs. across the whole of
  Iberia. >1 means concentrated here, <1 means underrepresented, and it
  actually differentiates one region from another (verified: Madrid's and A
  Coruña's top-characteristic lists are now genuinely different species).
  Share-of-family is still computed and returned on every row — it's a
  legitimate sampling-effort normaliser — it just no longer drives the sort
  order. See `_rank_by_concentration` in `src/queries.py`.
- **`MIN_LIST_OCCURRENCES = 5`** (also in `src/queries.py`). Concentration
  is a ratio, so a single-occurrence vagrant with a large global population
  scores a concentration near zero purely from sample-size noise, not
  genuine regional absence — it would otherwise swamp the "least
  characteristic" list with statistical flukes rather than species actually
  uncommon in that region relative to their normal range. This gates only
  which species are *eligible* for the top/bottom-15 lists; it does not
  affect `total_occurrences`/`distinct_species`, which still count every
  species genuinely present. Checked against the smallest regions in the
  dataset (a 3-cell island) to confirm it never starves either list.
- **BioCLIP's label space restricted to Iberian species.**
  `TreeOfLifeClassifier`'s default vocabulary spans the entire tree of
  life — every organism it was trained on, not just birds, not just
  Iberian ones. Both `scripts/identify.py --species-list` (the CLI) and
  `src/identification.py`'s `_get_classifier()` (the `/api/identify`
  endpoint) call `create_taxa_filter` + `apply_filter` to restrict
  predictions to the ~584-species list this atlas actually covers, which
  improves both accuracy (far less chance of confusion with an unrelated
  global species) and interpretability (every prediction is guaranteed
  relevant to what the atlas documents).
- **IDENTIFY feature isolation: the model layer is importable without being
  loadable.** The feature adds ~1.7GB of model weights plus PyTorch, which
  affects container image size, cold start and memory — a deployment that
  doesn't want that cost must be able to run without it, not merely
  "without using" it. Two independent mechanisms, deliberately layered
  rather than relying on either alone: (1) `src/identification.py` never
  imports `torch`/`bioclip`/PIL at module level — every such import is
  lazy, inside `_get_classifier()`/`classify_image_bytes()` — so `api.py`
  can import this module unconditionally with zero cost when those
  packages aren't installed at all; (2) `api.py` reads `ENABLE_IDENTIFY`
  from the environment once at import time and only registers
  `POST /api/identify` and `GET /identify` inside `if ENABLE_IDENTIFY:`, so
  with the flag unset those heavy-import functions are never even called,
  and the routes genuinely don't exist (a request gets a plain 404, not a
  500 from a missing package). The frontend mirrors this at the UI layer:
  every page's nav link to `/identify` (`.nav-identify-link`) starts
  `hidden` in the static HTML and is only revealed by `applyFeatureFlags()`
  in `lang.js` after `GET /api/config` confirms `identify_enabled` — so a
  deployment with the feature off shows no trace of it, not just a
  disabled/broken-looking button. `requirements.txt` still lists
  `pybioclip` unconditionally today (it's also needed by the offline
  `scripts/identify.py` and `build_species_list.py`); splitting it into an
  optional/extra dependency group is Azure-deployment work (see Current
  state's Next list), not needed until there's an actual Dockerfile to
  make lean.
- **IDENTIFY confidence threshold: 0.5, chosen from a direct measurement,
  not a guess.** Under the same Iberian-restricted classifier, 5 real
  Iberian species photos (blackbird, house sparrow, serin, blackcap, white
  stork) scored between 0.956 and 0.998 top-1 confidence. Four images the
  classifier should NOT be confident about — a non-Iberian bird (an Emperor
  Penguin, fetched from iNaturalist, forced to pick among only Iberian
  species since the filter can't return anything else), a domestic cat, a
  screenshot of this app's own UI, and a random-noise image — scored
  between 0.14 and 0.36. `CONFIDENCE_THRESHOLD = 0.5` in
  `identification.py` sits in the wide gap between those two clusters, so
  `classify_image_bytes()`'s `confident` flag is a genuine signal, not an
  arbitrary cutoff. This directly drives the frontend's honesty
  requirement (see `identify.html`/`identify.js` above): a low-confidence
  result is shown as "could not identify with confidence", with the raw
  candidates available only behind an explicit, viscerally
  de-emphasized "show anyway" disclosure — never presented as a ranked
  answer the way a confident result is. Re-measure before changing this
  threshold if the classifier, its species list, or its restriction filter
  ever change materially (see the same "Explain before accepting on schema
  changes" convention this project already applies elsewhere).
- **`data/taxonomy_synonyms.csv`.** GBIF's current taxonomic backbone and
  BioCLIP 2's internal vocabulary disagree on naming for a number of
  species — recent genus splits, spelling variants. This CSV maps GBIF's
  accepted name to whatever name BioCLIP recognizes, so
  `build_species_list.py` can still include a species that would otherwise
  look "unknown to BioCLIP" purely from a naming mismatch. A handful of
  species have no such mapping (BioCLIP genuinely lumps them into an older,
  broader parent taxon, or has no equivalent at all) — see README's Known
  limitations for the exact list; this is a real, disclosed data gap, not
  a bug to silently fix.
- **15km coastal rescue threshold** (`RESCUE_THRESHOLD_KM` in
  `assign_regions.py`). GISCO's administrative boundaries are coarser than
  the OSM coastline the map itself uses, so a real coastal grid cell's
  centroid can legitimately fall just outside the simplified region
  polygon. Diagnosed directly before picking a value: of cells with no
  containing polygon, 93% of the affected occurrence-value sat within 5km
  of the nearest region and 98.4% within 15km — a steep natural cliff, not
  a smooth falloff — and a species-level sanity check confirmed cells
  rescued within 15km are dominated by common land/urban birds (centroid
  just missing the coarse boundary), while cells still unassigned beyond
  15km are genuinely pelagic seabirds. Don't change this threshold without
  redoing that same diagnostic; it's not an arbitrary round number.
- **Island region shapes come from OSM (`iberia.geojson`), not GISCO.**
  Related to the point above but a separate fix: even at GISCO's finest
  published NUTS3 scale (1:1,000,000), its own polygons overlap real land
  by only 31% of Desertas' own area and 56% of Selvagens' (measured
  directly by intersecting GISCO's polygon for each island against the
  OSM-derived land layer already used for this app's basemap — see
  `scripts/build_regions.py`'s module docstring for the full breakdown,
  including the even coarser 1:3,000,000 GISCO product this project used
  before upgrading to 01M). `build_regions.py` now sources every
  island-kind region's *rendered shape* from the precise OSM-derived land
  layer instead,
  matched to the correct named region via the same nearest-reference-point
  logic previously used only to decompose GISCO's own geometry. GISCO is
  still authoritative for mainland district shapes (only GISCO has the
  internal administrative boundary lines between neighbouring districts —
  there's no OSM equivalent to swap in there).
- **Schema duplication between `build_database.py` and every script that
  enriches an existing table.** `build_database.py`'s `SCHEMA` string is the
  canonical definition of every table, but four other scripts each carry
  their own second copy of the columns/tables they populate, behind an
  `ensure_schema()`/`ensure_columns()` using `CREATE TABLE IF NOT EXISTS`/
  `ALTER TABLE ... ADD COLUMN`: `assign_regions.py`
  (`regions`/`grid_cells.region_id`/`region_name`), `build_phylogeny_db.py`
  (`phylo_nodes`/`phylo_closure`), `fetch_vernacular_names.py`
  (`species.common_name_*`), and `fetch_species_images.py`
  (`species.image_*`). Each stays safely re-runnable against a database
  that already has real data, without requiring a full `build_database.py`
  rebuild first (which would also throw away every other script's
  populated data). Nothing enforces these definitions agree — if you change
  a table's shape in `build_database.py`, you must update the matching
  `ensure_schema()`/`ensure_columns()` by hand in whichever script(s) also
  touch that table, or they will silently drift apart.
- **Phylogeny storage: adjacency list + closure table, not nested sets or a
  materialized path** (`phylo_nodes.parent_id` + `phylo_closure`). Chosen
  against the four query patterns `src/queries.py`'s `phylo_*` functions
  support: MRCA-of-two-species is the deciding one — one indexed self-join
  on `phylo_closure` (common ancestors of both, take the one with the
  greatest `phylo_nodes.depth`) matches this project's plain-SQL style,
  where nested sets would need range-containment logic and a materialized
  path would need longest-common-prefix logic. Descendants-of-a-node and
  closest-relatives both reduce to a single indexed range scan on
  `phylo_closure`; subtree rendering uses `phylo_nodes.parent_id` directly
  since a closure table alone can't reconstruct topology. The tree is tiny
  (~2,600 nodes) and, like `grid_cells`/`regions`, is rebuilt from scratch
  every pipeline run rather than mutated live, so the closure table's usual
  weakness (expensive to keep in sync under inserts) never applies — same
  precompute-once, serve-cheaply pattern already used for
  `regions.total_occurrences`. **Do not confuse `phylo_nodes.depth`**
  (a node's own distance from the tree root) **with `phylo_closure.depth`**
  (the distance from one specific ancestor down to one specific
  descendant) — MRCA needs the former; using the latter by mistake silently
  returns the tree ROOT as the "closest" common ancestor for any pair
  instead of their actual most specific one (caught during this feature's
  own testing, not theoretical — see `phylo_mrca` in `src/queries.py`).
- **Not every species has a phylogeny placement.** Of the 584 species, 577
  resolve to a distinct node in the current Open Tree synthesis
  (`opentree16.1`); 7 resolve cleanly to a real, valid OTT taxon that no
  input phylogeny happens to sample, so OToL's `induced_subtree` endpoint
  either folds them into an ancestor placeholder or (for one, flagged
  `"hidden"` in OToL's own taxonomy) refuses the id outright. This is a
  genuine, disclosed gap in Open Tree's current data, not a fetch bug —
  `phylo_closest_relatives`/`phylo_mrca` treat "species exists but has no
  tree placement" as a valid non-error outcome (empty list / a clearly
  labeled `ValueError`), never a guessed-at placement. See
  `fetch_phylogeny.py`'s own report (rerun it) for the current, exact list.
- **`phylo_closest_relatives`'s "distance" is a raw node-hop count, not an
  ultrametric measure — so "bound the whole neighbourhood" needs the MRCA
  of the FULL set, not just the two farthest-apart members.** Caught while
  building the tree-view frontend: `GET /api/species/{id}/relatives` used
  to compute `clade_node_id` (the node bounding a species' shown
  neighbourhood, used both to fetch that neighbourhood as a subtree and to
  link "open in full tree") as `phylo_mrca(species, farthest_listed_relative)`.
  That's unsafe, because OToL's single-child synthesis chains (see the
  storage bullet above) inflate hop-count on some branches but not others —
  concretely, among Turdus merula's 6 closest relatives by this metric, the
  MRCA with the *farthest*-ranked one (Turdus naumanni) excluded two
  *nearer*-ranked ones (Turdus philomelos, Turdus viscivorus) entirely,
  since their shared ancestor with merula sat outside naumanni's. Fixed by
  `phylo_mrca_of_node_ids` in `src/queries.py`: the true MRCA of every node
  in the set at once (a `phylo_closure` join grouped by ancestor, keeping
  only ancestors common to ALL of them, deepest wins) — see
  `test_species_relatives_finds_congeneric_species` in `tests/test_api.py`,
  which asserts `clade_node_id` contains every listed relative, not just
  the last one, specifically to keep this fixed.
- **Rank view: competition ranking (`RANK()`), not `ROW_NUMBER()`, with
  `gbif_name` as an alphabetical tiebreak.** `species_ranking` in
  `src/queries.py` orders by `RANK() OVER (ORDER BY total_occurrences DESC)`
  specifically so species tied on the exact same count (real ties exist —
  e.g. two species both sit at exactly 4 occurrences) get the SAME rank
  number, matching the same `RANK()` already used for a species' own
  "X of 584" figure on its own page (`species_profile`'s `global_rank`). A
  plain `ROW_NUMBER()` would instead silently imply one of two identically-
  recorded species is "more recorded" than the other, which isn't true —
  see `test_species_ranking_ties_share_the_same_rank_number` in
  `tests/test_api.py`. `gbif_name` breaks ties only in the SQL `ORDER BY`
  (row iteration order, for deterministic/reproducible API responses), not
  in the rank NUMBER itself — two tied species still show the same `#N`.
- **Cladogram rendering: pure SVG via `document.createElementNS`, no graph
  library, one render pass rather than incremental layout.** `cladogram.js`
  is deliberately minimal — given a small node graph (id, children, label,
  tip/clickable/muted flags) it lays out and draws it, nothing more,
  knowing nothing about phylogeny, the API, or i18n (see the Frontend
  section above). This was viable specifically because the full tree
  (~1,160 nodes after collapsing single-child chains) measured at ~20ms to
  render in a real headless-Chrome test — a library (D3, or a dedicated
  tree/graph package) would have added a real dependency to solve a
  performance problem this project doesn't actually have. If a future
  change makes the node count much larger (e.g. rendering un-collapsed,
  or a much bigger species list), re-measure before assuming this still
  holds — the design explicitly trades "handles arbitrary scale" for
  "simple and fast at this scale."
- **Species photo cascade: iNaturalist first, Wikidata/Commons only as a
  fallback — decided from a measurement, not defaulted to.** Before
  building `fetch_species_images.py`, a 50-species sample (spanning common
  mainland birds, scarce mainland species and island endemics) was queried
  against both sources. Wikidata had a P18 image for 98% of the sample, but
  after filtering to commercially-usable licences that dropped to 40% — 59%
  of Wikidata's own bird images turned out to be CC-BY-SA, the single
  largest licence bucket there (bigger than CC-BY, far bigger than CC0).
  iNaturalist, filtered server-side to `photo_license=cc0,cc-by` and
  research-grade observations, hit **100%** coverage on the same sample,
  including every island endemic and a genuinely local specialist
  (*Chersophilus duponti*, Dupont's Lark, with only 25 qualifying
  observations worldwide at measurement time). iNaturalist was chosen as
  the PRIMARY source (not just the higher-coverage one) for a second,
  independent reason: its photos are field observations in natural
  habitat, visually consistent with each other, whereas Wikidata's P18
  often points at a mixed bag of studio, museum-specimen or captive-bird
  photos — a worse fit for an occurrence atlas. Mixing the two roughly
  40/60 would have bought no extra coverage (iNaturalist alone already
  covered the sample) at the cost of an inconsistent visual identity across
  the atlas, so Wikidata/Commons is used ONLY for whatever iNaturalist
  still misses on the full 584-species run. **CC-BY-SA is excluded from
  BOTH sources**, not merely deprioritized — viral copyleft is incompatible
  with a possible future commercial deployment, and the 59% figure above is
  the measured, deliberate cost of that choice, not an oversight. See
  ATTRIBUTIONS.md for the same figures recorded against the site's actual
  licence policy, and `fetch_species_images.py`'s own module docstring for
  the full writeup. The full run confirmed the sample's prediction: **584/584
  species got an image from iNaturalist alone** (71 CC0, 513 CC-BY); the
  Wikidata/Commons fallback path was exercised zero times on the real data,
  not just rarely — it still earns its place in the code for whatever future
  species join the atlas with sparser iNaturalist coverage, but it did no
  work this run. 8 species failed on the first pass; investigating each one
  by hand (not just re-trying blindly) found 2 were a bug in this project's
  own matching code — `/v1/taxa`'s free-text search buried an exact match
  outside its top 10 results for names like *Bubo bubo* even though the
  taxon exists and is active, fixed by switching to `/v1/taxa/autocomplete`,
  which is built for exact-name lookups and doesn't have this problem — and
  6 were genuine GBIF-vs-iNaturalist taxonomy disagreements (recent genus
  splits, a spelling difference, and one species GBIF/Wikidata rank as a
  full species that iNaturalist's active taxonomy instead nests as a
  subspecies — see `data/image_source_synonyms.csv` for the resolved list
  and each one's specific reasoning). Both fixes were verified against the
  live API and confirmed with the user before being applied, not guessed at.
- **Candidate-photo screening: dead-specimen/taxidermy/toy/identifiable-
  person detection is a real filter on WHICH observation gets picked, not
  just documentation.** A manual review of the first 584-species run found
  40 species (see the git commit that introduced this screening for the
  exact list) whose photo showed a dead/taxidermied specimen or contained a
  toy/figurine rather than a live bird in the field — the "most-faved
  research-grade observation" heuristic alone doesn't protect against
  either, since a striking taxidermy shot or a novelty toy photo can still
  collect real faves. Fixed two ways in `observation_is_suspect()`, checked
  before ranking (not after — an excluded observation is never a candidate
  at all, not merely deprioritized): (1) iNaturalist's own structured
  "Alive or Dead" controlled-term annotation (`GET /v1/controlled_terms`
  attribute id 17, value id 19 = "Dead") when present — reliable where it
  exists, since it's a human annotation, not inferred; (2) a word-boundary
  keyword regex (`SUSPECT_KEYWORDS_RE`) over the observation's description
  + tags, covering both dead-specimen language (taxidermy, museum,
  specimen, mounted, roadkill, ...) and toy/figurine language (toy,
  figurine, decoy, replica, statue, ...), since plenty of specimen photos
  are never annotated and toy photos have no "alive or dead" annotation to
  begin with (they're not an organism at all). This is a heuristic, not a
  guarantee — a photo with no description, no tags and no annotation sails
  through regardless of what it shows — which is exactly why
  `data/image_overrides.csv` (see the `fetch_species_images.py` pipeline
  step above) exists alongside it as the human-verified escape hatch, not
  instead of it. Ties are now broken by `identifications_count` (more
  independent agreement the ID itself is correct) ahead of the
  observation id, on the theory that an observation several people have
  separately confirmed is marginally more likely to be a clean, correctly
  identified photo than one nobody but the uploader has weighed in on —
  a secondary signal, not load-bearing on its own.

  A second, later pass through the SAME 40-species remediation added a
  third check, `PEOPLE_KEYWORDS_RE`: an identifiable-person keyword
  (person, child, selfie, portrait, ...) in the description/tags also
  excludes a candidate now. This is a genuinely different concern from
  everything above — CC0/CC-BY clears the PHOTO's own copyright, it says
  nothing about the separate image/likeness rights of a person who happens
  to be IN the photo, and this project has no consent from any such person
  to publish their picture. Caught the same way as the dead/toy cases: by
  actually looking at the photo, not by this filter — both
  `Columba livia`'s and `Sitta europaea`'s automated re-picks during the
  40-species remediation put an identifiable person's face (a child's, in
  one case) prominently in frame, with nothing in either observation's
  text for a keyword screen to have caught. Both are now pinned via
  `data/image_overrides.csv` to a re-checked photo showing only the bird.
  `PEOPLE_KEYWORDS_RE` is added going forward as a best-effort net over the
  minority of observations that DO carry descriptive text, exactly as
  limited as `SUSPECT_KEYWORDS_RE` above — it is not a substitute for
  visually checking a candidate before trusting it.
- **`fetch_species_images.py`'s observation cache is filtered before being
  cached, not the literal raw API response, despite this file's usual
  convention of caching responses verbatim.** Caught the hard way: the
  first real 584-species run was killed after several hours once its
  `data/inat_observations_raw.json` reached 2.5GB and was still growing —
  each cached iNaturalist observation embeds a full nested taxon (with its
  own ancestor chain and a Wikipedia summary paragraph), user, comments and
  identifications, none of which this script ever reads, and the
  checkpoint save re-serializes the WHOLE accumulated cache every 20
  species, so the cost compounds (effectively O(n²) over the run). Fixed by
  `_slim_observation()`: keep only the observation id, its fave count, and
  each photo's id/url/licence/attribution before caching. Final cache size
  for all 584 species: ~23MB, not gigabytes. If you ever see this cache
  file growing unexpectedly large again, this is the first thing to check.
  Separately, the SAME long run also died once outright to a plain
  `TimeoutError` on one HTTP request (network hiccups are simply expected
  over a ~30-minute run against two external APIs) — `_get_json` retries
  transient failures (timeouts, connection errors, 429, 5xx) with backoff
  now; a genuine 4xx still raises immediately rather than retrying
  something that will never succeed.
- **`species.image_attribution` is stored (and shown) in English only, even
  though the rest of the UI is pt/es/en.** It's a factual credit
  line (photographer name + a standard licence abbreviation like "CC BY"),
  not prose — the same category of string as "OSM"/"BioCLIP 2" elsewhere in
  this file, which nothing in the app translates either. iNaturalist's own
  API returns this field in English regardless of the requester's locale,
  so matching that for the Wikidata/Commons fallback (rather than
  hand-rolling three-language attribution phrasing for a legal credit
  line) keeps both sources' output genuinely uniform, not just
  superficially so. Only the surrounding UI chrome around it (a "view
  original" link, aria-labels) is localised normally.
- **Two display forms of the same attribution, not two stored values.**
  `buildPhotoCredit` in `lang.js` (shared by `app.js`, `rank.js`) takes an
  `options.compact` flag: the atlas grid packs far more cards per screen
  than the rank list or a species page, so it renders a shorter `"Photo:
  {name}, (CC BY)"` there instead of the full `"Photo: {name}, some rights
  reserved (CC BY)"` sentence — but both are derived client-side from the
  SAME `image_attribution`/`image_license` fields (`photographerNameFrom`
  just extracts the name back out of the full sentence), never a second
  fetched/stored value. The full sentence is always still in the element's
  `title`/aria-label regardless of which form is visibly shown. Rank rows
  keep the full sentence despite being nearly as compact as atlas cards —
  a deliberate choice, not an oversight, since only the atlas view was
  reported as needing the extra space.
- **Species photos are hotlinked, never downloaded.** `species.image_url`
  always points at iNaturalist's own CDN (`inaturalist-open-data.s3.
  amazonaws.com`) or Wikimedia's `upload.wikimedia.org`, not a local copy
  under `static/`. Checked before deciding this, not assumed: Wikimedia
  Commons' own documentation explicitly allows hotlinking to
  `upload.wikimedia.org` (`Commons:Reusing content outside Wikimedia/
  technical`) — it only discourages it, purely because a file can later be
  renamed, replaced or deleted upstream with no notice to a hotlinker (a
  reliability tradeoff accepted here, not a licensing one). iNaturalist's
  API guidance instead cautions against heavy *downloading* specifically
  (bulk media fetches over 5GB/hour or 24GB/day risk a permanent API
  block) — hotlinking each photo through their own CDN on every page view
  is the lighter-weight, policy-aligned choice, not a workaround of it.
  Either way, the photo's own licence still requires attribution regardless
  of hotlink vs. local copy, which is why `image_attribution` is built once
  at fetch time in one format for both sources — `"Photo: {name}, some
  rights reserved (CC BY)"` / `"Photo: {name}, no rights reserved (CC0)"`
  (`build_display_attribution` in `fetch_species_images.py`) — the frontend
  just displays the string, no per-source branching needed. This is NOT
  iNaturalist's own raw `photo["attribution"]` string: that one omits the
  photographer entirely for a CC0 photo (just `"no rights reserved"`, no
  name), so the name is read from the observation's own uploader instead
  (`user.name`, falling back to their `login`) for both licences, and the
  same fixed template is used regardless of source.
- **A CSS Grid item's automatic minimum size is its content's min-content
  size — for a grid item containing an `<img>`, that's the image's own
  INTRINSIC size, regardless of any `width`/`aspect-ratio` set on the
  image itself.** Caught live: once real photos replaced the empty
  placeholder `<span>`s on the atlas cards, the grid's columns stopped
  being uniform — some cards were visibly wider than others, and the whole
  grid could overflow horizontally. Root cause wasn't the image's own CSS
  (`.card-thumb`'s `width:100%; aspect-ratio:1/1; object-fit:cover` was
  already correct and, measured directly, DID render each image as a
  perfect square) — it was `.species-card` (the actual grid item)'s
  automatic minimum width being floored by its child image's intrinsic
  pixel size (e.g. 1024px for iNaturalist's "large" size) under CSS Grid's
  default `minmax(auto, 1fr)` track sizing. Fixed with `min-width: 0` on
  `.species-card` — the standard, documented fix for this exact class of
  bug — plus `max-width: 100%` on `.card-thumb` for defense-in-depth. If a
  future grid/flex layout starts showing uneven tracks or unexplained
  horizontal overflow right after adding an image (or any other
  intrinsically-sized replaced element) to a grid/flex item, this is the
  first thing to check.

## Conventions

- **English everywhere in the repository** — code, comments, commit
  messages, docs. The *product* is trilingual (pt/es/en, see
  `static/i18n.json`); the codebase that builds it is English-only.
- **Conventional commits** (`feat:`, `fix:`, `chore:`, ...) — check `git
  log` for the established style before committing.
- **`static/i18n.json` is the single source of truth for every UI
  string.** Never hardcode user-facing text in a `.js` file or inline in
  HTML outside a `data-i18n`/`data-i18n-placeholder`/`data-i18n-aria-label`
  attribute or a `t()`/`tPlural()` call. Adding a string means adding a key
  there first, in all three languages, then referencing that key.
- **`data/` is gitignored as a whole, but small hand-curated decision files
  inside it are deliberately tracked anyway**, via explicit `!data/...`
  negations in `.gitignore`: `taxonomy_synonyms.csv`,
  `ott_taxonomy_synonyms.csv`, `ott_ambiguous_resolutions.csv`,
  `image_source_synonyms.csv`, `image_overrides.csv`. The principle: everything in `data/` that a
  script can *derive* (the built SQLite DB, cached API responses, the
  cleaned cube) stays out of git, since it's regenerable from source and
  would just bloat the repo — but a file that records a *human decision*
  (which OTT taxon to pick for a genuinely ambiguous name match, which
  alternate name a source currently uses for a species) is exactly the
  kind of thing that CAN'T be re-derived by rerunning a script, so it's
  tracked like any other source file. If you add a new curated
  gbif_name-to-something mapping file for a future data source, add its
  own negation line here too — don't assume `data/*` will let it through.
- **Explain before accepting on schema changes.** Every DB schema change in
  this project's history was preceded by a concrete before/after diagnosis
  (a query, a measured count, a sample) shown to the user, not just applied
  because it seemed reasonable — see "Key design decisions" above for what
  that looked like in practice. Keep doing that: propose the change, show
  the evidence, get a decision, then implement.
- **Diagnose before fixing, especially for anything geometry/data-related.**
  Several rounds of this project's history involved a reported symptom that
  turned out to have a different root cause than the obvious guess (see the
  region-map fixes in git history/this file's design decisions). Measure
  first.
- **Windows environment.** Development happens on Windows via PowerShell
  and Git Bash. Watch for: PowerShell-vs-bash path syntax when a script
  needs an absolute path argument; default console encoding is cp1252, so
  printing accented characters (á, ã, ç, ...) without
  `sys.stdout.reconfigure(encoding='utf-8')` shows up as mojibake in
  terminal output — this is a display artifact only, not data corruption,
  but it's worth ruling out explicitly rather than assuming real corruption.

## Current state

**Done:** the full data pipeline (species list → cube → SQLite →
vernacular names → administrative regions → phylogeny → species photos),
the query layer and FastAPI backend with a 50-test pytest suite, and all
six frontend pages (species atlas grid landing page, region map, species
detail page, tree of life view, occurrence-count ranking, photo
identification) fully implemented and localized in pt/es/en with shared
top-level ATLAS/MAP/TREE/RANK/IDENTIFY navigation, in that order. Every species now has a
real, commercially-usable, attributed photo (584/584 — see the "Species
photo cascade" design decision) shown on its atlas card, its own page and
the rank lists; a manual review afterward found 40 species with an
unsuitable (dead-specimen/taxidermy/toy) photo and, in a later visual pass,
2 more with an identifiable person's face (a child's, in one case)
prominently in frame — all now both screened against automatically going
forward (`observation_is_suspect()`, covering dead-specimen/taxidermy/toy
AND identifiable-person cases) and fixable by hand via
`data/image_overrides.csv` — see the "Candidate-photo screening" design
decision. BioCLIP 2 identification is wired into the web app as the
IDENTIFY view (`POST /api/identify`, `static/identify.html`), sharing its
Iberian-species restriction with the standalone `scripts/identify.py` CLI,
gated end-to-end behind `ENABLE_IDENTIFY` so a deployment can still run
without the model/PyTorch installed at all — see "IDENTIFY feature
isolation" and "IDENTIFY confidence threshold" in Design decisions.
Phylogeny specifically: 577/584 species are
placed in an Open Tree of Life-derived tree stored in
`phylo_nodes`/`phylo_closure`, with `src/queries.py` functions for all four
query patterns (closest relatives, MRCA, descendants-of-a-node, subtree
rendering); `tree.html` renders the full tree as a navigable rectangular
cladogram (`GET /api/phylo/root` + `GET /api/phylo/{id}/subtree`), and
`species.html` shows each species' own local neighbourhood
(`GET /api/species/{id}/relatives`) in the same style, both via the shared
`static/cladogram.js` renderer.

**In progress:** nothing actively broken. As of this file's writing, a
substantial amount of work — the entire region map, the concentration-ratio
ranking change, this rename, the phylogeny feature end to end, the Tree of
Life and Rank views, and the species-photo pipeline (fetch, schema, and
frontend wiring across atlas cards/species pages/rank rows) — is complete
and passing tests but **not yet committed to git** — run `git status`
before assuming the working tree matches the last commit.

**Next** (not yet started, roughly in this order):
1. **Azure deployment.** Docker, GitHub Actions CI/CD, and picking an
   actual Azure hosting target. Nothing in the repo currently deploys
   anywhere; everything described above runs locally only. Also the point
   at which `src/api.py`'s `no-store` `Cache-Control` middleware (a
   local-dev convenience — see Architecture above) needs revisiting for
   real caching, and at which `requirements.txt` would actually benefit
   from splitting `pybioclip`/PyTorch into an optional extra now that
   ENABLE_IDENTIFY makes that split meaningful for a lightweight
   container (see "IDENTIFY feature isolation" in Design decisions).
2. Species description text — real photos are now wired in (see
   `fetch_species_images.py` / the "Species photo cascade" design
   decision), but there is still no descriptive text field anywhere.
3. A private user sightings log — letting a user record their own
   observations, geographically. This was part of the *original* product
   vision (see README's earlier drafts) but has no schema, endpoint, or UI
   yet, and would need real auth/user-identity design first.

## Running it locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The repo ships without `data/` or the generated `static/*.geojson` files
populated (they're gitignored / build artifacts) — either restore your own
`data/nidatlas.db` + `static/regions.geojson`/`iberia.geojson`/
`pt_es_border.geojson`, or rebuild from scratch following "Scripts, in
rebuild order" above (you'll need the two GBIF CSV downloads yourself; see
README.md for the DOIs).

Once `data/nidatlas.db` exists:

```powershell
python src/api.py
```

Serves on `http://127.0.0.1:8000/` (the species atlas). `/map` is the
region map. Static files are served with `Cache-Control: no-store`, so a
plain reload always reflects the latest files on disk — no need to hard-refresh
during development.

The IDENTIFY view is off by default. To turn it on, `pip install
pybioclip` (already in `requirements.txt`) and set `ENABLE_IDENTIFY=1`
before starting the server:

```powershell
$env:ENABLE_IDENTIFY = "1"
python src/api.py
```

With the flag unset (or any value other than `1`/`true`/`yes`), the app
runs exactly as before — no IDENTIFY nav link, `/identify` and
`/api/identify` don't exist, and nothing in `src/identification.py`'s heavy
imports ever gets touched — see "IDENTIFY feature isolation" in Design
decisions.

## Tests

```powershell
python -m pytest tests/ -q
```

Requires `data/nidatlas.db` to exist and be fully built (species +
regions + grid_cells assignment + phylogeny all populated) — the test suite
hits the real FastAPI app against the real local database, not a mock. 50
tests, should all pass on a correctly rebuilt database. `tests/conftest.py`
sets `ENABLE_IDENTIFY=1` before any test module imports `src/api.py` (that
flag is read once at import time — see "IDENTIFY feature isolation" in
Design decisions), so `tests/test_identify.py`'s routes exist regardless of
which test file pytest happens to import `api` from first;
`tests/test_identify.py` mocks `identification.classify_image_bytes`
itself, so the suite never loads the real model and doesn't need
`pybioclip`/PyTorch installed to run.
