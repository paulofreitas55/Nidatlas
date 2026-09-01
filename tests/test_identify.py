import io
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import identification  # noqa: E402
from api import app  # noqa: E402

# The model itself is never loaded in this test file -- mock_classifier below
# replaces identification.classify_image_bytes for every test, so the suite
# stays fast and doesn't need pybioclip/torch installed to run. See
# tests/conftest.py for why ENABLE_IDENTIFY is guaranteed on by the time
# `api` is imported above.


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def mock_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_classify_image_bytes(image_bytes: bytes) -> dict:
        return {
            "confident": True,
            "candidates": [
                {"bioclip_name": "Turdus merula", "common_name": "Eurasian Blackbird", "score": 0.98},
                {"bioclip_name": "Turdus philomelos", "common_name": "Song Thrush", "score": 0.01},
            ],
        }

    monkeypatch.setattr(identification, "classify_image_bytes", fake_classify_image_bytes)


def _tiny_jpeg_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color=(110, 90, 60)).save(buf, format="JPEG")
    return buf.getvalue()


def test_identify_accepts_valid_image_and_returns_expected_shape(client: TestClient) -> None:
    response = client.post(
        "/api/identify", content=_tiny_jpeg_bytes(), headers={"Content-Type": "image/jpeg"}
    )
    assert response.status_code == 200

    body = response.json()
    assert body["confident"] is True
    assert len(body["candidates"]) == 2

    top = body["candidates"][0]
    assert top["gbif_name"] == "Turdus merula"
    assert top["score"] == pytest.approx(0.98)
    assert "id" in top  # links this candidate to its species page


def test_identify_rejects_unsupported_file_type(client: TestClient) -> None:
    response = client.post("/api/identify", content=b"not an image", headers={"Content-Type": "text/plain"})
    assert response.status_code == 415


def test_identify_rejects_oversized_upload(client: TestClient) -> None:
    # Sized past IDENTIFY's own 8MB business-rule cap but still under the
    # app-wide 9MB RequestBodyLimitMiddleware ceiling (see api.py), so this
    # exercises the route's own len(body) check specifically, not the global
    # middleware below -- rejected on length alone before ever reaching the
    # (mocked) classifier, so this doesn't need to be a real image.
    oversized = b"\xff" * (8 * 1024 * 1024 + 1)
    response = client.post("/api/identify", content=oversized, headers={"Content-Type": "image/jpeg"})
    assert response.status_code == 413


def test_identify_rejects_upload_past_global_body_size_cap(client: TestClient) -> None:
    # Exercises the app-wide RequestBodyLimitMiddleware itself (see api.py's
    # _MAX_REQUEST_BODY_BYTES), a different code path than the route's own
    # 8MB check above -- this one runs before the request even reaches the
    # route handler, for ANY endpoint, not just /api/identify.
    way_oversized = b"\xff" * (10 * 1024 * 1024)
    response = client.post("/api/identify", content=way_oversized, headers={"Content-Type": "image/jpeg"})
    assert response.status_code == 413


def test_identify_rejects_undecodable_image(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_value_error(image_bytes: bytes) -> dict:
        raise ValueError("not a decodable image")

    monkeypatch.setattr(identification, "classify_image_bytes", raise_value_error)
    response = client.post(
        "/api/identify", content=b"not really a jpeg", headers={"Content-Type": "image/jpeg"}
    )
    assert response.status_code == 422
