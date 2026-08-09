# CP-011 Phase-A — Historical DMV: Point-in-Time (PIT) Audit

**Status:** CP-011 UNBLOCKING RECON (Phase A only) — no backtests run, no methodology
changed, no performance published.
**Worktree:** `C:\cpio_db\CryptoPrism-DB-cp011` · branch `feat/cp011-dmv-pit-recon` @ `origin/main` `2d6d4b7`
**CP-009:** frozen at `feat/cp009-phase2-golden-set` `e5d76a3`; PRs #33/#36 untouched.

All DB numbers below are **read-only** observations of `dbcp` (live) and `cp_backtest` (historical).

---

## 1. End-to-end historical DMV generation

Two independent paths produce DMV tables:

**Live path (dbcp — snapshot only):**
- Signal generators (`gcp_dmv_mom/osc/tvv/rat/candle/dow/levels/met.py`) each fetch
  `NOW() - INTERVAL '110 days'` (ratios: `'30 days'`) of OHLCV joined to
  `crypto_listings_latest_1000` (`cmc_rank <= 1000`), compute indicators, **keep only the
  latest timestamp per slug** (`groupby('slug')['timestamp'].idxmax()`), write `FE_*_SIGNALS`.
- `gcp_dmv_core.py` FULL-OUTER-JOINs **8** signal tables on `(slug, timestamp)`,
  `fillna(0)`, counts bullish/bearish/neutral bins, computes D/M/V scores
  (`sum(bins)/n*100`), TRUNCATE+INSERTs `FE_DMV_ALL`/`FE_DMV_SCORES` (dbcp), appends
  latest snapshot to `cp_backtest`.
- Observed: dbcp `FE_DMV_ALL`/`FE_DMV_SCORES`/`FE_PCT_CHANGE` = **1 date (2026-08-08)**, 1,000 rows.

**Backfill path (cp_backtest — the "historical" DMV):**
- `backtesting/backfill_cp_backtest.py` fetches **ALL** OHLCV (2013→now) joined to the
  **current** listings (`INNER JOIN crypto_listings_latest_1000`, `cmc_rank <= 1000`),
  recomputes momentum / oscillators / TVV / PCT / ratios (ratios use a **trailing 28-day
  window per date**), metrics (latest snapshot only), then `phase_core` FULL-OUTER-JOINs
  **only 5** signal tables (OSC, MOM, METRICS, TVV, RATIOS) → `FE_DMV_ALL`/`FE_DMV_SCORES`.
- Observed: `FE_DMV_ALL` = 99,203 rows / **4,835 dates** (2013-04-28..2026-08-08).

## 2. Confirmed leakage

1. **Full-sample VaR/CVaR look-ahead** — `gcp_dmv_pct.calculate_var_cvar`:
   `df.groupby('slug')['m_pct_1d'].quantile(1-confidence)` and the conditional-tail mean are
   computed over the **entire** fetched sample (2013→now) and merged onto **every** row of the
   slug. `backfill phase_pct` runs this over all history → every historical `d_pct_var`/
   `d_pct_cvar` embeds future returns. (Empirically: bitcoin shows **93 distinct** `d_pct_var`
   across its 4,587-row history = mixed/rebased full-sample snapshots; the 2015-01-05 stored
   value is **NULL** = inconsistent basis.)
2. **Metrics full-sample ATH/ATL + `now()`** — `gcp_dmv_met.py` and `backfill phase_metrics`:
   `groupby('slug')['high'].transform(lambda x: x.idxmax())` (full-series argmax) assigns the
   *future* ATH date to every historical row, and `days since ATH/ATL` / `coin age` use
   `pd.Timestamp.now()` instead of the row date.
