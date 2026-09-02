import concurrent.futures
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import queries  # noqa: E402
from api import app  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "nidatlas.db"


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="session")
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def madeirensis_id(client: TestClient) -> int:
    response = client.get("/api/species", params={"q": "Regulus madeirensis"})
    return response.json()[0]["id"]


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.requires_full_dataset
def test_search_finds_merula(client: TestClient) -> None:
    response = client.get("/api/species", params={"q": "merula"})
    assert response.status_code == 200
    results = response.json()
    assert len(results) > 0
    assert all("merula" in r["gbif_name"].lower() for r in results)


def test_search_query_too_long_gives_422(client: TestClient) -> None:
    # q has a 100-char max_length (see api.py's api_search_species) so an
    # unbounded search string can't force a 5-column LIKE scan over
    # arbitrarily long input.
    response = client.get("/api/species", params={"q": "a" * 101})
    assert response.status_code == 422


def test_unknown_species_id_gives_404(client: TestClient) -> None:
    response = client.get("/api/species/999999")
    assert response.status_code == 404


def test_invalid_month_gives_422(client: TestClient) -> None:
    response = client.get("/api/zones/28S/month/13")
    assert response.status_code == 422


def test_invalid_mgrs_prefix_gives_422(client: TestClient) -> None:
    response = client.get("/api/zones/lowercase")
    assert response.status_code == 422


@pytest.mark.requires_full_dataset
def test_zone_totals_are_internally_consistent(client: TestClient) -> None:
    response = client.get("/api/zones/28S")
    assert response.status_code == 200
    summary = response.json()
    top_species_total = sum(r["occurrences"] for r in summary["top_species"])
    assert top_species_total <= summary["total_occurrences"]


@pytest.mark.requires_full_dataset
def test_zone_top_species_are_ranked_by_concentration_not_family_share(client: TestClient) -> None:
    # 28S is Madeira's MGRS zone. Regulus madeirensis (Madeira Firecrest) is
    # a Madeira endemic that should rank at or near the top by concentration
    # (it is essentially absent everywhere else in Iberia) -- under the old
    # share-of-family ranking this slot was instead dominated by whichever
    # species happened to be the sole member of a small family, which wasn't
    # necessarily true anywhere in particular.
    response = client.get("/api/zones/28S")
    summary = response.json()
    top_names = [r["gbif_name"] for r in summary["top_species"]]
    assert "Regulus madeirensis" in top_names

    for r in summary["top_species"]:
        assert "concentration" in r
        assert "share" in r  # share-of-family kept in the data model, just not the ranking key

    concentrations = [r["concentration"] for r in summary["top_species"]]
    assert concentrations == sorted(concentrations, reverse=True)

    bottom_concentrations = [r["concentration"] for r in summary["bottom_species"]]
    assert bottom_concentrations == sorted(bottom_concentrations)
    # The single lowest-concentration species overall must be at least as
    # low as anything in the top list -- bottom_species and top_species
    # should never overlap in a region with more than 30 species.
    assert bottom_concentrations[0] <= concentrations[-1]


@pytest.mark.requires_full_dataset
def test_zone_bottom_species_meet_minimum_occurrence_threshold(client: TestClient) -> None:
    # Regression test: before MIN_LIST_OCCURRENCES, the bottom list surfaced
    # single-record vagrants (occurrences=1) whose concentration is near
    # zero purely from sample-size noise, not genuine underrepresentation.
    response = client.get("/api/zones/28S")
    summary = response.json()
    for r in summary["bottom_species"] + summary["top_species"]:
        assert r["occurrences"] >= 5


@pytest.mark.requires_full_dataset
def test_species_cell_count_matches_db(
    client: TestClient, db_conn: sqlite3.Connection, madeirensis_id: int
) -> None:
    response = client.get(f"/api/species/{madeirensis_id}/cells")
    assert response.status_code == 200
    cells = response.json()

    expected = db_conn.execute(
        "SELECT COUNT(*) FROM species_cell WHERE species_id = ?", (madeirensis_id,)
    ).fetchone()[0]
    assert len(cells) == expected


