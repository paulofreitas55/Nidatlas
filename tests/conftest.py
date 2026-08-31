import os

# Must run before src/api.py is first imported by any test module -- ENABLE_IDENTIFY
# is read once at that module's import time (see api.py's own comment on the
# flag), and pytest always imports conftest.py before collecting test files,
# regardless of which test file pytest happens to import api.py from first.
# This turns the IDENTIFY feature on for the whole test session so
# tests/test_identify.py's routes exist; test_api.py's own tests don't care
# either way.
os.environ.setdefault("ENABLE_IDENTIFY", "1")
