"""CP-011 Phase B — centralized bin-only score computation + range validation.

Replaces the leaky/permissive score math in ``gcp_dmv_core.py`` / backfill
``phase_core`` (which summed whatever columns fell under ``d_``/``m_``/``v_``
prefixes and could exceed 100 — observed Durability 101.97).

Rules:
  - Sum ONLY explicitly approved bin columns (no prefix sweep).
  - A bin column must contain values in {-1, 0, 1} (NaN allowed = missing).
  - D/M/V in [-100, 100] is enforced (clip) and validated (raise on non-bins).
  - No z-score normalization.
  - Rows missing a required bin are marked incomplete (score NaN), never
    zero-filled.
"""

from __future__ import annotations

import pandas as pd

ALLOWED_BIN_VALUES = {-1.0, 0.0, 1.0}
SCORE_LOW, SCORE_HIGH = -100.0, 100.0


def validate_bin_columns(df: pd.DataFrame, bin_cols: list[str]) -> None:
    """Raise ValueError if any bin column contains non {-1,0,1} values."""
    for c in bin_cols:
        if c not in df.columns:
            raise ValueError(f"missing approved bin column: {c}")
        vals = set(pd.unique(df[c].dropna()))
        extra = {float(v) for v in vals} - ALLOWED_BIN_VALUES
        if extra:
            raise ValueError(f"column {c!r} contains non-bin values: {sorted(extra)}")


def _phase_score(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    sub = df[cols]
    has_missing = sub.isna().any(axis=1)
    score = sub.sum(axis=1) / len(cols) * 100.0
    score = score.where(~has_missing)  # incomplete -> NaN
    return score.clip(SCORE_LOW, SCORE_HIGH)


def compute_scores(
    df: pd.DataFrame,
    *,
    durability_cols: list[str],
    momentum_cols: list[str],
    valuation_cols: list[str],
    validate: bool = True,
) -> pd.DataFrame:
    """Compute D/M/V scores from approved bin columns. Mutates no input.

    Adds: Durability_Score, Momentum_Score, Valuation_Score, incomplete.
    """
    all_bins = durability_cols + momentum_cols + valuation_cols
    if validate:
        validate_bin_columns(df, all_bins)
    out = df.copy()
    out["Durability_Score"] = _phase_score(out, durability_cols)
    out["Momentum_Score"] = _phase_score(out, momentum_cols)
    out["Valuation_Score"] = _phase_score(out, valuation_cols)
    out["incomplete"] = out[["Durability_Score", "Momentum_Score", "Valuation_Score"]].isna().any(axis=1)
    return out
