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
island. A BioCLIP 2-based photo identification script exists as a
standalone CLI tool; it is not yet wired into the web app.

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
directly (`StaticFiles` mounted at `/`, with two explicit routes for `/` and
`/atlas` since a same-directory HTML file under a different name needs an
explicit route — `StaticFiles(html=True)` only auto-serves `index.html`).
Runs a `no-store` `Cache-Control` middleware on every response — this is a
local/dev-oriented choice (see the comment in `api.py`), revisit before a
real public deployment where you'd want real caching back.

**Frontend** (`static/`, no build step, no framework, no bundler — plain
`<script>` tags): three pages sharing common CSS/JS.
- `map.html` + `map.css` + `map.js` — the landing page (served at `/`). The
  97-region choropleth, shaded by total occurrences, with an archipelago
  panel selector and a side panel showing per-region species rankings.
- `atlas.html` + `app.js` — served at `/atlas`. The full 584-species card
  grid, grouped by order/family, with search.
- `species.html` + `species.js` + `species.css` — one species' profile:
  identity, seasonality chart (pure SVG), and its own distribution map.
- `common.js` — shared between `map.js` and `species.js`: panel-map
  construction, the archipelago panel-group framing, the warm log-binned
  color scale, the legend, GBIF-search-link building. Factored out once both
  pages needed the same Leaflet setup rather than duplicating it.
- `lang.js` — the pt/es/en language switch, `t()`/`tPlural()` translation
  lookup, and the declarative `data-i18n*` attribute binder. Loaded by every
  page before its own script.
- `style.css` — shared header, footer, card, and map-panel styles across all
  three pages. `species.css`/`map.css` hold only what's specific to that one
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

## Database schema (`data/nidatlas.db`)

| Table | Columns | Purpose |
|---|---|---|
| `species` | `id`, `gbif_name` (UNIQUE), `bioclip_name`, `genus`, `family`, `"order"`, `total_occurrences`, `common_name_pt/es/en` | One row per Iberian bird species (584 currently). `gbif_name` is GBIF's accepted scientific name; `bioclip_name` is what BioCLIP 2's vocabulary calls it (differs when `data/taxonomy_synonyms.csv` maps one to the other). `common_name_*` are populated later by `fetch_vernacular_names.py`, nullable until then. |
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
  Iberian ones. `scripts/identify.py --species-list` calls
  `create_taxa_filter` + `apply_filter` to restrict predictions to the
  ~584-species list this atlas actually covers, which improves both
  accuracy (far less chance of confusion with an unrelated global species)
  and interpretability (every prediction is guaranteed relevant to what the
  atlas documents).
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
- **Schema duplication between `build_database.py` and both
  `assign_regions.py` and `build_phylogeny_db.py`.** `build_database.py`'s
  `SCHEMA` string is the canonical definition of every table, but both of
  the other two scripts carry their own second copy of the tables they
  populate (`regions`/`grid_cells.region_id`/`region_name` for the former,
  `phylo_nodes`/`phylo_closure` for the latter) behind an `ensure_schema()`
  using `CREATE TABLE IF NOT EXISTS`/`ALTER TABLE ... ADD COLUMN`, so each
  stays safely re-runnable against a database that already has real data,
  without requiring a full `build_database.py` rebuild first (which would
  also throw away the other one's populated data). Nothing enforces these
  definitions agree — if you change a table's shape in `build_database.py`,
  you must update the matching `ensure_schema()` by hand in whichever
  script(s) also touch that table, or they will silently drift apart.
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
vernacular names → administrative regions → phylogeny), the query layer and
FastAPI backend with a 39-test pytest suite, and all three frontend pages
(region map landing page, species atlas grid, species detail page) fully
implemented and localized in pt/es/en with shared top-level MAP/ATLAS
navigation. The BioCLIP 2 identification script works standalone but is
**not yet wired into the web app** — there is no upload-a-photo flow in the
UI yet, despite the README's original description mentioning one.
Phylogeny is data-and-API-only so far: 577/584 species are placed in an
Open Tree of Life-derived tree stored in `phylo_nodes`/`phylo_closure`,
with `src/queries.py` functions for all four query patterns (closest
relatives, MRCA, descendants-of-a-node, subtree rendering) and two of them
exposed as endpoints (`/api/species/{id}/relatives`,
`/api/phylo/{node_id}/subtree`) — but no frontend tree view consumes them
yet (see **Next** below).

**In progress:** nothing actively broken. As of this file's writing, a
substantial amount of work (the entire region map, the concentration-ratio
ranking change, this rename, and the phylogeny data/query layer) is
complete and passing tests but **not yet committed to git** — run `git
status` before assuming the working tree matches the last commit.

**Next** (not yet started):
- The actual phylogenetic tree *view* in the UI — the data and query layer
  behind it are done (see **Done** above), but nothing in `static/` renders
  it yet.
- Species description text and real photos (every species card currently
  shows a placeholder thumbnail; there is no description field anywhere).
- A private user sightings log — letting a user record their own
  observations, geographically. This was part of the *original* product
  vision (see README's earlier drafts) but has no schema, endpoint, or UI
  yet, and would need real auth/user-identity design first.
- Azure deployment: Docker, GitHub Actions CI/CD, and picking an actual
  Azure hosting target. Nothing in the repo currently deploys anywhere;
  everything described above runs locally only.

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

Serves on `http://127.0.0.1:8000/` (the region map). `/atlas` is the
species list. Static files are served with `Cache-Control: no-store`, so a
plain reload always reflects the latest files on disk — no need to hard-refresh
during development.

## Tests

```powershell
python -m pytest tests/test_api.py -q
```

Requires `data/nidatlas.db` to exist and be fully built (species +
regions + grid_cells assignment + phylogeny all populated) — the test suite
hits the real FastAPI app against the real local database, not a mock. 39
tests, should all pass on a correctly rebuilt database.
