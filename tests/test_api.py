import concurrent.futures
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

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


def test_search_finds_merula(client: TestClient) -> None:
    response = client.get("/api/species", params={"q": "merula"})
    assert response.status_code == 200
    results = response.json()
    assert len(results) > 0
    assert all("merula" in r["gbif_name"].lower() for r in results)


def test_unknown_species_id_gives_404(client: TestClient) -> None:
    response = client.get("/api/species/999999")
    assert response.status_code == 404


def test_invalid_month_gives_422(client: TestClient) -> None:
    response = client.get("/api/zones/28S/month/13")
    assert response.status_code == 422


def test_invalid_mgrs_prefix_gives_422(client: TestClient) -> None:
    response = client.get("/api/zones/lowercase")
    assert response.status_code == 422


def test_zone_totals_are_internally_consistent(client: TestClient) -> None:
    response = client.get("/api/zones/28S")
    assert response.status_code == 200
    summary = response.json()
    top_species_total = sum(r["occurrences"] for r in summary["top_species"])
    assert top_species_total <= summary["total_occurrences"]


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


def test_zone_bottom_species_meet_minimum_occurrence_threshold(client: TestClient) -> None:
    # Regression test: before MIN_LIST_OCCURRENCES, the bottom list surfaced
    # single-record vagrants (occurrences=1) whose concentration is near
    # zero purely from sample-size noise, not genuine underrepresentation.
    response = client.get("/api/zones/28S")
    summary = response.json()
    for r in summary["bottom_species"] + summary["top_species"]:
        assert r["occurrences"] >= 5


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


def test_species_cells_month_filter_returns_subset(client: TestClient, madeirensis_id: int) -> None:
    all_response = client.get(f"/api/species/{madeirensis_id}/cells")
    month_response = client.get(f"/api/species/{madeirensis_id}/cells", params={"month": 1})
    assert month_response.status_code == 200

    all_cells = {c["mgrs_cell"] for c in all_response.json()}
    month_cells = {c["mgrs_cell"] for c in month_response.json()}
    assert month_cells
    assert month_cells <= all_cells


def test_all_species_includes_vernacular_name_columns(client: TestClient) -> None:
    response = client.get("/api/species/all")
    assert response.status_code == 200
    species = response.json()
    assert len(species) == 584
    for key in ("common_name_pt", "common_name_es", "common_name_en"):
        assert key in species[0]


def test_all_species_dex_number_is_a_stable_1_to_584_sequence(client: TestClient) -> None:
    response = client.get("/api/species/all")
    species = response.json()
    dex_numbers = [s["dex_number"] for s in species]
    assert dex_numbers == list(range(1, 585))

    # stable across repeated calls, not just a valid permutation on one call
    again = client.get("/api/species/all").json()
    assert [s["dex_number"] for s in again] == dex_numbers
    assert [s["id"] for s in again] == [s["id"] for s in species]


def test_search_finds_species_by_portuguese_name(client: TestClient) -> None:
    response = client.get("/api/species", params={"q": "Melro"})
    assert response.status_code == 200
    results = response.json()
    assert any(r["gbif_name"] == "Turdus merula" for r in results)


def test_search_finds_species_by_spanish_name(client: TestClient) -> None:
    response = client.get("/api/species", params={"q": "Mirlo"})
    assert response.status_code == 200
    results = response.json()
    assert any(r["gbif_name"] == "Turdus merula" for r in results)


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


def test_region_invalid_month_gives_422(client: TestClient, madrid_region_id: int) -> None:
    response = client.get(f"/api/regions/{madrid_region_id}", params={"month": 13})
    assert response.status_code == 422


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


def test_grid_cells_region_assignment_matches_regions_table(
    db_conn: sqlite3.Connection,
) -> None:
    # Every grid_cells row must have a region assigned (including the
    # offshore fallback) -- none left NULL -- and region_name must match
    # what the regions table says for that region_id, not just be some
    # independently-set string that could drift out of sync.
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
