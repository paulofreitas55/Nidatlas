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
    files = {"file": ("bird.jpg", _tiny_jpeg_bytes(), "image/jpeg")}
    response = client.post("/api/identify", files=files)
    assert response.status_code == 200

    body = response.json()
    assert body["confident"] is True
    assert len(body["candidates"]) == 2

    top = body["candidates"][0]
    assert top["gbif_name"] == "Turdus merula"
    assert top["score"] == pytest.approx(0.98)
    assert "id" in top  # links this candidate to its species page


def test_identify_rejects_unsupported_file_type(client: TestClient) -> None:
    files = {"file": ("notes.txt", b"not an image", "text/plain")}
    response = client.post("/api/identify", files=files)
    assert response.status_code == 415


def test_identify_rejects_oversized_upload(client: TestClient) -> None:
    # Sized past the 8MB cap -- rejected on length alone before ever reaching
    # the (mocked) classifier, so this doesn't need to be a real image.
    oversized = b"\xff" * (8 * 1024 * 1024 + 1)
    files = {"file": ("big.jpg", oversized, "image/jpeg")}
    response = client.post("/api/identify", files=files)
    assert response.status_code == 413


def test_identify_rejects_undecodable_image(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_value_error(image_bytes: bytes) -> dict:
        raise ValueError("not a decodable image")

    monkeypatch.setattr(identification, "classify_image_bytes", raise_value_error)
    files = {"file": ("bad.jpg", b"not really a jpeg", "image/jpeg")}
    response = client.post("/api/identify", files=files)
    assert response.status_code == 422
