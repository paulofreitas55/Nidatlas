# Nidario

Nidario is a web atlas of birds of the Iberian Peninsula. It lets users browse bird sightings across the region and explore a species atlas, alongside user-facing features.

Users can upload a photo to get the most likely species, browse the species atlas, and geographically log where the sighting occurred.

## Architecture

_(placeholder — to be filled once the stack is implemented: FastAPI, BioCLIP 2, Azure SQL, Docker, GitHub Actions)_

## Setup

Create and activate the virtual environment, then install dependencies:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Data sources

**Species list.** GBIF.org (27 August 2026) GBIF Occurrence Download https://doi.org/10.15468/dl.sddbky

Covers bird species of Portugal and Spain. Includes only CC0 and CC-BY licensed records.

**Occurrence cube.** GBIF.org (28 August 2026) GBIF Occurrence Download https://doi.org/10.15468/dl.kb6cwg

Species × year-month × MGRS 10km grid, for birds of Portugal and Spain. Includes only CC0 and CC-BY licensed records, with family-level counts for sampling-bias normalisation.

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
