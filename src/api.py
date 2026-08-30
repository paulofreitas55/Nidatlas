#!/usr/bin/env python
"""FastAPI web layer over the query functions in queries.py."""

import sqlite3
from collections.abc import Generator

import queries
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Nidario API")
# static/iberia.geojson and the cell-list endpoints are JSON/geometry --
# exactly the content type gzip compresses best (typically 80-90% smaller
# in transfer size), so this cuts real load time on top of the vertex-count
# reduction already done in the file itself.
app.add_middleware(GZipMiddleware, minimum_size=1000)


def get_db() -> Generator[sqlite3.Connection, None, None]:
    # One connection per request, opened read-only (mode=ro) so the public API
    # can never write to the database no matter what a handler does with it --
    # even a bug or a future endpoint can't corrupt data/nidario.db. The
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


# Mounted last and at "/" so it only catches paths no /api/* route above
# already matched -- Starlette tries routes in registration order, and a
# Mount at "/" would otherwise shadow everything if it came first.
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