@pytest.mark.requires_full_dataset
def test_species_cells_month_filter_returns_subset(client: TestClient, madeirensis_id: int) -> None:
    all_response = client.get(f"/api/species/{madeirensis_id}/cells")
    month_response = client.get(f"/api/species/{madeirensis_id}/cells", params={"month": 1})
    assert month_response.status_code == 200

    all_cells = {c["mgrs_cell"] for c in all_response.json()}
    month_cells = {c["mgrs_cell"] for c in month_response.json()}
    assert month_cells
    assert month_cells <= all_cells


@pytest.mark.requires_full_dataset
def test_all_species_includes_vernacular_name_columns(client: TestClient) -> None:
    response = client.get("/api/species/all")
    assert response.status_code == 200
    species = response.json()
    assert len(species) == 584
    for key in ("common_name_pt", "common_name_es", "common_name_en"):
        assert key in species[0]


@pytest.mark.requires_full_dataset
def test_all_species_dex_number_is_a_stable_1_to_584_sequence(client: TestClient) -> None:
    response = client.get("/api/species/all")
    species = response.json()
    dex_numbers = [s["dex_number"] for s in species]
    assert dex_numbers == list(range(1, 585))

    # stable across repeated calls, not just a valid permutation on one call
    again = client.get("/api/species/all").json()
    assert [s["dex_number"] for s in again] == dex_numbers
    assert [s["id"] for s in again] == [s["id"] for s in species]


@pytest.mark.requires_full_dataset
def test_species_ranking_covers_all_species_sorted_by_occurrences_desc(client: TestClient) -> None:
    response = client.get("/api/species/ranking")
    assert response.status_code == 200
    ranking = response.json()
    assert len(ranking) == 584

    counts = [r["total_occurrences"] for r in ranking]
    assert counts == sorted(counts, reverse=True)
    for key in ("id", "gbif_name", "rank", "common_name_pt", "common_name_es", "common_name_en"):
        assert key in ranking[0]


@pytest.mark.requires_full_dataset
def test_species_ranking_ties_share_the_same_rank_number(client: TestClient) -> None:
    # RANK() (not ROW_NUMBER()): species tied on the same total_occurrences
    # must show the same rank, not an arbitrary tie-broken position -- e.g.
    # two species tied at 4 occurrences shouldn't be silently labelled #583
    # and #584 as if one were more recorded than the other.
    ranking = client.get("/api/species/ranking").json()
    by_count: dict[int, set[int]] = {}
    for r in ranking:
        by_count.setdefault(r["total_occurrences"], set()).add(r["rank"])

    tied_counts = {count: ranks for count, ranks in by_count.items() if len([r for r in ranking if r["total_occurrences"] == count]) > 1}
    assert tied_counts  # sanity: the real data does contain ties
    for count, ranks in tied_counts.items():
        assert len(ranks) == 1, f"species tied at {count} occurrences have differing ranks: {ranks}"


@pytest.mark.requires_full_dataset
def test_species_ranking_rank_matches_species_profile_global_rank(
    client: TestClient, madeirensis_id: int
) -> None:
    ranking = client.get("/api/species/ranking").json()
    entry = next(r for r in ranking if r["id"] == madeirensis_id)

    profile = client.get(f"/api/species/{madeirensis_id}").json()
    assert entry["rank"] == profile["global_rank"]["rank"]


@pytest.mark.requires_full_dataset
def test_all_species_includes_image_columns_with_valid_shape(client: TestClient) -> None:
    # Doesn't assert exact coverage numbers -- fetch_species_images.py hits
    # live external APIs, so how many of the 584 end up with a photo can
    # shift between reruns. What must always hold is the SHAPE: the columns
    # exist on every row, and whenever image_url is set, the rest of that
    # row's image_* fields are internally consistent with it.
    species = client.get("/api/species/all").json()
    for key in ("image_url", "image_source", "image_license", "image_attribution", "image_source_url"):
        assert key in species[0]

    with_image = [s for s in species if s["image_url"]]
    assert with_image  # sanity: the fetch pipeline has actually run and found at least some images
    for s in with_image:
        assert s["image_license"] in ("cc0", "cc-by")  # never cc-by-sa or anything else -- see CLAUDE.md
        assert s["image_source"] in ("inaturalist", "wikidata")
        assert s["image_attribution"]
        assert s["image_source_url"]
        if s["image_source"] == "inaturalist":
            assert "inaturalist" in s["image_url"]
        else:
            assert "wikimedia.org" in s["image_url"]


