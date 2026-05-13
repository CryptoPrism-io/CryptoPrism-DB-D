Note: All sensitive configuration values are redacted in this document.

# gcp_dmv_core.py - Central Signal Aggregation Hub

## Overview
This script is the **final aggregation engine** of the CryptoPrism-DB system, merging all technical analysis signals from 8 different signal tables into unified DMV (Durability, Momentum, Valuation) scores and a comprehensive signal matrix.

As of v4.8.0 the JOIN spans 8 tables (was 5 prior to v4.8.0).

## Detailed Functionality

### Signal Collection -- FULL OUTER JOIN of 8 tables

```sql
SELECT *
FROM "FE_OSCILLATORS_SIGNALS" AS o
FULL OUTER JOIN "FE_MOMENTUM_SIGNALS"     AS m  USING (slug, timestamp)
FULL OUTER JOIN "FE_METRICS_SIGNAL"       AS me USING (slug, timestamp)
FULL OUTER JOIN "FE_TVV_SIGNALS"          AS t  USING (slug, timestamp)
FULL OUTER JOIN "FE_RATIOS_SIGNALS"       AS r  USING (slug, timestamp)
FULL OUTER JOIN "FE_CANDLESTICK_SIGNALS"  AS c  USING (slug, timestamp)  -- v4.8.0
FULL OUTER JOIN "FE_DOW_PATTERNS"         AS d  USING (slug, timestamp)  -- v4.8.0
FULL OUTER JOIN "FE_PRICE_LEVELS"         AS l  USING (slug, timestamp); -- v4.8.0
```

`FULL OUTER JOIN` means rows missing from any side contribute NULL for that side's columns. The downstream `.fillna(0)` converts those to neutral signals.

### Data Processing Pipeline

#### 1. Data Cleaning (v4.8.1 + v4.8.3 fixes)
```python
df = (
    df.loc[:, ~df.columns.duplicated()]
      .dropna(subset=['slug', 'timestamp'])     # v4.8.1
      .fillna(0)                                 # NaN -> neutral signal
)
```

**v4.8.1 fix**: the previous filter `d[d['slug'].eq('bitcoin') | d.drop(columns='slug').notna().all(axis=1)]` was collapsing `FE_DMV_ALL` to a single row (bitcoin) whenever any joined table lagged or had NaN. Replaced with a minimal `dropna(subset=['slug','timestamp'])` -- the existing `.fillna(0)` already handles missing signals correctly.

#### 2. Bullish/Bearish/Neutral Counts
```python
for col in ['bullish', 'bearish', 'neutral']:
    df[col] = 0

for index, row in df.iloc[:, 4:].iterrows():
    df.at[index, 'bullish'] = (row == 1).sum()
    df.at[index, 'bearish'] = (row == -1).sum()
    df.at[index, 'neutral'] = (row == 0).sum()
```
Counts each row's signal columns by their bin value. Identifier columns (`slug`, `timestamp`, `id`, `name`) are skipped via the `iloc[:, 4:]` slice.

#### 3. v4.8.3 schema filter (FE_DMV_ALL write only)
```python
with gcp_engine.connect() as conn:
    fe_dmv_all_cols = {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='FE_DMV_ALL'"
    )).fetchall()}
df = df[[c for c in df.columns if c in fe_dmv_all_cols]]
```
**v4.8.3 fix**: the v4.8.0 JOIN extension pulled 12 value columns from `FE_DOW_PATTERNS` (m_dow_support, m_dow_resistance) and `FE_PRICE_LEVELS` (fib_swing_*, fib_0_*, atr_band_*) through `SELECT *`, but `FE_DMV_ALL`'s schema was extended with only the bin columns. The INSERT errored with column-not-found, hidden behind pg8000's "in failed transaction block" message. The schema filter dynamically discovers FE_DMV_ALL's columns from `information_schema` and trims the df to match.

#### 4. DMV Score Calculation

