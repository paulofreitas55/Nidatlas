#!/usr/bin/env python
"""FastAPI web layer over the query functions in queries.py."""

import os
import sqlite3
import time
from collections import defaultdict
from collections.abc import Generator

import queries
import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Path, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Whole IDENTIFY feature (route registration below, /identify static page,
# nav button) is off unless this is explicitly set -- see CLAUDE.md's
# "IDENTIFY feature isolation" design decision. This module never imports
# torch/pybioclip itself either way (see src/identification.py's own
# docstring); this flag additionally controls whether that module's heavy
# functions are ever actually called, so a deploy with the packages simply
# not installed is safe as long as the flag stays unset.
ENABLE_IDENTIFY = os.environ.get("ENABLE_IDENTIFY", "").strip().lower() in ("1", "true", "yes")

app = FastAPI(title="Nidatlas API")
# static/iberia.geojson and the cell-list endpoints are JSON/geometry --
# exactly the content type gzip compresses best (typically 80-90% smaller
# in transfer size), so this cuts real load time on top of the vertex-count
# reduction already done in the file itself.
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def no_cache_headers(request, call_next):
    # StaticFiles otherwise lets browsers cache map.js/common.js/*.geojson/etc
    # indefinitely between visits (Starlette sets Last-Modified/ETag but no
    # Cache-Control, and browsers apply their own heuristic freshness window
    # on top of that) -- during active development, where these files change
    # every few minutes, that heuristic window is exactly wrong: a user can
    # reload the page and still get yesterday's JS with no visible sign
    # anything is stale. no-store forces every request to hit the server
    # fresh, trading away caching entirely in exchange for "what's on disk is
    # always what's served" -- the right tradeoff for a small local app, not
    # necessarily for a public production deployment under real load.
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def get_db() -> Generator[sqlite3.Connection, None, None]:
    # One connection per request, opened read-only (mode=ro) so the public API
    # can never write to the database no matter what a handler does with it --
    # even a bug or a future endpoint can't corrupt data/nidatlas.db. The
    # generator + try/finally is FastAPI's documented pattern for a dependency
    # that owns a resource: it guarantees conn.close() runs after the request,
    # including when the handler raises.
    #
    # check_same_thread=False: FastAPI runs a sync generator dependency's setup
    # and teardown as separate calls into its worker threadpool, and under
    # concurrent load they can land on different threads even though they're
    # never used concurrently (one request, sequential phases). sqlite3's
    # default same-thread check rejects that; disabling it is safe here since
    # each request still gets its own dedicated connection, never shared
    # across requests.
    uri = queries.DB_PATH.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/config")
def api_config() -> dict:
    # Lets the static frontend (lang.js's applyFeatureFlags, called on every
    # page) decide at runtime whether to show the IDENTIFY nav button/page --
    # there's no build step to bake this in statically, and it must reflect
    # what THIS deployment actually has enabled, not a hardcoded guess.
    return {"identify_enabled": ENABLE_IDENTIFY}


# --- /api/identify rate limiting ---
#
# In-process sliding-window counter, not a new dependency (no slowapi/redis):
# this endpoint is the one place in the app that runs real model inference,
# so it's the one place worth protecting from being hammered, but the rest
# of this project's architecture (single local/dev-oriented process, no
# shared cache -- see the no-store Cache-Control middleware above) doesn't
# justify pulling in an external rate-limiting stack for it. Same tradeoff,
# revisit together whenever a real multi-process deployment happens (see
# CLAUDE.md's Current state / Next section).
_IDENTIFY_RATE_LIMIT_MAX_REQUESTS = 10
_IDENTIFY_RATE_LIMIT_WINDOW_SECONDS = 60
_identify_request_times: dict[str, list[float]] = defaultdict(list)


def _enforce_identify_rate_limit(client_key: str) -> None:
    now = time.monotonic()
    cutoff = now - _IDENTIFY_RATE_LIMIT_WINDOW_SECONDS
    recent = [t for t in _identify_request_times[client_key] if t >= cutoff]
    if len(recent) >= _IDENTIFY_RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="too many identification requests -- try again shortly")
    recent.append(now)
    _identify_request_times[client_key] = recent


if ENABLE_IDENTIFY:
    import identification

    _IDENTIFY_MAX_FILE_SIZE_BYTES = 8 * 1024 * 1024  # 8MB -- generous for a phone photo, small for a DoS upload
    _IDENTIFY_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}

    @app.post("/api/identify")
    async def api_identify(
        request: Request,
        file: UploadFile = File(...),
        lat: float | None = Form(None),
        lon: float | None = Form(None),
        conn: sqlite3.Connection = Depends(get_db),
    ) -> dict:
        # lat/lon accepted (for a future geographic-plausibility check) but not
        # yet used to affect the result -- see CLAUDE.md; don't build that
        # logic speculatively before it's actually needed.
        del lat, lon

        client_key = request.client.host if request.client else "unknown"
        _enforce_identify_rate_limit(client_key)

        if file.content_type not in _IDENTIFY_ALLOWED_CONTENT_TYPES:
            raise HTTPException(status_code=415, detail=f"unsupported file type: {file.content_type!r}")

        # Read with a +1-byte cap so an oversized upload is rejected without
        # ever buffering more than one byte past the limit, and the image is
        # never written to disk at any point (see identification.py).
        body = await file.read(_IDENTIFY_MAX_FILE_SIZE_BYTES + 1)
        if len(body) > _IDENTIFY_MAX_FILE_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="image too large")

        try:
            # run_in_threadpool: classify_image_bytes is a synchronous, CPU-bound
            # call (real model inference) -- calling it directly here would block
            # this whole process's single event loop for its duration, stalling
            # every other concurrent request (including unrelated ones) for as
            # long as inference takes.
            result = await run_in_threadpool(identification.classify_image_bytes, body)
        except identification.IdentificationUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except ValueError:
            raise HTTPException(status_code=422, detail="could not decode image")

        bioclip_names = [c["bioclip_name"] for c in result["candidates"]]
        species_by_name = queries.species_by_bioclip_names(conn, bioclip_names)
        candidates = [
            {**species_by_name[c["bioclip_name"]], "score": c["score"]}
            for c in result["candidates"]
            if c["bioclip_name"] in species_by_name  # always true in practice -- the classifier is
            # filtered to exactly this DB's own bioclip_name values -- but a
            # candidate that somehow doesn't resolve is dropped, not crashed on
        ]

        return {"confident": result["confident"], "candidates": candidates}

    @app.get("/identify", include_in_schema=False)
    def serve_identify() -> FileResponse:
        return FileResponse("static/identify.html")