3. **Current-universe survivorship bias** — both paths join OHLCV to
   `crypto_listings_latest_1000` (today's top-1000). History exists only for current
   survivors; delisted/dropped coins are absent. (Empirically: momentum signals have
   **5 slugs in 2013 → 1,315 in 2026**; raw `1K_coins_ohlcv` has 10 slugs in 2013 → 1,499 in
   2026.)

## 3. Neutral-filling, timestamp-alignment, score-range issues

- **Neutral filling inconsistent:** live `gcp_dmv_core.py` does `dropna(subset=[slug,timestamp])
  .fillna(0)` (silent phantom-neutral rows for slugs/dates missing from any signal table);
  backfill `phase_core` drops rows unless `slug='bitcoin'` OR every non-slug column is non-NaN
  (then `fillna(0)`). Different completeness semantics → live and historical DMV disagree.
- **Timestamp alignment:** FULL OUTER JOIN on exact `(slug, timestamp)`; `FE_METRICS_SIGNAL`
  is latest-snapshot only (**113 dates, 2026-03-29..08-08**), so the historical join is
  misaligned by construction; live writes a `23:59:59` snapshot timestamp; per-phase warmup
  drops first rows → NaN → dropped (backfill) or zero-filled (live).
- **Score range/comparability:** score = `sum(bins)/(n_cols-1)*100` over `{-1,0,1}` bins.
  Observed 2026-08-08 ranges: Durability **-56.9..101.97**, Momentum -44.7..39.5,
  Valuation -100..100. **Durability > 100 is impossible for pure bins** → non-bin/value
  columns leak into the `d_`/`m_` column set (and the `df.iloc[:, 4:]` positional slice is
  fragile). Backfill core joins **5** signal tables vs live **8** → historical scores omit
  candlestick/DOW/price-levels signals and are not comparable to live scores.

## 4. Consequences for history (observed data)

- `FE_METRICS_SIGNAL` covers only 113 dates → the completeness filter drops non-bitcoin rows
  for every earlier date → **historical `FE_DMV_ALL` is effectively bitcoin-only**:
  (2025 = 363 rows ≈ 1/day; `FE_DMV_ALL` has **0 slugs on 2015-01-05**; ~20 rows/date on
  average across 4,835 dates). Cross-sectional DMV backtesting on history is not possible
  with the current tables.
- `FE_PCT_CHANGE` historical VaR/CVaR is structurally future-contaminated and numerically
  inconsistent (93 distinct var levels per slug; NULLs).

## 5. PIT-safe parts (already correct)

- Ratios phase: **trailing 28-day window per date** (`window = [date-28d, date]`) — the
  pattern to follow.
- Momentum/oscillator/TVV rolling indicators: no `shift(-n)`/`center=True` in active modules.
- `m_pct_1d`, `d_pct_cum_ret`, `d_pct_vol_1d`: past-based.

## 6. Reconstruction options

1. **Trailing-window VaR/CVaR:** `d_pct_var(t) = quantile_0.05(m_pct_1d over [t-W, t])` and
   CVaR = mean of that window's tail, computed per date (mirror ratios). W default 365d,
   min sample 252d; below min → NULL (and exclude from bins), never full-sample.
2. **Rolling ATH/ATL + row-date:** keep `v_met_ath/atl` as `cummax/cummin` (PIT), drop the
   full-series `idxmax/idxmin` transform, and compute days-since vs the **row** timestamp,
   not `now()`.
3. **PIT universe:** replace the `crypto_listings_latest_1000` join with a per-date universe —
   options: (a) historical CMC listings snapshots table; (b) `(slug, listed_at, last_seen)`
   interval table; (c) approximate from `1K_coins_ohlcv` first/last seen. Every date's rows
   must be restricted to the universe as of that date.
4. **Align paths:** use the same 8-table join + same completeness/fill rule in live and
   backfill; emit per-date rows for ALL signal tables (fix metrics to be historical, not a
   snapshot).

## 7. Regression tests + required data

**Tests (Phase B):**
- `test_var_cvar_pit`: for random `(slug, date)`, `d_pct_var` equals the trailing-window
  quantile computed only on rows ≤ date (property test; assert no future rows contribute).
- `test_universe_pit`: every `FE_DMV_ALL` row at date `d` has its slug in the PIT universe
  for `d`; a delisted-only slug is absent from earlier dates.
- `test_score_range`: D/M/V ∈ [-100, 100] for all rows; reject >100 contamination.
- `test_neutral_fill`: missing-signal handling is consistent and explicit (drop vs flagged),
  no silent phantom-neutral rows.
- `test_timestamp_alignment`: `FE_DMV_ALL` dates == intersection of all 8 signal-table dates;
  no date present from only one table.
- `test_live_backfill_comparability`: live and backfill use the same signal set + rules.
- `test_determinism`: same input → identical output.

**Required data:**
- Point-in-time universe (per-date listings or slug life interval).
- ≥252 trading days of `m_pct_1d` per slug for trailing VaR/CVaR (slugs below min → excluded).
- Historical per-date ATH/ATL (or recompute via cummax/cummin from OHLCV).
- Historical metrics (or drop `v_met_*` from historical DMV if not reconstructable).

## 8. Implementation plan (PIT-safe DMV history)

- **A1 — VaR/CVaR PIT:** rewrite `calculate_var_cvar` to per-date trailing windows; update
  `gcp_dmv_pct.py` (live) and `backfill phase_pct` to the same function.
- **A2 — Metrics PIT:** rolling ATH/ATL + row-date days-since; make metrics emit per-date rows.
- **A3 — PIT universe:** add universe table; join OHLCV/signals on PIT membership; backfill.
- **A4 — Align core:** 8-table join + consistent completeness/fill in `gcp_dmv_core.py` and
  `backfill phase_core`; fix `iloc[:, 4:]` and score column selection (bin-only, [-100,100]).
- **B — Validate:** run the §7 test battery; cross-check historical vs live on overlap dates.
- **C — Gate:** CP-011 backtests consume only PIT-safe history; publish only PIT results.

## 9. Blocking decisions needed

- VaR/CVaR window (default 365d) and min-sample (default 252d) for trailing computation.
- PIT universe source (CMC snapshots vs slug-life interval vs OHLCV-derived approximation).
- Whether to backfill 2013+ in full or start later (early years are ~5-30 surviving slugs).
- Score normalization: keep bins `[-100,100]` and fix contamination, or switch to z-scores.
- Metrics handling: reconstruct historical per-date metrics or drop `v_met_*` from historical DMV.
