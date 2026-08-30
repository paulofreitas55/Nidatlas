# Nidatlas

A web atlas of the birds of the Iberian Peninsula — Portugal and Spain,
including the Azores, Madeira, the Canary Islands and the Balearics — built
on public GBIF occurrence data.

## What it does

- **Region map** (the landing page) — every district, province and named
  island shaded by total bird occurrences, with a side panel showing which
  species are most and least characteristic of the region you click, and a
  month filter.
- **Species atlas** — all ~584 Iberian bird species as a browsable,
  searchable card grid grouped by taxonomic order and family.
- **Species pages** — a seasonality chart and a distribution map (down to a
  10km grid cell) for any individual species, including archipelago-aware
  views for island endemics.
- Fully localised in **Portuguese, Spanish and English**.

A BioCLIP 2-based photo identification script (`scripts/identify.py`)
exists and works standalone from the command line, but is not yet wired
into the web app — uploading a photo through the site isn't possible yet.
See [CLAUDE.md](CLAUDE.md)'s "Current state" section for what's built vs.
planned (a phylogenetic tree view, species descriptions/photos, a personal
sightings log, and deployment are the next pieces of work).

## Tech stack

- **Backend:** Python, [FastAPI](https://fastapi.tiangolo.com/), SQLite (no
  ORM — plain `sqlite3` + hand-written SQL in `src/queries.py`)
- **Data pipeline:** pandas, pyarrow, shapely, pyproj — see
  [CLAUDE.md](CLAUDE.md) for the full script-by-script rebuild order
