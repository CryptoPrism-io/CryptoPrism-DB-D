"""Make the technical-analysis modules importable without touching a database.

They validate DB_* at import time and read .env when not on GitHub Actions. Values
are forced, not defaulted, so a developer's real credentials can't reach a test.
"""

import os
import sys
import types

os.environ.update(
    DB_HOST="offline-test",
    DB_USER="offline-test",
    DB_PASSWORD="offline-test",
    GITHUB_ACTIONS="1",
)

# The backfill imports the POSIX-only `resource` for an end-of-run memory log.
if sys.platform == "win32":
    sys.modules.setdefault("resource", types.ModuleType("resource"))
