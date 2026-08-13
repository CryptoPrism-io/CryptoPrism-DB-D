"""CP-011 Phase B — shadow-rebuild target guard.

The full PIT shadow rebuild (when approved) must write to a UNIQUELY VERSIONED
SHADOW schema and must NEVER resolve to canonical ``FE_*`` production tables
through any configuration default. This module is the single guard: any writer
calls ``assert_shadow_schema`` before touching a table, and canonical names
(``FE_DMV_ALL``, ``FE_DMV_SCORES``, ``public``, etc.) are rejected outright.
"""

from __future__ import annotations

import re

SHADOW_SCHEMA_PREFIX = "pit_dmv_"

# Canonical names that must never be a write target.
CANONICAL_FORBIDDEN = (
    "FE_DMV_ALL", "FE_DMV_SCORES", "FE_PCT_CHANGE", "FE_MOMENTUM_SIGNALS",
    "FE_OSCILLATORS_SIGNALS", "FE_TVV_SIGNALS", "FE_RATIOS_SIGNALS",
    "FE_METRICS_SIGNAL", "public", "dbcp", "cp_backtest",
)

_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def shadow_schema(version: str) -> str:
    """Return the versioned shadow schema name for a rebuild run."""
    v = re.sub(r"[^a-zA-Z0-9_]+", "_", str(version)).strip("_")
    if not v:
        raise ValueError("shadow version must be non-empty")
    return f"{SHADOW_SCHEMA_PREFIX}{v}"


def assert_shadow_schema(schema: str) -> str:
    """Validate that ``schema`` is a versioned shadow schema, else raise.

    Rejects canonical/production names and anything that isn't a valid schema
    identifier prefixed with ``pit_dmv_``. A writer may only use the returned
    (normalized) name.
    """
    if not isinstance(schema, str) or not schema:
        raise ValueError(f"invalid schema target: {schema!r}")
    if schema in CANONICAL_FORBIDDEN or any(
        schema.upper().startswith(f) for f in CANONICAL_FORBIDDEN
    ):
        raise ValueError(
            f"refusing to write to canonical/production table schema {schema!r}; "
            "shadow rebuilds must target a versioned pit_dmv_* schema"
        )
    if not schema.startswith(SHADOW_SCHEMA_PREFIX):
        raise ValueError(
            f"schema {schema!r} is not a versioned shadow schema "
            f"(must start with {SHADOW_SCHEMA_PREFIX!r})"
        )
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"schema {schema!r} is not a valid lower-case identifier")
    return schema
