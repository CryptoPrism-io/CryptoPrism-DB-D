# gcp_dmv_dow.py - Dow Theory Price-Action Pattern Detector

## Overview
This script detects **Dow Theory price-action patterns** (support/resistance, double/triple tops & bottoms, range breakouts, flags) using `scipy.signal.find_peaks`. Added in v4.8.0 (Phase 3 of the DMV indicator expansion). Outputs go to `FE_DOW_PATTERNS` and feed into `FE_DMV_ALL` via the FULL OUTER JOIN in `gcp_dmv_core.py`.

## Detected Patterns

### Value columns (raw price levels, nullable)
| Column | Type | Meaning |
|---|---|---|
| `m_dow_support` | `double precision` | Most recent twice-tested swing low within 60-bar window |
| `m_dow_resistance` | `double precision` | Most recent twice-tested swing high within 60-bar window |

NULL when no twice-tested level exists for the slug — ~60% of slugs are NULL on a given day, which is expected (most coins don't have a clean double-tested swing within 60 bars).

### Bin columns (signal, default=0)
| Column | Trigger | Direction |
|---|---|---|
| `m_dow_dbl_top_bin` | Two swing highs ~equal price | -1 |
| `m_dow_dbl_bot_bin` | Two swing lows ~equal price | +1 |
| `m_dow_trpl_top_bin` | Three swing highs ~equal price | -1 |
| `m_dow_trpl_bot_bin` | Three swing lows ~equal price | +1 |
| `m_dow_range_break_up_bin` | Close > prior resistance with >=1.3x avg volume | +1 |
| `m_dow_range_break_dn_bin` | Close < prior support with >=1.3x avg volume | -1 |
| `m_dow_flag_bin` | 10%+ move over prior 15 bars + tight 5-bar range (<=3%) | +1 / -1 |

## Detailed Functionality

### Swing detection
```python
from scipy.signal import find_peaks

DISTANCE = 5           # min separation between peaks (bars)
PRICE_TOLERANCE = 0.02 # 2% tolerance when comparing two peak/trough prices
BREAKOUT_VOL_RATIO = 1.3
LOOKBACK_BARS = 60
```
- `find_peaks(closes, distance=5)` -> swing highs
- `find_peaks(-closes, distance=5)` -> swing lows
- Two levels considered "equal" if their price difference is within 2% of either price

### Per-slug processing
```python
def _detect_per_slug(g):
    """Compute all Dow pattern values + bins for one slug's history."""
    g = g.reset_index(drop=True)
    n = len(g)
    out = {
        "m_dow_support": np.nan,
        "m_dow_resistance": np.nan,
        "m_dow_dbl_top_bin": 0,
        "m_dow_dbl_bot_bin": 0,
        # ...all bins default to 0
    }
    if n < DISTANCE * 2 + 1:
        return pd.Series(out)
    # ... detection logic on the last 60 bars
```
- Slugs with <11 bars: all values/bins return defaults (NaN for values, 0 for bins)
- Slugs with >=60 bars: detection runs on the trailing 60-bar window for relevance + performance

### Flag pattern logic
```python
if nW >= 20:
    recent5_high = highs[-5:].max()
    recent5_low  = lows[-5:].min()
    recent5_range_pct = (recent5_high - recent5_low) / max(recent5_low, 1e-9)

    prior15_close_start = closes[-20]
    prior15_close_end   = closes[-6]
    prior_move_pct = (prior15_close_end - prior15_close_start) / max(abs(prior15_close_start), 1e-9)

    if recent5_range_pct <= 0.03 and abs(prior_move_pct) >= 0.10:
        out["m_dow_flag_bin"] = 1 if prior_move_pct > 0 else -1
```
A flag is a sharp ~10%+ move followed by a tight (<=3%) 5-bar consolidation. The bin direction follows the prior move's direction.

### Output schema
```sql
CREATE TABLE "FE_DOW_PATTERNS" (
  id          bigint,             -- nullable
  slug        text NOT NULL,
  timestamp   timestamptz NOT NULL,
  m_dow_support     double precision,   -- nullable
  m_dow_resistance  double precision,   -- nullable
  m_dow_dbl_top_bin          bigint DEFAULT 0,
  m_dow_dbl_bot_bin          bigint DEFAULT 0,
  m_dow_trpl_top_bin         bigint DEFAULT 0,
  m_dow_trpl_bot_bin         bigint DEFAULT 0,
  m_dow_range_break_up_bin   bigint DEFAULT 0,
  m_dow_range_break_dn_bin   bigint DEFAULT 0,
  m_dow_flag_bin             bigint DEFAULT 0,
  PRIMARY KEY (slug, timestamp)
);
```

### v4.8.4 dtype fix
The script sets `out["id"] = pd.NA` to leave id NULL. Previously this created an `object`-dtype column of Python `None`, which pg8000 refused to bind for a bigint column (the `to_sql` failed silently with "in failed transaction block"). v4.8.4 changed it to:
```python
out["id"] = pd.array([pd.NA] * len(out), dtype="Int64")
```
Same intent (NULL in postgres), but a dtype pg8000 can serialize.

### v4.8.3 downstream filter
Because the script writes 2 value columns (`m_dow_support`, `m_dow_resistance`) that `FE_DMV_ALL`'s schema does NOT have, the `SELECT *` JOIN in `gcp_dmv_core.py` previously pulled them through and the FE_DMV_ALL INSERT failed. v4.8.3 added a dynamic `information_schema` filter in core.py that trims the df to only FE_DMV_ALL's columns before INSERT — see `knowledge_base/gcp_dmv_core.md`.

### Write semantics
- **dbcp**: TRUNCATE + INSERT (latest-per-slug snapshot)
- **cp_backtest**: DELETE-by-timestamp + INSERT (accumulates history)
- Both use `method="multi", chunksize=200` (v4.8.2)

## Integration Points
- **Upstream**: `1K_coins_ohlcv` (110-day window)
- **Downstream**: `FE_DOW_PATTERNS` -> `FE_DMV_ALL` (bin cols only — value cols are stripped by the v4.8.3 schema filter in core.py)
- **Pipeline position**: Stage 3.8 (after `gcp_dmv_candle.py`, before `gcp_dmv_levels.py`)
- **Score mapping**: All `m_dow_*_bin` columns -> `Momentum_Score`. The value cols `m_dow_support` / `m_dow_resistance` are pre-filtered out by core.py so they don't pollute the score with raw price magnitudes.

## Usage
```bash
python gcp_postgres_sandbox/technical_analysis/gcp_dmv_dow.py
# Runtime: ~1.5-2 minutes for 1000 cryptocurrencies
```

## Migration & Backfill
- Migration: `migrations/2026_05_11_phase3_dow.sql` (creates `FE_DOW_PATTERNS` + extends `FE_DMV_ALL` with the 7 bin cols, NOT the 2 value cols)
- Historical backfill: `scripts/backfill_phase3_5_cp_backtest.py`

## Dependencies
- `scipy>=1.13.0` — `scipy.signal.find_peaks` for swing detection
- `pandas>=2.2.2`, `numpy>=1.26.4`
- `sqlalchemy>=2.0.32`, `pg8000>=1.31.5`

## Key Features
1. **Swing-based detection** using `scipy.signal.find_peaks` rather than naive local-extrema scans
2. **Tolerance-based equality** (2%) for "twice-tested level" patterns
3. **Volume-confirmed breakouts** (1.3x 10-day average) — filters whipsaw noise
4. **60-bar rolling window** balances pattern recency with performance
5. **Bigint bins default to 0** — no NULL pollution into DMV scores