@pytest.mark.requires_full_dataset
def test_species_profile_includes_image_fields(client: TestClient, madeirensis_id: int) -> None:
    profile = client.get(f"/api/species/{madeirensis_id}").json()
    for key in ("image_url", "image_source", "image_license", "image_attribution", "image_source_url"):
        assert key in profile


@pytest.mark.requires_full_dataset
def test_species_ranking_includes_image_fields(client: TestClient) -> None:
    ranking = client.get("/api/species/ranking").json()
    for key in ("image_url", "image_source", "image_license", "image_attribution", "image_source_url"):
        assert key in ranking[0]


@pytest.mark.requires_full_dataset
def test_search_finds_species_by_portuguese_name(client: TestClient) -> None:
    response = client.get("/api/species", params={"q": "Melro"})
    assert response.status_code == 200
    results = response.json()
    assert any(r["gbif_name"] == "Turdus merula" for r in results)


@pytest.mark.requires_full_dataset
def test_search_finds_species_by_spanish_name(client: TestClient) -> None:
    response = client.get("/api/species", params={"q": "Mirlo"})
    assert response.status_code == 200
    results = response.json()
    assert any(r["gbif_name"] == "Turdus merula" for r in results)


@pytest.mark.requires_full_dataset
def test_species_profile_includes_dex_number_and_vernacular_names(client: TestClient) -> None:
    response = client.get("/api/species/566")
    assert response.status_code == 200
    profile = response.json()
    assert profile["gbif_name"] == "Turdus merula"
    assert 1 <= profile["dex_number"] <= 584
    for key in ("common_name_pt", "common_name_es", "common_name_en"):
        assert key in profile
    assert profile["global_rank"]["total"] == 584

    # dex_number must match all_species()'s numbering for the same species --
    # one source of truth (ROW_NUMBER over order/family/gbif_name), not two
    # independently-computed sequences that could drift apart.
    all_species = client.get("/api/species/all").json()
    matching = next(s for s in all_species if s["id"] == 566)
    assert matching["dex_number"] == profile["dex_number"]


@pytest.fixture(scope="session")
def madrid_region_id(client: TestClient) -> int:
    regions = client.get("/api/regions").json()
    return next(r["id"] for r in regions if r["name_en"] == "Madrid")


@pytest.mark.requires_full_dataset
def test_list_regions_returns_districts_islands_and_offshore(client: TestClient) -> None:
    response = client.get("/api/regions")
    assert response.status_code == 200
    regions = response.json()
    assert len(regions) > 0
    kinds = {r["kind"] for r in regions}
    assert "district_province" in kinds
    assert "island" in kinds
    # The offshore/pelagic fallback is a first-class, browsable region on the
    # region map (shown as its own cells rather than a polygon -- see
    # test_offshore_region_cells_endpoint_returns_points below), not hidden
    # from the list the way it was before the region map existed.
    assert "fallback" in kinds
    for r in regions:
        for key in ("id", "region_key", "name_pt", "name_es", "name_en", "kind",
                     "total_occurrences", "cell_count"):
            assert key in r


@pytest.mark.requires_full_dataset
def test_list_regions_includes_individual_azores_islands(client: TestClient) -> None:
    regions = client.get("/api/regions").json()
    names = {r["name_en"] for r in regions}
    # Acores is a single NUTS3 unit but must appear here decomposed into its
    # real islands, not as one "Azores" blob (see scripts/build_regions.py)
    assert {"Corvo", "Flores", "Faial", "Pico", "Terceira", "Graciosa",
            "São Jorge", "São Miguel", "Santa Maria"} <= names


def test_unknown_region_id_gives_404(client: TestClient) -> None:
    response = client.get("/api/regions/999999")
    assert response.status_code == 404


