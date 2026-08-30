import concurrent.futures
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from api import app  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "nidario.db"


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
