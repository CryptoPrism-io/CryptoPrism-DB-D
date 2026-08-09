"""Ensure ``gcp_postgres_sandbox`` (and thus the ``pit`` package) is importable."""

import sys
from pathlib import Path

_SANDBOX = Path(__file__).resolve().parent.parent.parent
if str(_SANDBOX) not in sys.path:
    sys.path.insert(0, str(_SANDBOX))