@pytest.mark.requires_full_dataset
def test_region_summary_totals_are_internally_consistent(
    client: TestClient, madrid_region_id: int
) -> None:
    response = client.get(f"/api/regions/{madrid_region_id}")
    assert response.status_code == 200
    summary = response.json()
    assert summary["name_en"] == "Madrid"
    assert summary["cell_count"] > 0
    top_species_total = sum(r["occurrences"] for r in summary["top_species"])
    assert top_species_total <= summary["total_occurrences"]

    for r in summary["top_species"]:
        assert "concentration" in r
        assert "share" in r
    concentrations = [r["concentration"] for r in summary["top_species"]]
    assert concentrations == sorted(concentrations, reverse=True)


@pytest.mark.requires_full_dataset
def test_region_summary_top_species_differ_between_regions(
    client: TestClient, madrid_region_id: int
) -> None:
    # Regression test for the share-of-family ranking this replaced: a
    # species that's the sole member of a small family always scored 100%
    # share in any region it appeared in at all, which made "most
    # characteristic" nearly identical everywhere. Concentration compares a
    # species against its own distribution instead, so two unrelated
    # regions' top-characteristic species should not be identical.
    regions = client.get("/api/regions").json()
    a_coruna_id = next(r["id"] for r in regions if r["name_en"] == "A Coruña")

    madrid_top = {r["gbif_name"] for r in client.get(f"/api/regions/{madrid_region_id}").json()["top_species"]}
    coruna_top = {r["gbif_name"] for r in client.get(f"/api/regions/{a_coruna_id}").json()["top_species"]}
    assert madrid_top != coruna_top


@pytest.mark.requires_full_dataset
def test_region_summary_month_filter_returns_subset(
    client: TestClient, madrid_region_id: int
) -> None:
    all_response = client.get(f"/api/regions/{madrid_region_id}")
    month_response = client.get(f"/api/regions/{madrid_region_id}", params={"month": 1})
    assert month_response.status_code == 200
    assert month_response.json()["month"] == 1

    all_total = all_response.json()["total_occurrences"]
    month_total = month_response.json()["total_occurrences"]
    assert 0 < month_total <= all_total


@pytest.mark.requires_full_dataset
def test_region_invalid_month_gives_422(client: TestClient, madrid_region_id: int) -> None:
    # Marked despite being a pure validation check: it depends on the
    # madrid_region_id fixture, which needs a real "Madrid" region to exist.
    response = client.get(f"/api/regions/{madrid_region_id}", params={"month": 13})
    assert response.status_code == 422


@pytest.mark.requires_full_dataset
def test_offshore_fallback_region_is_listed_and_queryable(client: TestClient) -> None:
    regions = client.get("/api/regions").json()
    offshore = next(r for r in regions if r["kind"] == "fallback")
    assert offshore["name_en"] == "Open sea"
    assert offshore["name_pt"] == "Alto-mar"
    assert offshore["name_es"] == "Alta mar"

    response = client.get(f"/api/regions/{offshore['id']}")
    assert response.status_code == 200
    summary = response.json()
    assert summary["kind"] == "fallback"
    assert summary["cell_count"] > 0


@pytest.mark.requires_full_dataset
def test_offshore_region_summary_via_db_lookup(
    client: TestClient, db_conn: sqlite3.Connection
) -> None:
    offshore_id = db_conn.execute(
        "SELECT id FROM regions WHERE kind = 'fallback'"
    ).fetchone()[0]
    response = client.get(f"/api/regions/{offshore_id}")
    assert response.status_code == 200
    summary = response.json()
    assert summary["kind"] == "fallback"
    assert summary["cell_count"] > 0


@pytest.mark.requires_full_dataset
def test_region_cells_endpoint_matches_cell_count(
    client: TestClient, madrid_region_id: int
) -> None:
    response = client.get(f"/api/regions/{madrid_region_id}/cells")
    assert response.status_code == 200
    cells = response.json()
    summary = client.get(f"/api/regions/{madrid_region_id}").json()
    assert len(cells) == summary["cell_count"]
    for cell in cells:
        for key in ("mgrs_cell", "centroid_lat", "centroid_lon", "occurrences"):
            assert key in cell


