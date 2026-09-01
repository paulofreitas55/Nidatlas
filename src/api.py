#!/usr/bin/env python
"""FastAPI web layer over the query functions in queries.py."""

import os
import sqlite3
import time
from collections import defaultdict
from collections.abc import Generator

import queries
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

# Whole IDENTIFY feature (route registration below, /identify static page,
# nav button) is off unless this is explicitly set -- see CLAUDE.md's
# "IDENTIFY feature isolation" design decision. This module never imports
# torch/pybioclip itself either way (see src/identification.py's own
# docstring); this flag additionally controls whether that module's heavy
# functions are ever actually called, so a deploy with the packages simply
# not installed is safe as long as the flag stays unset.
ENABLE_IDENTIFY = os.environ.get("ENABLE_IDENTIFY", "").strip().lower() in ("1", "true", "yes")

# Was hardcoded to 127.0.0.1:8000, which only accepts connections from
# inside the same network namespace -- fine for local dev, but unreachable
# from outside a Docker container even with the port published. 0.0.0.0
# binds every interface, which is what a container needs; HOST/PORT stay
# overridable for whatever a given deployment target expects (e.g. a
# platform that assigns its own PORT).
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# Off by default: X-Forwarded-For is client-suppliable, so trusting it
# blindly would let anyone dodge rate limiting by just claiming a different
# IP. Only turn this on when actually deployed behind a reverse proxy (e.g.
# Azure Container Apps' own ingress) that ITSELF sets/overwrites this header
# on every request rather than passing through whatever a client already
# sent -- see _client_ip below and CLAUDE.md's rate-limiting design decision.
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "").strip().lower() in ("1", "true", "yes")

app = FastAPI(title="Nidatlas API")

# Global safety net, applies to every route: no request body over this size
# is ever fully received, in memory or otherwise -- Starlette enforces it
# DURING the read itself (tracking a running total across the ASGI receive
# stream, not just trusting a client-declared Content-Length, which a client
# could omit or lie about), before any route or other middleware gets a
# chance to buffer it. Set just above /api/identify's own 8MB image cap --
# the only endpoint that accepts a body at all today -- so that cap (checked
# inside the route itself) is always what actually rejects an oversized
# image with IDENTIFY's own error message; this one exists purely as a hard
# ceiling so nothing in this app can ever be made to buffer an unbounded
# payload. See CLAUDE.md's upload/disk-spooling design decision for the
# measurement that motivated this.
#
# MUST be added FIRST (before GZip and the @app.middleware("http") functions
# below): add_middleware() inserts each new middleware at the OUTERMOST
# position, so registering this one first makes it the INNERMOST instead --
# directly wrapping the router with no BaseHTTPMiddleware layer in between.
# That ordering isn't cosmetic: this middleware raises its 413 by making the
# wrapped ASGI `receive` callable itself throw once the running total is
# exceeded, and a BaseHTTPMiddleware layer (which is what every
# @app.middleware("http") function below compiles to) sitting between it and
# the route runs call_next() inside its own anyio task group -- an exception
# raised from deep inside that nested receive() call gets wrapped into an
# ExceptionGroup that this middleware's own try/except no longer recognises,
# so the request crashes with an unhandled 500 instead of a clean 413.
# Confirmed by reproducing exactly that failure with this middleware added
# last, before moving it here -- see tests/test_identify.py's global-cap
# test, which exercises this directly.
_MAX_REQUEST_BODY_BYTES = 9 * 1024 * 1024
app.add_middleware(RequestBodyLimitMiddleware, max_body_size=_MAX_REQUEST_BODY_BYTES)

# static/iberia.geojson and the cell-list endpoints are JSON/geometry --
# exactly the content type gzip compresses best (typically 80-90% smaller
# in transfer size), so this cuts real load time on top of the vertex-count
# reduction already done in the file itself.
app.add_middleware(GZipMiddleware, minimum_size=1000)


_CACHE_LONG = "public, max-age=86400"  # static assets/images: 1 day
_CACHE_SHORT = "public, max-age=60"  # read-only /api/* GETs: 1 minute
_CACHE_NONE = "no-store"

# Endpoints that must never be cached regardless of the /api/ prefix rule
# below: /api/health and /api/config are cheap to recompute but wrong to
# serve stale (a cached "identify_enabled": false would hide the IDENTIFY nav
# link even after an operator turns the feature on); /api/identify is a POST
# with per-request rate-limiting and file-upload side effects, not a
# cacheable resource at all.
_CACHE_NONE_PATHS = {"/api/health", "/api/config", "/api/identify"}


