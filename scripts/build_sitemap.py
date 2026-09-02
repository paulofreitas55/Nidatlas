#!/usr/bin/env python
"""Build static/sitemap.xml from data/nidatlas.db.

Build-time, not request-time -- same pattern as every other file in
static/ (iberia.geojson, regions.geojson, taxa_labels.json, ...): a
pre-built artifact baked into the Docker image, not computed per-request.
Means a species-list change needs a rebuild+redeploy to show up here too,
same tradeoff already accepted and documented (CLAUDE.md's "Data updates
require a rebuild") for every other derived static file.

Species URLs use the CURRENT real route, species.html?id=<id> -- not a
clean /species/<id> path, which doesn't exist yet (that would need the
species pages to become a real per-species FastAPI route with server-
rendered metadata, a separate, larger piece of work that hasn't been
built). Regenerate this file if that migration ever happens.
"""

import sqlite3
from pathlib import Path
from xml.sax.saxutils import escape

DB_PATH = Path("data") / "nidatlas.db"
OUT_PATH = Path("static") / "sitemap.xml"
BASE_URL = "https://nidatlas.com"

STATIC_PATHS = ["/", "/map", "/tree", "/rank"]


def main() -> None:
    conn = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    species_ids = [row[0] for row in conn.execute("SELECT id FROM species ORDER BY id")]
    conn.close()

    urls = [f"{BASE_URL}{path}" for path in STATIC_PATHS]
    urls += [f"{BASE_URL}/species.html?id={sid}" for sid in species_ids]

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url in urls:
        lines.append(f"  <url><loc>{escape(url)}</loc></url>")
    lines.append("</urlset>")

    OUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_PATH} with {len(urls)} URLs ({len(STATIC_PATHS)} static + {len(species_ids)} species)")


if __name__ == "__main__":
    main()