@pytest.mark.requires_full_dataset
def test_offshore_region_cells_endpoint_returns_points(
    client: TestClient, db_conn: sqlite3.Connection
) -> None:
    # The offshore/"Alto-mar" region has no polygon (see static/regions.geojson,
    # which only covers real administrative units), so the region map draws it
    # as its individual cells instead -- this endpoint is what makes that
    # possible, unlike every other region which is drawn as a filled polygon.
    offshore_id = db_conn.execute("SELECT id FROM regions WHERE kind = 'fallback'").fetchone()[0]
    response = client.get(f"/api/regions/{offshore_id}/cells")
    assert response.status_code == 200
    cells = response.json()
    assert len(cells) == 5016


def test_unknown_region_id_cells_gives_404(client: TestClient) -> None:
    response = client.get("/api/regions/999999/cells")
    assert response.status_code == 404


@pytest.mark.requires_full_dataset
def test_grid_cells_region_assignment_matches_regions_table(
    db_conn: sqlite3.Connection,
) -> None:
    # Every grid_cells row must have a region assigned (including the
    # offshore fallback) -- none left NULL -- and region_name must match
    # what the regions table says for that region_id, not just be some
    # independently-set string that could drift out of sync. Marked despite
    # passing vacuously on an empty database (0 rows trivially satisfies
    # "0 mismatched") -- that vacuous pass verifies nothing, so this only
    # has real meaning against the real dataset.
    mismatched = db_conn.execute(
        """
        SELECT COUNT(*) FROM grid_cells gc
        JOIN regions r ON r.id = gc.region_id
        WHERE gc.region_name != r.name_en
        """
    ).fetchone()[0]
    assert mismatched == 0

    unassigned = db_conn.execute(
        "SELECT COUNT(*) FROM grid_cells WHERE region_id IS NULL"
    ).fetchone()[0]
    assert unassigned == 0


@pytest.mark.requires_full_dataset
def test_concurrent_requests_do_not_break_the_db_connection(client: TestClient) -> None:
    # Regression test: get_db() used to open sqlite3 without check_same_thread=False.
    # FastAPI runs a sync generator dependency's setup and teardown as separate
    # threadpool calls, which can land on different worker threads under real
    # concurrent load even though a single request never uses the connection
    # from two threads at once -- sqlite3's same-thread check rejected that.
    # Sequential requests (the rest of this test file) never exercised this.
    def hit(_: int) -> int:
        return client.get("/api/species/566").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(hit, range(30)))

    assert results == [200] * 30


# --- Phylogeny (data/phylo_nodes, data/phylo_closure -- see
# scripts/fetch_phylogeny.py and scripts/build_phylogeny_db.py) ---


@pytest.fixture(scope="session")
def phylo_root_id(db_conn: sqlite3.Connection) -> int:
    return db_conn.execute("SELECT id FROM phylo_nodes WHERE parent_id IS NULL").fetchone()[0]


@pytest.mark.requires_full_dataset
def test_phylo_root_matches_the_only_parentless_node(
    client: TestClient, phylo_root_id: int
) -> None:
    # The frontend must discover the root through this endpoint, never by
    # hardcoding an id -- phylo_nodes.id is rebuild-dependent (see CLAUDE.md).
    response = client.get("/api/phylo/root")
    assert response.status_code == 200
    body = response.json()
    assert body["node_id"] == phylo_root_id
    assert body["name"] is not None  # the root happens to be a named taxon (Neognathae)