@app.middleware("http")
async def cache_control_headers(request, call_next):
    # Static assets (the six HTML pages, JS/CSS, the GeoJSON basemap/region
    # files) only change when the image is rebuilt and redeployed -- see
    # CLAUDE.md's Deployment section: data/nidatlas.db and the generated
    # static/*.geojson files are baked into the image at build time, there is
    # no live edit-and-reload story in a deployed container the way there was
    # for local dev. A day-long max-age is "long" without being "forever":
    # these files aren't served under a content-hashed filename (no build
    # step generates one), so a multi-day or immutable cache would risk
    # browsers holding onto stale JS/CSS across a redeploy that changes
    # behaviour, for as long as that max-age lasts.
    #
    # /api/* responses reflect data/nidatlas.db, which is likewise fixed for
    # the life of a deployed container -- a short cache (1 minute) trades a
    # small, bounded staleness window for real load reduction on popular
    # endpoints (species list, rankings, region summaries) without the
    # complexity of per-route invalidation. The three paths in
    # _CACHE_NONE_PATHS above are carved out because caching them at all
    # would be actively wrong, not just imprecise.
    response = await call_next(request)
    path = request.url.path
    if path in _CACHE_NONE_PATHS:
        response.headers["Cache-Control"] = _CACHE_NONE
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = _CACHE_SHORT
    else:
        response.headers["Cache-Control"] = _CACHE_LONG
    return response


# External hosts this app actually loads something from -- kept in sync with
# ATTRIBUTIONS.md: unpkg.com serves Leaflet's JS/CSS (species.html/map.html),
# iNaturalist's S3 bucket and Wikimedia Commons serve hotlinked species
# photos (never downloaded/re-hosted -- see CLAUDE.md's "Species photos are
# hotlinked" design decision), blob: is the identify page's own
# URL.createObjectURL() preview of a locally-selected file before upload.
# Nothing else is allowed -- if a future change adds another external
# resource (a new CDN, a different photo host), it needs a matching addition
# here or the browser will silently block it.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' https://unpkg.com; "
    "style-src 'self' https://unpkg.com; "
    "img-src 'self' data: blob: https://inaturalist-open-data.s3.amazonaws.com https://upload.wikimedia.org https://unpkg.com; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # nosniff: stops a browser from guessing a response's type from its
    # content instead of trusting Content-Type -- relevant here since
    # /api/identify accepts a user-supplied image and StaticFiles serves
    # whatever's on disk. strict-origin-when-cross-origin: this site links
    # out to GBIF/iNaturalist/OpenStreetMap/etc. in footers and species
    # pages -- full URL on same-origin navigation, origin only cross-origin,
    # nothing on a downgrade to plain HTTP.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
    return response


def _client_ip(request: Request) -> str:
    # See TRUST_PROXY_HEADERS above: only reads X-Forwarded-For when this
    # deployment has explicitly said a trusted proxy is in front of it and
    # owns that header. X-Forwarded-For is a comma-separated hop chain (each
    # proxy appends its own view) -- the FIRST entry is the original client
    # as seen by the nearest trusted proxy, which is only meaningful under
    # the precondition above (a trusted proxy that sets/overwrites this
    # header itself, never blindly forwarding a client-supplied one).
    if TRUST_PROXY_HEADERS:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class _SlidingWindowRateLimiter:
    # In-process, single-worker-only -- see CLAUDE.md's rate-limiter design
    # decision (this is a KNOWN limitation, not fixed here: each process
    # gets its own independent counters, so the effective limit multiplies
    # by worker/replica count under any multi-process deployment). Shared by
    # the general /api/* limiter and (when enabled) IDENTIFY's own stricter
    # one below, rather than duplicating this logic twice.
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._request_times: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        recent = [t for t in self._request_times[key] if t >= cutoff]
        allowed = len(recent) < self.max_requests
        if allowed:
            recent.append(now)
        self._request_times[key] = recent
        return allowed


# General /api/* rate limit: deliberately generous (100/min, vs IDENTIFY's
# 10/min below) since these are cheap read-only SQLite queries against a
# small local database, not model inference -- the goal is blocking a script
# hammering the API, not throttling normal browsing. See CLAUDE.md: this
# should move to a reverse-proxy/CDN layer once one exists in front of this
# app, rather than growing more elaborate in-process.
_GENERAL_API_RATE_LIMIT_MAX_REQUESTS = 100
_GENERAL_API_RATE_LIMIT_WINDOW_SECONDS = 60
_general_api_limiter = _SlidingWindowRateLimiter(
    _GENERAL_API_RATE_LIMIT_MAX_REQUESTS, _GENERAL_API_RATE_LIMIT_WINDOW_SECONDS
)


