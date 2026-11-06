"""Range guards for published Indicators and the DMV scores (cpio-mcp#3).

No database code, so it imports safely from the pipeline scripts, the backfill
and the tests.
"""

import logging

import pandas as pd

logger = logging.getLogger(__name__)

# Past this a value is genuinely out of range, not floating-point noise.
NOISE_TOLERANCE = 1e-9

SCORE_PILLARS = [
    ("Durability_Score", "d_"),
    ("Momentum_Score", "m_"),
    ("Valuation_Score", "v_"),
]

# Score inputs are -1..1. d_met_ath_month_signal is stored on its own wider scale
# ((100 - months_since_ath) * 2 / 100: 2 at a fresh ATH, 0 at 100 months), which ML
# features and FE_DMV_ALL rely on, so it's rescaled here and only here.
SCORE_INPUT_SCALE = {"d_met_ath_month_signal": 0.5}


def clamp_to_range(series, low, high, name):
    """Clip to [low, high], warning if anything beyond float noise was cut."""
    out_of_range = (series < low - NOISE_TOLERANCE) | (series > high + NOISE_TOLERANCE)
    if out_of_range.any():
        logger.warning(
            "%s: clamped %d value(s) to [%s, %s] (observed %s..%s)",
            name, int(out_of_range.sum()), low, high, series.min(), series.max(),
        )
    return series.clip(low, high)


def compute_dmv_scores(df):
    """Durability/Momentum/Valuation scores, each in [-100, 100].

    A score is the mean of its pillar's d_/m_/v_ columns times 100. cryptoprism-api
    maps -100..100 to 0..100 itself, so that scale must not change here.
    """
    scores = {}
    for score_name, prefix in SCORE_PILLARS:
        columns = [c for c in df.columns if c.startswith(prefix)]
        inputs = pd.DataFrame({
            c: clamp_to_range(df[c].astype(float) * SCORE_INPUT_SCALE.get(c, 1.0), -1.0, 1.0, c)
            for c in columns
        }, index=df.index)
        scores[score_name] = inputs.sum(axis=1) / max(len(columns), 1) * 100
    return pd.DataFrame(scores, index=df.index)