@pytest.mark.requires_full_dataset
def test_species_relatives_finds_congeneric_species(
    client: TestClient, db_conn: sqlite3.Connection
) -> None:
    # 566 = Turdus merula (Common Blackbird, see
    # test_species_profile_includes_dex_number_and_vernacular_names above).
    # Turdus iliacus (Redwing) is another Turdus species in the atlas, so it
    # should show up among the very closest relatives by tree topology.
    # limit=6 matches what species.js actually requests -- deliberately not
    # a bigger number, since limit=6 is exactly the scenario that caught a
    # real bug (see phylo_mrca_of_node_ids's docstring): among Turdus
    # merula's 6 closest relatives by raw distance, the farthest-ranked one
    # (Turdus naumanni) does NOT sit in the same branch as two nearer-ranked
    # ones (Turdus philomelos, Turdus viscivorus), so their MRCA excludes
    # those two entirely -- only the true MRCA of the whole set is safe.
    response = client.get("/api/species/566/relatives", params={"limit": 6})
    assert response.status_code == 200
    body = response.json()
    assert body["species_id"] == 566

    expected_node_id = db_conn.execute("SELECT id FROM phylo_nodes WHERE species_id = 566").fetchone()[0]
    assert body["node_id"] == expected_node_id
    assert body["clade_node_id"] is not None

    relatives = body["relatives"]
    assert len(relatives) == 6
    assert any(r["gbif_name"] == "Turdus iliacus" for r in relatives)
    assert any(r["gbif_name"] == "Turdus philomelos" for r in relatives)
    assert any(r["gbif_name"] == "Turdus viscivorus" for r in relatives)

    distances = [r["distance"] for r in relatives]
    assert distances == sorted(distances)
    for r in relatives:
        assert r["species_id"] != 566  # never lists the species itself
        assert isinstance(r["node_id"], int)

    # clade_node_id must actually be an ancestor of the species AND of
    # EVERY listed relative -- not just the farthest-ranked one (that
    # narrower check is exactly what let the bug above slip through).
    descendant_ids = [expected_node_id] + [r["node_id"] for r in relatives]
    for descendant_id in descendant_ids:
        is_ancestor = db_conn.execute(
            "SELECT 1 FROM phylo_closure WHERE ancestor_id = ? AND descendant_id = ?",
            (body["clade_node_id"], descendant_id),
        ).fetchone()
        assert is_ancestor is not None, f"clade_node_id does not contain node {descendant_id}"


def test_species_relatives_unknown_species_gives_404(client: TestClient) -> None:
    response = client.get("/api/species/999999/relatives")
    assert response.status_code == 404


@pytest.mark.requires_full_dataset
def test_species_relatives_species_with_no_tree_placement_returns_empty(client: TestClient) -> None:
    # 289 = Himantopus himantopus, one of the species TNRS resolves to a
    # valid OTT taxon that OToL's synthesis doesn't sample -- see
    # fetch_phylogeny.py's report. The species is real (not a 404), it just
    # has no placement in this tree, so this must be an empty 200, not an error.
    response = client.get("/api/species/289/relatives")
    assert response.status_code == 200
    body = response.json()
    assert body == {"species_id": 289, "node_id": None, "clade_node_id": None, "relatives": []}


@pytest.mark.requires_full_dataset
def test_phylo_subtree_root_contains_every_placed_species(
    client: TestClient, db_conn: sqlite3.Connection, phylo_root_id: int
) -> None:
    response = client.get(f"/api/phylo/{phylo_root_id}/subtree")
    assert response.status_code == 200
    subtree = response.json()
    assert subtree["root_id"] == phylo_root_id

    root_node = next(n for n in subtree["nodes"] if n["id"] == phylo_root_id)
    assert root_node["parent_id"] is None
    assert root_node["depth"] == 0

    tip_species_ids = {n["species_id"] for n in subtree["nodes"] if n["is_tip"] and n["species_id"] is not None}
    expected = db_conn.execute("SELECT COUNT(*) FROM phylo_nodes WHERE species_id IS NOT NULL").fetchone()[0]
    assert len(tip_species_ids) == expected


