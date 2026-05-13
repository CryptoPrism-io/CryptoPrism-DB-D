# gcp_dmv_levels.py - Fibonacci Retracements & ATR Bands

## Overview
This script computes **Fibonacci retracement levels** and an **ATR-based volatility envelope** per slug. Added in v4.8.0 (Phase 5 of the DMV indicator expansion — the final indicators from Zerodha Varsity Module 2). Outputs go to `FE_PRICE_LEVELS` and feed into `FE_DMV_ALL` via the FULL OUTER JOIN in `gcp_dmv_core.py`.

## Computed Levels

### Value columns (raw price levels, nullable)
| Column | Type | Meaning |
|---|---|---|
| `fib_swing_low` | `double precision` | Most recent swing low (anchor for retracement) |
| `fib_swing_high` | `double precision` | Most recent swing high (anchor for retracement) |
| `fib_0_236` | `double precision` | 23.6% retracement level |
| `fib_0_382` | `double precision` | 38.2% retracement level |
| `fib_0_500` | `double precision` | 50% retracement level |
| `fib_0_618` | `double precision` | 61.8% retracement level (golden ratio) |
| `fib_0_786` | `double precision` | 78.6% retracement level |
| `atr_band_mid` | `double precision` | 5-period SMA of close |
| `atr_band_upper` | `double precision` | mid + 3 * ATR(14) |
| `atr_band_lower` | `double precision` | mid - 3 * ATR(14) |

NULL on slugs with <16 bars (need ATR period + 2). On 1000 coins, typically ~36-38 NULLs.

### Bin columns (signal, default=0)
| Column | Trigger | Direction |
|---|---|---|
| `m_lvl_fib_proximity_bin` | Close within 1% of `fib_0_382` or `fib_0_618` | +1 (buying retracement) |
| `m_lvl_fib_proximity_bin` | Close within 1% of `fib_0_236` | -1 (shallow rejection) |
| `m_lvl_atr_band_bin` | Close < `atr_band_lower` | +1 (oversold envelope) |
| `m_lvl_atr_band_bin` | Close > `atr_band_upper` | -1 (overbought envelope) |

## Detailed Functionality

### Constants
```python
SWING_DISTANCE = 5      # find_peaks min separation
LOOKBACK       = 60     # bars used per slug
PROXIMITY_PCT  = 0.01   # 1% band around fib levels
ATR_PERIOD     = 14
SMA_PERIOD     = 5
ATR_BAND_MULT  = 3.0
```

### Swing anchors
```python
high_idx, _ = find_peaks(closes,  distance=SWING_DISTANCE)
low_idx,  _ = find_peaks(-closes, distance=SWING_DISTANCE)

if len(high_idx) >= 1 and len(low_idx) >= 1:
    anchor_high_idx = int(high_idx[-1])
    anchor_low_idx  = int(low_idx[-1])
    # Order them so high > low
    if anchor_high_idx > anchor_low_idx:
        swing_low  = float(closes[anchor_low_idx])
        swing_high = float(closes[anchor_high_idx])
        # ... compute fib retracement levels
```
Most recent peak + trough within the 60-bar window are the swing anchors. Retracement levels are computed from there.

### ATR-band envelope
The mid line is a 5-SMA of close (short-term, responsive). The upper/lower bands are mid +/- 3 * ATR(14). At 3 sigma, breaches signal genuine overbought/oversold rather than ordinary noise.

### Output schema
```sql
CREATE TABLE "FE_PRICE_LEVELS" (
  id          bigint,             -- nullable
  slug        text NOT NULL,
  timestamp   timestamptz NOT NULL,
  fib_swing_low      double precision,
  fib_swing_high     double precision,
  fib_0_236          double precision,
  fib_0_382          double precision,
  fib_0_500          double precision,
  fib_0_618          double precision,
  fib_0_786          double precision,
  atr_band_mid       double precision,
  atr_band_upper     double precision,
  atr_band_lower     double precision,
  m_lvl_fib_proximity_bin   bigint DEFAULT 0,
  m_lvl_atr_band_bin        bigint DEFAULT 0,
  PRIMARY KEY (slug, timestamp)
);
```

### v4.8.4 dtype fix
Same pattern as `gcp_dmv_dow.py`: `out["id"] = pd.NA` was producing an object-dtype column of Python `None` which pg8000 refused for bigint binding. v4.8.4 uses `pd.array([pd.NA] * len(out), dtype="Int64")`.

### v4.8.3 schema filter
`FE_DMV_ALL` only holds the 2 bin columns from this table -- the 10 value columns (fib_swing_*, fib_0_*, atr_band_*) are NOT in FE_DMV_ALL's schema. The schema filter in `gcp_dmv_core.py` (v4.8.3) drops these from the df before INSERT to FE_DMV_ALL.

### Write semantics
- **dbcp**: TRUNCATE + INSERT (latest-per-slug snapshot)
- **cp_backtest**: DELETE-by-timestamp + INSERT (accumulates history)
- Both use `method="multi", chunksize=200` (v4.8.2)

## Integration Points
- **Upstream**: `1K_coins_ohlcv` (110-day window)
- **Downstream**: `FE_PRICE_LEVELS` -> `FE_DMV_ALL` (only the 2 bin cols flow through; value cols stripped by core.py's schema filter)
- **Pipeline position**: Stage 3.9 (after `gcp_dmv_dow.py`, before `gcp_dmv_core.py` — the final analytical step)
- **Score mapping**: `m_lvl_fib_proximity_bin` and `m_lvl_atr_band_bin` -> `Momentum_Score`

## Usage
```bash
python gcp_postgres_sandbox/technical_analysis/gcp_dmv_levels.py
# Runtime: ~1.5-2 minutes for 1000 cryptocurrencies
```

## Migration & Backfill
- Migration: `migrations/2026_05_11_phase5_levels.sql` (creates `FE_PRICE_LEVELS` + extends `FE_DMV_ALL` with the 2 bin cols, NOT the 10 value cols)
- Historical backfill: `scripts/backfill_phase3_5_cp_backtest.py`

## Dependencies
- `scipy>=1.13.0` — `scipy.signal.find_peaks` for swing detection
- `pandas>=2.2.2`, `numpy>=1.26.4`
- `sqlalchemy>=2.0.32`, `pg8000>=1.31.5`

## Key Features
1. **Standard Fibonacci ratios** (0.236, 0.382, 0.5, 0.618, 0.786) anchored on detected swings
2. **ATR-band envelope** at 3 sigma for true overbought/oversold signals
3. **1% proximity tolerance** on Fib levels — captures "near level" without false-positive noise
4. **Most-recent-swing anchoring** — Fib levels update as new swings form, not frozen on first peak/trough
5. **Bigint bins default to 0** — no NaN propagation into DMV scores