- **Species identification:** [BioCLIP 2](https://github.com/Imageomics/bioclip-2) via `pybioclip`
- **Frontend:** vanilla JavaScript, no framework or build step, [Leaflet](https://leafletjs.com/) for mapping
- **Tests:** pytest, against a real FastAPI app + real local database

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`data/` and the generated `static/*.geojson` files are not included in the
repository (gitignored build artifacts). To run the app locally you need to
either obtain a copy of `data/nidatlas.db` and the built GeoJSON files, or
rebuild everything from scratch from the GBIF downloads below — see
[CLAUDE.md](CLAUDE.md) for the exact script order. Once the database
exists:

```powershell
python src/api.py
```

Serves at `http://127.0.0.1:8000/`. Run the tests with:

```powershell
python -m pytest tests/test_api.py -q
```

## Data sources

Full provenance, licences and required attributions for every external
dataset, model and library this project depends on are tracked in
**[ATTRIBUTIONS.md](ATTRIBUTIONS.md)**. Summary of the two GBIF downloads
this project is built on:

**Species list.** GBIF.org (27 August 2026) GBIF Occurrence Download
https://doi.org/10.15468/dl.sddbky

Covers bird species of Portugal and Spain. Includes only CC0 and CC-BY
licensed records.

**Occurrence cube.** GBIF.org (28 August 2026) GBIF Occurrence Download
https://doi.org/10.15468/dl.kb6cwg

Species × year-month × MGRS 10km grid, for birds of Portugal and Spain.
Includes only CC0 and CC-BY licensed records, with family-level counts for
sampling-bias normalisation.

**Country boundaries.** `static/iberia.geojson` is built in two stages.
`scripts/build_land_polygons.py` clips OpenStreetMap's
[land-polygons](https://osmdata.openstreetmap.de/data/land-polygons.html)
dataset (coastline-derived, ODbL 1.0, © OpenStreetMap contributors) to the
study bounding box. This replaced two earlier Natural Earth-based attempts
(1:50m, then 1:10m admin-0 boundaries) that kept silently dropping small
islands — Porto Santo, Corvo, Graciosa, and, even at 1:10m, the uninhabited
Desertas, Selvagens and La Graciosa (Canaries) reserves — since OSM's land
polygons are coastline-derived rather than country-bounded, so they include
every islet regardless of habitation status. But that same property means
the bbox-clipped output also fuses mainland Iberia into France, Andorra and
a sliver of Morocco/Western Sahara, since OSM doesn't split land by country.
`scripts/clip_land_to_countries.py` removes that: it intersects the OSM
land against Natural Earth's admin-0 Portugal + Spain polygons (public
domain, used purely as a clip mask) and re-simplifies the result, keeping
OSM's fine coastline detail (e.g. the Galician rias) everywhere except the
new inland France/Andorra edge.

**Administrative regions.** `static/regions.geojson` (the 73
districts/provinces + 24 named islands shown on the region map) is built by
`scripts/build_regions.py` from Eurostat GISCO's NUTS3 boundaries (naming
and mainland shapes) combined with the OSM land layer above (island
shapes — GISCO's own island geometry is too coarse to align with the
coastline the map actually draws). See [CLAUDE.md](CLAUDE.md) for why.

## Known limitations

- **Taxonomy version mismatch.** GBIF's current backbone and BioCLIP 2's internal vocabulary disagree on naming for a number of species (recent genus splits, spelling variants). This is resolved via `data/taxonomy_synonyms.csv`, which maps GBIF names to the BioCLIP-recognized equivalent used at inference time.
- **Genus-lumping cases.** For 4 species, BioCLIP has no equivalent for a recent species-level split and only recognizes the older, broader parent taxon. Predictions for these will surface the parent name, not the split:
  - `Circus hudsonius` → `Circus cyaneus`
  - `Strix mauritanica` → `Strix aluco`
  - `Cecropis rufula` → `Cecropis daurica`
  - `Calonectris borealis` → `Calonectris diomedea`
- **`Ardea brachyrhyncha` excluded.** No BioCLIP-recognized equivalent could be resolved for this species, confidently or otherwise, so it is dropped from `data/iberian_species.txt` rather than mapped to a guess.
- **The 50-occurrence threshold is a tunable product decision, not a fixed rule.** `scripts/build_species_list.py --min-occurrences` defaults to 50 to exclude vagrants and data noise, but this cutoff was chosen as a reasonable starting point, not derived from any formal analysis — revisit it as the atlas's species coverage needs evolve.
- **Grid choice: EEA reference grid replaced with MGRS.** The initial occurrence cube used the EEA reference grid (ETRS89-based, continental Europe coverage), which fails to assign a cell to most records from the Macaronesian islands (Azores, Madeira, Canaries) — leaving island endemics like `Regulus madeirensis` and `Pyrrhula murina` without any spatial data at all. The cube was regenerated using the MGRS 10km grid, which has global coverage, including pelagic records.
- **7 species have no placement in the phylogenetic tree.** All 584 species resolve to a valid Open Tree of Life taxon (`scripts/fetch_phylogeny.py`), but 7 of those taxa aren't sampled by any input phylogeny in synthesis `opentree16.1`, so they have no tip in `phylo_nodes`. Six are folded into an ancestor placeholder node by the `induced_subtree` endpoint itself:
  - `Calandrella brachydactyla`
  - `Himantopus himantopus`
  - `Oenanthe hispanica`
  - `Porphyrio porphyrio`
  - `Spatula cyanoptera` (matched as `Anas cyanoptera`)
  - `Tyto alba`

  The seventh, `Emberiza rustica`, is more severe: its exact taxon match is flagged `"hidden"` in Open Tree's own taxonomy, so `induced_subtree` refuses the id outright rather than folding it into a placeholder. `src/queries.py`'s phylogeny functions treat all 7 as a valid "no tree placement" outcome (empty relatives list, not a 404), never a guessed-at position. See `scripts/fetch_phylogeny.py`'s own report for the up-to-date list if the resolution is ever rerun.
- **No caching in production terms.** The API currently sends `Cache-Control: no-store` on every response, a deliberate dev-convenience choice (see `src/api.py`) that should be revisited before any real deployment.
- **Not yet deployed anywhere.** Everything above runs locally only — there is no Docker image, CI pipeline, or hosting target configured yet.
