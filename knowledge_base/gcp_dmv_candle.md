# gcp_dmv_candle.py - Candlestick Pattern Detection Engine

## Overview
This script implements **9 directional candlestick pattern detectors** from Zerodha Varsity Module 2 as plain vectorised pandas/numpy. Added in v4.8.0 (Phase 2 of the DMV indicator expansion). Outputs go to `FE_CANDLESTICK_SIGNALS` and feed into `FE_DMV_ALL` via the FULL OUTER JOIN in `gcp_dmv_core.py`.

Non-directional patterns (Doji, Spinning Top) are intentionally skipped — they convey indecision rather than direction and would distort DMV bullish/bearish counts if encoded as +/-1.

## The Nine Patterns

Each detector writes a single column `m_cnd_<name>_bin` taking values in `{-1, 0, +1}`:

| Pattern | Bin Column | Direction | Logic |
|---|---|---|---|
| Marubozu | `m_cnd_marubozu_bin` | +1 / -1 | Body fills >=95% of range; tiny shadows |
| Hammer | `m_cnd_hammer_bin` | +1 | Paper umbrella after downtrend (long lower shadow, small body up top) |
| Hanging Man | `m_cnd_hanging_man_bin` | -1 | Paper umbrella after uptrend (same geometry, bearish context) |
| Shooting Star | `m_cnd_shooting_star_bin` | -1 | Inverted paper umbrella after uptrend (long upper shadow) |
| Engulfing | `m_cnd_engulfing_bin` | +1 / -1 | Current body fully engulfs prior body with opposite direction |
| Harami | `m_cnd_harami_bin` | +1 / -1 | Inside bar opposite-coloured to prior body (mother-with-baby) |
| Piercing | `m_cnd_piercing_bin` | +1 | Bullish reversal: red bar then blue bar closing above prior midpoint |
| Dark Cloud Cover | `m_cnd_dark_cloud_bin` | -1 | Bearish reversal: blue bar then red bar closing below prior midpoint |
| Star | `m_cnd_star_bin` | +1 / -1 | Three-bar morning star (+1) / evening star (-1) |

## Detailed Functionality

### Prior-trend gate
Several patterns (Hammer, Hanging Man, Shooting Star, Piercing, Dark Cloud, Star) only register inside an existing trend. Trend proxy is fast and cheap:
```python
def add_prior_trend_flags(df):
    prev_close   = df.groupby("slug")["close"].shift(1)
    close_5_back = df.groupby("slug")["close"].shift(5)
    df["_prior_up"]   = (prev_close > close_5_back)
    df["_prior_down"] = (prev_close < close_5_back)
```
Requires >=5 prior bars per slug; NaN otherwise.

### Per-bar geometry helpers
```python
def add_geometry(df):
    df["_body"]  = (df["close"] - df["open"]).abs()
    df["_us"]    = df["high"] - df[["open", "close"]].max(axis=1)   # upper shadow
    df["_ls"]    = df[["open", "close"]].min(axis=1) - df["low"]    # lower shadow
    df["_rng"]   = df["high"] - df["low"]
    df["_red"]   = df["close"] < df["open"]
    df["_blue"]  = df["close"] > df["open"]
```

### Tolerances (constraint #2 from Zerodha module: "be flexible — quantify and verify")
- Marubozu: shadows up to 5% of range allowed
- Hammer/Hanging Man: body in upper 1/3 of range; lower shadow >=2x body
- Shooting Star: body in lower 1/3 of range; upper shadow >=2x body
- Harami: child body fully inside parent body (open and close both within parent's open/close)
- Engulfing: child body fully engulfs parent body
- Piercing/Dark Cloud: penetrates >=50% of prior body

### Output schema
```sql
CREATE TABLE "FE_CANDLESTICK_SIGNALS" (
  id          bigint,             -- nullable, carries through from OHLCV when available
  slug        text NOT NULL,
  timestamp   timestamptz NOT NULL,
  m_cnd_marubozu_bin       bigint DEFAULT 0,
  m_cnd_hammer_bin         bigint DEFAULT 0,
  m_cnd_hanging_man_bin    bigint DEFAULT 0,
  m_cnd_shooting_star_bin  bigint DEFAULT 0,
  m_cnd_engulfing_bin      bigint DEFAULT 0,
  m_cnd_harami_bin         bigint DEFAULT 0,
  m_cnd_piercing_bin       bigint DEFAULT 0,
  m_cnd_dark_cloud_bin     bigint DEFAULT 0,
  m_cnd_star_bin           bigint DEFAULT 0,
  PRIMARY KEY (slug, timestamp)
);
```

All 9 bin columns default to `0` (neutral) using `np.select(default=0)` — they never carry NULL, which keeps `gcp_dmv_core.py`'s NaN-handling cheap.

### Write semantics
- **dbcp**: TRUNCATE + INSERT (latest-per-slug snapshot)
- **cp_backtest**: DELETE-by-timestamp + INSERT (accumulates history)
- Both use `method="multi", chunksize=200` (v4.8.2) for ~10x faster writes

## Integration Points
- **Upstream**: `1K_coins_ohlcv` (110-day window)
- **Downstream**: `FE_CANDLESTICK_SIGNALS` -> `FE_DMV_ALL` via JOIN in `gcp_dmv_core.py`
- **Pipeline position**: Stage 3.7 (after `gcp_dmv_rat.py`, before `gcp_dmv_dow.py`)
- **Score mapping**: All `m_cnd_*` bins start with `m_` -> aggregated into `Momentum_Score`

## Usage
```bash
python gcp_postgres_sandbox/technical_analysis/gcp_dmv_candle.py
# Runtime: ~1.5-2 minutes for 1000 cryptocurrencies (98k OHLCV rows)
```

## Migration & Backfill
- Migration: `migrations/2026_05_11_phase2_candlestick.sql` (creates `FE_CANDLESTICK_SIGNALS` + extends `FE_DMV_ALL` with the 9 bin cols)
- Historical backfill: `scripts/backfill_phase2_cp_backtest.py` (psycopg2 COPY for ~2.5 min over 1.13M rows)

## Dependencies
- `pandas>=2.2.2` — vectorised pattern detection
- `numpy>=1.26.4` — np.select for bin assignment
- `sqlalchemy>=2.0.32`, `pg8000>=1.31.5` — database connectivity

## Key Features
1. **9 directional patterns** covering Zerodha Module 2's full candle catalogue
2. **No TA-Lib dependency** — avoids the GHA build pain entirely
3. **Bigint bin convention** — `default=0` (no NULLs, no NaN propagation)
4. **Prior-trend gate** keeps Hammer/Hanging Man semantics correct
5. **Tolerance-based geometry** — "approximately" rather than "exactly" matches real-world candles
