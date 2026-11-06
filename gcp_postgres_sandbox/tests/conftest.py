"""Make the technical-analysis modules importable without touching a database.

They validate DB_* at import time and read .env when not on GitHub Actions. Values
are forced, not defaulted, so a developer's real credentials can't reach a test.
"""

import os

os.environ.update(
    DB_HOST="offline-test",
    DB_USER="offline-test",
    DB_PASSWORD="offline-test",
    GITHUB_ACTIONS="1",
)
