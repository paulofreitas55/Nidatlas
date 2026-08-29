import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from api import app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)


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