@pytest.mark.requires_full_dataset
def test_phylo_subtree_of_a_genus_clade_is_smaller_than_the_whole_tree(
    client: TestClient, db_conn: sqlite3.Connection, phylo_root_id: int
) -> None:
    # There's no single node literally named "Turdus" in this induced
    # subtree -- OToL only labels an internal node when the requested tips
    # happen to exactly span a taxon it already recognizes, which a
    # same-genus PAIR rarely does on its own (see build_phylogeny_db.py's
    # module docstring on unnamed mrca placeholders) -- so the Turdus clade
    # here is reached via its two species' own MRCA instead of a name lookup.
    turdus_node_id = queries.phylo_mrca(db_conn, 566, 565)["node_id"]  # Turdus merula, Turdus iliacus
    response = client.get(f"/api/phylo/{turdus_node_id}/subtree")
    assert response.status_code == 200
    subtree = response.json()

    full_tree = client.get(f"/api/phylo/{phylo_root_id}/subtree").json()
    assert 0 < subtree["node_count"] < full_tree["node_count"]

    tip_names = {n["gbif_name"] for n in subtree["nodes"] if n["is_tip"] and n["gbif_name"]}
    assert "Turdus merula" in tip_names
    assert "Turdus iliacus" in tip_names


def test_phylo_subtree_unknown_node_gives_404(client: TestClient) -> None:
    response = client.get("/api/phylo/999999999/subtree")
    assert response.status_code == 404


@pytest.mark.requires_full_dataset
def test_phylo_mrca_of_congeneric_species_is_deeper_than_root(
    db_conn: sqlite3.Connection, phylo_root_id: int
) -> None:
    # Direct queries.py test (MRCA isn't one of the two patterns exposed via
    # the API, but it's one of the four required query functions).
    mrca = queries.phylo_mrca(db_conn, 566, 565)  # Turdus merula, Turdus iliacus
    assert mrca["depth"] > 0  # a real, more specific ancestor than the tree root
    assert mrca["node_id"] != phylo_root_id