@app.get("/api/species")
def api_search_species(
    q: str = Query(..., min_length=1),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    return queries.search_species(conn, q)


@app.get("/api/species/all")
def api_all_species(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return queries.all_species(conn)


@app.get("/api/species/ranking")
def api_species_ranking(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return queries.species_ranking(conn)


@app.get("/api/species/{species_id}")
def api_species_profile(
    species_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    try:
        return queries.species_profile(conn, species_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"no species with id {species_id}")


@app.get("/api/species/{species_id}/cells")
def api_species_cells(
    species_id: int,
    month: int | None = Query(None, ge=1, le=12),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    try:
        return queries.species_cells(conn, species_id, month)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"no species with id {species_id}")


@app.get("/api/zones/{mgrs_prefix}")
def api_cell_summary(
    mgrs_prefix: str = Path(..., pattern=queries.MGRS_PREFIX_RE.pattern),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    return queries.cell_summary(conn, mgrs_prefix)


@app.get("/api/zones/{mgrs_prefix}/month/{month}")
def api_cell_monthly(
    mgrs_prefix: str = Path(..., pattern=queries.MGRS_PREFIX_RE.pattern),
    month: int = Path(..., ge=1, le=12),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    return queries.cell_monthly(conn, mgrs_prefix, month)


@app.get("/api/species/{species_id}/relatives")
def api_species_relatives(
    species_id: int,
    limit: int = Query(10, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    # node_id: the species' own tip, for the frontend to fetch/highlight its
    # own position. clade_node_id: the MRCA of the species AND EVERY listed
    # relative together (not just the farthest one -- see
    # phylo_mrca_of_node_ids for why that shortcut doesn't work), i.e. the
    # smallest clade that actually contains every relative this response
    # names, so a caller can fetch ONE subtree
    # (GET /api/phylo/{clade_node_id}/subtree) to render this exact
    # neighbourhood as a connected cladogram, and link "open in full tree"
    # to that same node. Both are None when the species has no tree
    # placement at all (see phylo_species_node_id).
    try:
        node_id = queries.phylo_species_node_id(conn, species_id)
        relatives = queries.phylo_closest_relatives(conn, species_id, limit)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"no species with id {species_id}")

    clade_node_id = None
    if relatives:
        all_node_ids = [node_id] + [r["node_id"] for r in relatives]
        clade_node_id = queries.phylo_mrca_of_node_ids(conn, all_node_ids)["node_id"]
    elif node_id is not None:
        clade_node_id = node_id

    return {"species_id": species_id, "node_id": node_id, "clade_node_id": clade_node_id, "relatives": relatives}


@app.get("/api/phylo/root")
def api_phylo_root(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return queries.phylo_tree_root(conn)


@app.get("/api/phylo/{node_id}/subtree")
def api_phylo_subtree(
    node_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    try:
        return queries.phylo_subtree(conn, node_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"no phylo node with id {node_id}")


@app.get("/api/regions")
def api_list_regions(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return queries.list_regions(conn)


@app.get("/api/regions/{region_id}")
def api_region_summary(
    region_id: int,
    month: int | None = Query(None, ge=1, le=12),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    try:
        return queries.region_summary(conn, region_id, month)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"no region with id {region_id}")


@app.get("/api/regions/{region_id}/cells")
def api_region_cells(
    region_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    try:
        return queries.region_cells(conn, region_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"no region with id {region_id}")


# The region map is the site's landing page and the species atlas/tree
# views live at /atlas and /tree -- all three are plain static files
# (static/map.html, static/atlas.html, static/tree.html), but
# StaticFiles(html=True) below only auto-serves index.html for "/", not a
# same-directory file under a different name. These explicit routes are
# declared ahead of the Mount for that reason; every other static asset
# (map.js, atlas.html hit directly, species.html, *.geojson, ...) still
# resolves through the Mount by its own filename exactly as before.
@app.get("/", include_in_schema=False)
def serve_map() -> FileResponse:
    return FileResponse("static/map.html")


@app.get("/atlas", include_in_schema=False)
def serve_atlas() -> FileResponse:
    return FileResponse("static/atlas.html")


@app.get("/tree", include_in_schema=False)
def serve_tree() -> FileResponse:
    return FileResponse("static/tree.html")


@app.get("/rank", include_in_schema=False)
def serve_rank() -> FileResponse:
    return FileResponse("static/rank.html")


# Mounted last and at "/" so it only catches paths no route above already
# matched -- Starlette tries routes in registration order, and a Mount at "/"
# would otherwise shadow everything if it came first.
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
