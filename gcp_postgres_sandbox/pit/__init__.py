"""CP-011 Phase B — PIT-safe DMV computation layer.

Pure pandas/numpy modules (no DB, no network) so every function is unit-testable
and deterministic. The sample runner (run_sample.py) reads a bounded OHLCV subset
from cp_backtest (read-only) and exercises these against the real TA signal
functions.

Methodology (locked, CP-011):
  - VaR/CVaR: trailing 365-calendar-day window, min 252 observations, only rows
    with timestamp <= output row date; NULL before min history; never zero-fill.
  - Metrics: cumulative ATH/ATL as of each row date; days-since vs the row date;
    no `now()`; no full-series idxmax/idxmin.
  - Universe: PIT universe abstraction; PIT_APPROX from OHLCV first/last seen
    (no join to today's listings); delisted assets stay active on their dates.
  - Scores: sum ONLY approved bin columns; enforce D/M/V in [-100,100]; no
    z-scores; incomplete rows marked, not zero-filled.
  - Signal policy: declared central core/optional families + missing-data policy
    shared by live and backfill computation.
"""

from pit.var_cvar import calculate_var_cvar_pit
from pit.metrics import calculate_metrics_pit
from pit.universe import PITUniverse
from pit.scores import compute_scores, validate_bin_columns
from pit.policy import (
    METHODOLOGY_VERSION,
    SIGNAL_FAMILIES,
    CORE_FAMILIES,
    OPTIONAL_FAMILIES,
    MISSING_DATA_POLICY,
    VAR_CVAR_METHODOLOGY,
    METRICS_METHODOLOGY,
    UNIVERSE_METHODOLOGY,
    NEUTRAL_FILL,
)

__version__ = METHODOLOGY_VERSION

__all__ = [
    "calculate_var_cvar_pit",
    "calculate_metrics_pit",
    "PITUniverse",
    "compute_scores",
    "validate_bin_columns",
    "METHODOLOGY_VERSION",
    "SIGNAL_FAMILIES",
    "CORE_FAMILIES",
    "OPTIONAL_FAMILIES",
    "MISSING_DATA_POLICY",
    "VAR_CVAR_METHODOLOGY",
    "METRICS_METHODOLOGY",
    "UNIVERSE_METHODOLOGY",
    "NEUTRAL_FILL",
]
