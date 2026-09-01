# syntax=docker/dockerfile:1
#
# Builds Nidatlas as a self-contained image: the app code, its Python
# dependencies, and data/nidatlas.db + the generated static/*.geojson files
# are all baked in at build time (see CLAUDE.md's Deployment section --
# there is no runtime data-fetch step, no external storage mount).
# Updating the data means rebuilding and redeploying this image; it is not a
# live-editable volume.
#
# Build:
#   docker build -t nidatlas .                                   # IDENTIFY off (default)
#   docker build --build-arg INCLUDE_IDENTIFY=true -t nidatlas .  # IDENTIFY on (+ CPU-only torch/model deps)
#
# Run:
#   docker run -p 8000:8000 nidatlas
#   docker run -p 8000:8000 -e ENABLE_IDENTIFY=1 nidatlas   # only meaningful if built with INCLUDE_IDENTIFY=true

# ---------------------------------------------------------------------------
# Stage 1: build a virtualenv with exactly the packages this deployment needs
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

# Controls whether requirements-identify.txt (pybioclip + torch) is
# installed into the image at all -- see CLAUDE.md's "IDENTIFY feature
# isolation" design decision. Independent of ENABLE_IDENTIFY, which is a
# runtime env var read by src/api.py: this arg decides whether the packages
# exist in the image; ENABLE_IDENTIFY decides whether the app tries to use
# them. Building without this arg and then setting ENABLE_IDENTIFY=1 at
# runtime fails closed (503 IdentificationUnavailable), not a crash.
ARG INCLUDE_IDENTIFY=false

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# requirements-runtime.txt, not requirements.txt: the latter also carries
# pandas/pyarrow/shapely/pyproj/mgrs/httpx/pytest for the offline data
# pipeline and test suite (scripts/, tests/), none of which src/api.py or
# src/queries.py import at request time -- see that file's own header.
COPY requirements-runtime.txt requirements-identify.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements-runtime.txt && \
    if [ "$INCLUDE_IDENTIFY" = "true" ]; then \
        # requirements-identify.txt doesn't pin a torch version, and pip's
        # default index serves the CUDA-enabled build of torch by default on
        # Linux -- which bundles the full NVIDIA CUDA/cuDNN/cuBLAS runtime
        # (several GB) even though identification.py always runs
        # TreeOfLifeClassifier(device="cpu") and this image has no GPU to use
        # anyway. Measured directly: that mistake alone made this stage's
        # venv 5.2GB instead of ~1GB. Installing torch/torchvision from
        # PyTorch's own CPU-only wheel index FIRST means pybioclip (installed
        # right after, with no conflicting pin) is satisfied by the
        # already-installed CPU build and never pulls the CUDA one.
        pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu && \
        pip install --no-cache-dir -r requirements-identify.txt; \
    fi

# ---------------------------------------------------------------------------
# Stage 2: minimal runtime image -- just the venv + app code + baked data
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS final

RUN groupadd --gid 1000 nidatlas && \
    useradd --uid 1000 --gid nidatlas --no-create-home --shell /usr/sbin/nologin nidatlas

WORKDIR /app

COPY --from=builder --chown=nidatlas:nidatlas /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    ENABLE_IDENTIFY="" \
    TRUST_PROXY_HEADERS="" \
    HF_HOME=/tmp/huggingface

# Only matters when INCLUDE_IDENTIFY=true AND the model weights aren't
# already baked in (they aren't, by default -- see the data/nidatlas.db
# comment below): on first use, pybioclip downloads ~1.7GB from Hugging Face
# Hub via huggingface_hub, which needs a writable cache directory. The
# nidatlas user has no home directory (--no-create-home below, kept
# deliberately minimal), so huggingface_hub's default cache location
# (~/.cache/huggingface) doesn't exist and can't be created -- confirmed
# directly: without HF_HOME set, a real /api/identify request crashed with
# "Permission denied: '/home/nidatlas'" instead of running. /tmp is
# world-writable by default on this base image, so no extra chown needed.

# App code and pre-built static frontend (static/*.geojson are generated
# build artifacts, already sitting in static/ from a local pipeline run --
# see CLAUDE.md -- not fetched or built here).
#
# --chown on every COPY here, not a separate `RUN chown -R /app` afterward:
# on an overlay filesystem, chown-ing an existing layer's files forces a
# full copy-on-write rewrite of every file it touches into a NEW layer --
# measured directly on this exact Dockerfile: a trailing `RUN chown -R
# nidatlas:nidatlas /app` added a second ~220MB layer that just duplicated
# the ~220MB already copied by the three COPYs below, nearly doubling the
# image for zero benefit. --chown applied AT copy time sets ownership as
# part of the same layer instead, so it costs nothing extra.
COPY --chown=nidatlas:nidatlas src/ ./src/
COPY --chown=nidatlas:nidatlas static/ ./static/

# Only the built database is baked in, not the rest of data/ (raw GBIF CSVs,
# API-response caches, curated CSVs the pipeline reads) -- see
# .dockerignore. data/iberian_species.txt (needed only by the IDENTIFY
# feature) is deliberately NOT included here either; a deployment that wants
# IDENTIFY on needs to add it separately for now -- see CLAUDE.md.
COPY --chown=nidatlas:nidatlas data/nidatlas.db ./data/nidatlas.db

USER nidatlas

EXPOSE 8000

CMD ["python", "src/api.py"]
