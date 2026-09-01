#!/usr/bin/env python
"""In-memory BioCLIP 2 species identification, restricted to the atlas's own
584 Iberian species -- the web-facing counterpart to scripts/identify.py's
CLI, sharing the same restrict-to-Iberian-species approach (see that
script's own module docstring and CLAUDE.md's "BioCLIP's label space
restricted to Iberian species" design decision).

Import isolation, deliberately: nothing at THIS MODULE'S TOP LEVEL imports
torch, bioclip, or PIL. Every heavy import happens lazily inside
_get_classifier()/classify_image_bytes(), so importing this module -- which
api.py does unconditionally -- costs nothing and never requires those
~1.7GB-of-weights packages to be installed. The feature is additionally
gated end-to-end by the ENABLE_IDENTIFY environment variable in api.py: the
route is only registered, and this module's heavy-import functions are only
ever called, when that flag is on. See CLAUDE.md's "IDENTIFY feature
isolation" design decision for the full reasoning -- this split is what lets
the rest of the app run and deploy as a lightweight container with the
feature disabled.

Confidence threshold: chosen from a direct measurement, not a guess -- see
CLAUDE.md's "IDENTIFY confidence threshold" design decision. Under this same
Iberian-restricted classifier, 5 real Iberian species photos scored between
0.956 and 0.998 top-1 confidence; a non-Iberian bird (an Emperor Penguin,
forced to pick among only Iberian species), a domestic cat, a UI screenshot,
and random pixel noise scored between 0.14 and 0.36. CONFIDENCE_THRESHOLD
sits in the wide gap between those two clusters.
"""

import io
import threading
from pathlib import Path

DATA_DIR = Path("data")
SPECIES_LIST_PATH = DATA_DIR / "iberian_species.txt"

CONFIDENCE_THRESHOLD = 0.5
MAX_RESULTS = 5

_classifier = None
_classifier_lock = threading.Lock()


class IdentificationUnavailable(RuntimeError):
    """The model or its species list can't be loaded -- e.g. pybioclip/torch
    aren't installed, or data/iberian_species.txt hasn't been built yet.
    Distinct from ValueError (a bad image) so the API layer can tell "the
    feature is broken/misconfigured" (503) apart from "your upload is bad"
    (422)."""


def _load_species_names() -> list[str]:
    if not SPECIES_LIST_PATH.is_file():
        raise IdentificationUnavailable(
            f"{SPECIES_LIST_PATH} not found -- run scripts/build_species_list.py first"
        )
    # Same format scripts/identify.py already reads: "bioclip_name,gbif_name"
    # per line, only the first column needed here.
    return [
        line.strip().split(",")[0]
        for line in SPECIES_LIST_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _get_classifier():
    global _classifier
    if _classifier is not None:
        return _classifier
    with _classifier_lock:
        if _classifier is None:  # re-check inside the lock -- avoid a double load if two
            # requests race to build it (FastAPI runs sync/lazy work like this
            # in a threadpool, so concurrent first-requests are a real case)
            try:
                from bioclip import Rank, TreeOfLifeClassifier
            except ImportError as exc:
                raise IdentificationUnavailable(
                    "pybioclip/torch are not installed -- the IDENTIFY feature needs them even "
                    "though the rest of the app does not (see requirements.txt)"
                ) from exc
            species_names = _load_species_names()
            classifier = TreeOfLifeClassifier(device="cpu")
            taxa_filter = classifier.create_taxa_filter(Rank.SPECIES, species_names)
            classifier.apply_filter(taxa_filter)
            _classifier = classifier
    return _classifier


def classify_image_bytes(image_bytes: bytes) -> dict:
    """Runs the Iberian-restricted classifier on an in-memory image -- this
    function itself never writes anything to disk, decoding straight from
    bytes and handing PIL's Image object to BioCLIP directly (pybioclip's
    predict() accepts one directly, no temp file needed). That alone doesn't
    guarantee the image was never written to disk ANYWHERE in the request's
    lifecycle, though -- it previously wasn't true end-to-end, because
    api.py used to hand this function bytes read from a FastAPI UploadFile,
    and Starlette's multipart parser spools any upload over 1MB to a real
    temporary file before this function ever sees it. api.py's
    /api/identify handler now reads the raw request body directly instead
    (no multipart parsing at all), which is what actually makes "never
    written to disk" true end-to-end -- verified by reproducing the exact
    spooling mechanism, then confirming its absence with a live upload while
    polling the temp directory for the request's full duration. See
    CLAUDE.md's "Upload path: no multipart, no disk spooling" design
    decision for the full story; this docstring only ever described what
    THIS function does, not the request path leading into it.
    Returns {"confident": bool, "candidates": [...]}, up to
    MAX_RESULTS candidates each {"bioclip_name", "common_name", "score"},
    highest score first. "confident" is only True when the TOP candidate's
    score clears CONFIDENCE_THRESHOLD -- callers should not present the
    candidate list as a real answer otherwise (see CLAUDE.md).

    Raises IdentificationUnavailable if the model/species list can't load,
    ValueError if image_bytes isn't decodable as an image."""
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()  # Image.open() is lazy; force decoding now to catch a truncated/bad file here
        image = image.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("not a decodable image") from exc

    from bioclip import Rank

    classifier = _get_classifier()
    predictions = classifier.predict([image], Rank.SPECIES, k=MAX_RESULTS)
    candidates = [
        {
            "bioclip_name": prediction["species"],
            "common_name": prediction.get("common_name"),
            "score": prediction["score"],
        }
        for prediction in predictions
    ]
    confident = bool(candidates) and candidates[0]["score"] >= CONFIDENCE_THRESHOLD
    return {"confident": confident, "candidates": candidates}