Scores are computed via prefix-based column mapping after the schema filter (so raw value columns like `m_dow_support` are already removed and don't pollute the Momentum sum).

```python
Durability = df[['slug'] + [c for c in df.columns if c.startswith('d_')]]
Momentum   = df[['slug'] + [c for c in df.columns if c.startswith('m_')]]
Valuation  = df[['slug'] + [c for c in df.columns if c.startswith('v_')]]

Durability['Durability_Score'] = Durability.drop('slug', axis=1).sum(axis=1) / (Durability.shape[1] - 1) * 100
Momentum  ['Momentum_Score']   = Momentum  .drop('slug', axis=1).sum(axis=1) / (Momentum.shape[1] - 1) * 100
Valuation ['Valuation_Score']  = Valuation .drop('slug', axis=1).sum(axis=1) / (Valuation.shape[1] - 1) * 100
```

Each score is a normalized 0-100 value (`sum / N-1 * 100`) where N is the number of columns. The denominator auto-scales as new bins land in each bucket.

| Score | Prefix | Includes (v4.8.x) |
|---|---|---|
| Durability | `d_` | risk metrics, drawdown, beta, pain ratio, SMA/EMA crossovers |
| Momentum | `m_` | RSI, ROC, Williams %R, OBV, BB, Supertrend, Aroon, candlesticks, dow patterns, fib proximity, ATR band |
| Valuation | `v_` | Sharpe, Sortino, Treynor, common-sense, information, win/loss ratios |

#### 5. Per-slug latest filtering (v4.4.x)
```python
df = df.loc[df.groupby('slug')['timestamp'].idxmax()]
```
Keeps only the most recent timestamp **per slug** rather than a global max. Prevents dropping coins whose OHLCV is slightly behind the newest coin's bar.

### Output Tables

#### FE_DMV_ALL (Complete Signal Matrix)
- **Content**: All bin/signal columns (61 cols as of v4.8.0) + bullish/bearish/neutral counts
- **Schema**: PK on (slug, timestamp). Bin columns only — value columns are filtered out by the v4.8.3 schema filter.
- **Write mode (dbcp)**: TRUNCATE + INSERT (latest-per-slug snapshot)
- **Write mode (cp_backtest)**: DELETE-by-timestamp + INSERT (accumulates history)
- **Batching (v4.8.2)**: `method='multi', chunksize=200` -- ~10x faster than the prior row-by-row default

#### FE_DMV_SCORES (Normalized 0-100 scores)
```python
dmv_scores = pd.DataFrame({
    'slug': df['slug'],
    'timestamp': df['timestamp'],
    'Durability_Score': Durability['Durability_Score'],
    'Momentum_Score':   Momentum['Momentum_Score'],
    'Valuation_Score':  Valuation['Valuation_Score'],
})
dmv_scores.to_sql('FE_DMV_SCORES', con=gcp_engine, if_exists='append',
                  index=False, method='multi', chunksize=BATCH_SIZE)
```
`BATCH_SIZE = 10000`. Same TRUNCATE+INSERT pattern on dbcp; DELETE-by-timestamp+INSERT on cp_backtest.

### Error Handling & Monitoring
Comprehensive logging with both file (`app.log`) and console output. Database operations wrapped in try/except around the score and cp_backtest writes. The FE_DMV_ALL write is unprotected — its failure is a hard stop.

### Integration Points
- **Upstream**: 8 signal tables (must all complete first)
  - `FE_OSCILLATORS_SIGNALS` (gcp_dmv_osc.py)
  - `FE_MOMENTUM_SIGNALS` (gcp_dmv_mom.py)
  - `FE_METRICS_SIGNAL` (gcp_dmv_met.py)
  - `FE_TVV_SIGNALS` (gcp_dmv_tvv.py)
  - `FE_RATIOS_SIGNALS` (gcp_dmv_rat.py)
  - `FE_CANDLESTICK_SIGNALS` (gcp_dmv_candle.py) -- v4.8.0
  - `FE_DOW_PATTERNS` (gcp_dmv_dow.py) -- v4.8.0
  - `FE_PRICE_LEVELS` (gcp_dmv_levels.py) -- v4.8.0
- **Pipeline position**: **Final step** in DMV workflow (after the 10 indicator scripts)
- **Downstream**: Feeds the QA system and downstream API/trading consumers

### Database Schema
```sql
-- FE_DMV_ALL: 61 columns (post v4.8.0). All bin/signal cols + 3 counters.
slug, timestamp, id, name,
m_osc_*, m_mom_*, m_pct_*, m_tvv_*, m_rat_*, m_cnd_*, m_dow_*_bin, m_lvl_*_bin,
d_pct_*, d_met_*, d_tvv_*, d_rat_*,
v_rat_*,
bullish, bearish, neutral

-- FE_DMV_SCORES: normalized aggregates
slug, timestamp, "Durability_Score", "Momentum_Score", "Valuation_Score"
```

## Usage
```bash
python gcp_postgres_sandbox/technical_analysis/gcp_dmv_core.py
# Runtime: ~1.5-2 minutes (post v4.8.2 batched INSERTs); previously ~5-10 min
```

## Dependencies
- `pandas>=2.2.2` -- aggregation
- `numpy>=1.26.4`
- `sqlalchemy>=2.0.32`, `pg8000>=1.31.5` -- DB connectivity (note: pg8000 surfaces INSERT errors poorly; see v4.8.3/v4.8.4 changelog entries)
- `time`, `logging`

## Recent Changes
- **v4.8.4** (2026-05-13): pd.NA dtype fix in upstream candle/dow/levels scripts (id column). Indirectly required for core to receive valid input.
- **v4.8.3** (2026-05-13): Added dynamic FE_DMV_ALL schema filter (line 95-99). Drops 12 phantom value columns introduced by the v4.8.0 JOIN extension.
- **v4.8.2** (2026-05-13): FE_DMV_ALL write now uses `method='multi', chunksize=200`.
- **v4.8.1** (2026-05-13): Replaced the bitcoin-OR-non-null filter with `dropna(subset=['slug','timestamp'])`. Fixed FE_DMV_ALL collapsing to 1 row.
- **v4.8.0** (2026-05-11): JOIN extended from 5 to 8 tables to incorporate new candlestick, Dow, and Fibonacci/ATR signals.

## Key Outputs
1. **Signal aggregation**: 100+ technical indicators merged into a unified view
2. **DMV scoring**: Normalized 0-100 scores for systematic ranking
3. **Bullish/bearish/neutral counts**: Quick assessment of overall stance per slug
4. **Per-slug latest snapshot** on dbcp, **timestamped history** on cp_backtest
