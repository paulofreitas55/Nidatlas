#!/usr/bin/env python
"""FastAPI web layer over the query functions in queries.py."""

import sqlite3
from collections.abc import Generator

import queries
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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


@app.get("/api/species")
def api_search_species(
    q: str = Query(..., min_length=1),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    return queries.search_species(conn, q)


@app.get("/api/species/all")
def api_all_species(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return queries.all_species(conn)


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


# Mounted last and at "/" so it only catches paths no route above already
# matched -- Starlette tries routes in registration order, and a Mount at "/"
# would otherwise shadow everything if it came first.
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
