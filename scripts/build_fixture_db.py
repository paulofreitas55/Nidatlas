#!/usr/bin/env python
"""Build a minimal, schema-only data/nidatlas.db for CI.

Not a subset or synthetic replica of the real production dataset -- every
table this project's schema defines, with zero rows. Reuses
build_database.py's own SCHEMA string (which already includes
phylo_nodes/phylo_closure -- see that file) rather than a second
hand-copied definition, so this can't silently drift from what a real
pipeline run produces.

Exists only so the ~15 boundary/validation tests NOT marked
@pytest.mark.requires_full_dataset (see pytest.ini) can run in CI without
the real ~4.6M-row database, which needs the two GBIF downloads and a
multi-step pipeline run and isn't (and shouldn't be) checked into git. The
~30 tests that assert specific real-world facts (a Madeira endemic ranking
top by concentration, a genuine occurrence-count tie, a real MRCA in the
Open Tree of Life subtree, ...) are skipped in CI and must still be run
locally against the real database before merging any change that touches
query logic -- see CLAUDE.md's CI section for the full reasoning.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_database import SCHEMA  # noqa: E402

DB_PATH = Path("data") / "nidatlas.db"


def main() -> None:
    DB_PATH.parent.mkdir(exist_ok=True)
    DB_PATH.unlink(missing_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"Built empty fixture schema at {DB_PATH}")


if __name__ == "__main__":
    main()