def test_phylo_mrca_of_species_with_no_placement_raises(db_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        queries.phylo_mrca(db_conn, 289, 566)  # 289 = Himantopus himantopus, unplaced


@pytest.mark.requires_full_dataset
def test_phylo_descendant_species_of_turdus_clade_matches_subtree_tips(
    db_conn: sqlite3.Connection,
) -> None:
    # Direct queries.py test (descendant-listing isn't one of the two
    # patterns exposed via the API here, but it's one of the four required).
    turdus_node_id = queries.phylo_mrca(db_conn, 566, 565)["node_id"]  # Turdus merula, Turdus iliacus
    descendants = queries.phylo_descendant_species(db_conn, turdus_node_id)
    names = {d["gbif_name"] for d in descendants}
    assert "Turdus merula" in names
    assert "Turdus iliacus" in names
    assert len(descendants) == len(names)  # no duplicate species rows


def test_phylo_descendant_species_unknown_node_raises(db_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        queries.phylo_descendant_species(db_conn, 999999999)


@pytest.mark.requires_full_dataset
def test_species_page_route_returns_server_rendered_metadata(
    client: TestClient, madeirensis_id: int
) -> None:
    response = client.get(f"/species/{madeirensis_id}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Regulus madeirensis" in body  # substituted into <title> and the JSON-LD block
    assert f'<link rel="canonical" href="https://nidatlas.com/species/{madeirensis_id}">' in body
    assert 'property="og:image"' in body
    assert '"@type": "Taxon"' in body


def test_species_page_route_unknown_id_gives_404(client: TestClient) -> None:
    response = client.get("/species/999999")
    assert response.status_code == 404


def test_legacy_species_html_query_string_redirects_to_clean_path(client: TestClient) -> None:
    response = client.get("/species.html", params={"id": 1}, follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/species/1"


def test_legacy_species_html_with_no_id_gives_404(client: TestClient) -> None:
    response = client.get("/species.html", follow_redirects=False)
    assert response.status_code == 404


@pytest.mark.requires_full_dataset
def test_species_page_local_assets_are_root_absolute_not_relative(
    client: TestClient, madeirensis_id: int
) -> None:
    # Regression test for a real bug caught live the first time this route
    # shipped: /species/{id} is a TWO-segment path, unlike every other page
    # in this app (/, /map, /tree, /rank -- all one segment). A relative
    # asset reference like href="style.css" resolves against a two-segment
    # path's OWN directory ("/species/"), not the site root -- the page
    # returned 200 with the right server-rendered <title>, but every local
    # JS/CSS 422'd as /species/lang.js, /species/species.js etc, so
    # species.js never actually ran and the page stayed on "Loading
    # species..." forever. See CLAUDE.md's "A relative asset path breaks
    # once a page is served from a nested URL" design decision.
    body = client.get(f"/species/{madeirensis_id}").text
    for tag in (
        'href="/style.css"', 'href="/species.css"', 'src="/lang.js"',
        'src="/common.js"', 'src="/cladogram.js"', 'src="/species.js"',
    ):
        assert tag in body, f"expected a root-absolute {tag!r} in the served species page"
    for relative in ('href="style.css"', 'src="lang.js"', 'src="species.js"'):
        assert relative not in body, f"found a relative asset reference {relative!r} that would 404/422 under /species/<id>"


def test_species_js_derives_its_id_from_the_url_path(client: TestClient) -> None:
    # Regression test for the species.html?id=<id> -> /species/<id> URL
    # migration: species.js used to read the id ONLY from the query string
    # (new URLSearchParams(location.search).get("id")). Every real
    # /species/<id> link now carries no ?id= at all, so a served species.js
    # that still only checked location.search would derive a null id on
    # every real page and show "No species specified" -- exactly the
    # user-reported symptom this test guards against. This project has no
    # browser/JS test runner (see CLAUDE.md's "no build step, no framework"
    # frontend philosophy), so this asserts directly on the served source
    # rather than actually executing it -- the closest available proxy for
    # "the species page actually loads its data via the new route".
    source = client.get("/species.js").text
    assert "location.pathname" in source
    assert re.search(r"pathMatch\s*\?\s*pathMatch\[1\]\s*:\s*params\.get\(\"id\"\)", source), (
        "species.js must derive state.id from a /species/<id> path match, "
        "falling back to the legacy ?id= query string, not the other way around"
    )


@pytest.mark.requires_full_dataset
def test_phylo_neighbourhood_prunes_a_widely_scattered_relatives_set(
    client: TestClient, db_conn: sqlite3.Connection
) -> None:
    # 390 = Otis tarda (Great Bustard) -- measured directly against this
    # database as one of several species whose closest relatives by raw
    # tree-hop distance sit in a sparsely-sampled corner of the Open Tree
    # synthesis, dragging their MRCA up to a node whose FULL subtree covers
    # 507 of the tree's 577 tips (phylo_subtree(mrca) would return nearly
    # the whole tree). This endpoint must prune that down to just the
    # species, its closest relatives, and the branch points connecting
    # them -- see phylo_species_neighbourhood's own docstring.
    response = client.get("/api/species/390/phylo-neighbourhood", params={"limit": 6})
    assert response.status_code == 200
    body = response.json()
    assert body["species_id"] == 390
    assert body["node_id"] is not None
    assert body["clade_node_id"] is not None

    nodes = body["nodes"]
    tips = [n for n in nodes if n["is_tip"]]
    assert len(tips) < 30  # nowhere near the 507-tip full-MRCA-subtree blowup this replaces
    assert any(n["species_id"] == 390 for n in tips)  # the species' own tip is always present

    summaries = [n for n in tips if n.get("is_summary")]
    assert summaries  # Otis tarda's real branch points DO have excluded, collapsed side-branches
    real_ids = {n["id"] for n in nodes}
    for s in summaries:
        assert s["excluded_species_count"] > 0
        assert s["parent_id"] in real_ids  # attaches to a real backbone node in this same response

    # clade_node_id must be a genuine ancestor of the species' own tip --
    # sanity-checks that the endpoint didn't just echo back an unrelated id.
    is_ancestor = db_conn.execute(
        "SELECT 1 FROM phylo_closure WHERE ancestor_id = ? AND descendant_id = ?",
        (body["clade_node_id"], body["node_id"]),
    ).fetchone()
    assert is_ancestor is not None


def test_phylo_neighbourhood_unknown_species_gives_404(client: TestClient) -> None:
    response = client.get("/api/species/999999/phylo-neighbourhood")
    assert response.status_code == 404