@app.middleware("http")
async def enforce_general_api_rate_limit(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        if not _general_api_limiter.allow(_client_ip(request)):
            return JSONResponse({"detail": "too many API requests -- try again shortly"}, status_code=429)
    return await call_next(request)


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


if ENABLE_IDENTIFY:
    import identification

    _IDENTIFY_MAX_FILE_SIZE_BYTES = 8 * 1024 * 1024  # 8MB -- generous for a phone photo, small for a DoS upload
    _IDENTIFY_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}

    # Stricter than the general /api/* limiter above: this is the one place
    # in the app that runs real model inference, so it's the one place worth
    # protecting from being hammered specifically (not a new dependency --
    # reuses _SlidingWindowRateLimiter, same in-process/single-worker caveat
    # noted there).
    _IDENTIFY_RATE_LIMIT_MAX_REQUESTS = 10
    _IDENTIFY_RATE_LIMIT_WINDOW_SECONDS = 60
    _identify_limiter = _SlidingWindowRateLimiter(
        _IDENTIFY_RATE_LIMIT_MAX_REQUESTS, _IDENTIFY_RATE_LIMIT_WINDOW_SECONDS
    )

    @app.post("/api/identify")
    async def api_identify(
        request: Request,
        conn: sqlite3.Connection = Depends(get_db),
    ) -> dict:
        client_key = _client_ip(request)
        if not _identify_limiter.allow(client_key):
            raise HTTPException(status_code=429, detail="too many identification requests -- try again shortly")

        content_type = request.headers.get("content-type", "")
        if content_type not in _IDENTIFY_ALLOWED_CONTENT_TYPES:
            raise HTTPException(status_code=415, detail=f"unsupported file type: {content_type!r}")

        # Read the raw request body directly -- deliberately NOT via
        # FastAPI's UploadFile/File(...), which parses multipart/form-data
        # through Starlette's MultiPartParser. That parser spools any file
        # part over 1MB (SpooledTemporaryFile's spool_max_size, a hardcoded
        # 1MB default) to a REAL temporary file on disk -- confirmed by
        # directly reproducing that exact mechanism, not assumed -- which
        # broke this feature's own "never written to disk" promise for any
        # upload over 1MB (an ordinary phone photo, well under the 8MB cap
        # here). The frontend now POSTs the raw file bytes with its own
        # Content-Type instead of multipart/form-data (see identify.js) so
        # this endpoint never touches that code path at all.
        #
        # request.body() is a pure in-memory ASGI read (concatenates
        # receive() chunks into a bytes object -- no tempfile involved,
        # verified the same way), and the app-wide RequestBodyLimitMiddleware
        # registered above already bounds any request body to
        # _MAX_REQUEST_BODY_BYTES DURING this read, so this also can't be
        # abused to buffer an unbounded payload before the size check below
        # even runs. See CLAUDE.md's upload/disk-spooling design decision.
        body = await request.body()
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
    q: str = Query(..., min_length=1, max_length=100),
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


# The species atlas is the site's landing page and the region map view
# lives at /map -- both are plain static files (static/atlas.html,
# static/map.html), but StaticFiles(html=True) below only auto-serves
# index.html for "/", not a same-directory file under a different name.
# These explicit routes are declared ahead of the Mount for that reason;
# every other static asset (map.js, atlas.html hit directly, species.html,
# *.geojson, ...) still resolves through the Mount by its own filename
# exactly as before.
@app.get("/", include_in_schema=False)
def serve_atlas() -> FileResponse:
    return FileResponse("static/atlas.html")


@app.get("/map", include_in_schema=False)
def serve_map() -> FileResponse:
    return FileResponse("static/map.html")


@app.get("/tree", include_in_schema=False)
def serve_tree() -> FileResponse:
    return FileResponse("static/tree.html")


@app.get("/rank", include_in_schema=False)
def serve_rank() -> FileResponse:
    return FileResponse("static/rank.html")


@app.get("/privacy", include_in_schema=False)
def serve_privacy() -> FileResponse:
    return FileResponse("static/privacy.html")


# Mounted last and at "/" so it only catches paths no route above already
# matched -- Starlette tries routes in registration order, and a Mount at "/"
# would otherwise shadow everything if it came first.
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
