# CP-011 Phase D — Stage 1.5 Diagnosis: Mixed-Build Boundary (Path A invalidated)

**Status:** DIAGNOSIS COMPLETE — **mixed-build boundary found** → existing FE_*_SIGNALS are
**not one reproducible historical methodology**. Path A cannot reach zero genuine mismatches.
Recommend **Path B**. No benchmark, shadow build, push, PR, backtest or promotion started.
**Branch:** `feat/cp011-phase-d-hybrid-rebuild` (local) · main `c5cbfb8` · CP-009 frozen `e5d76a3`

## 1. Provenance evidence

- Existing tables **have** `m_osc_supertrend_bin`, `m_osc_aroon_bin`, `m_tvv_bb_bin` →
  table schema was created by **post-70ea2b9 (v4.8.0+) code**.
- `git diff 4223a87..HEAD` proves `calculate_adx`/`calculate_cmf`/`calculate_obv` were
  **NOT changed** by 70ea2b9 or any later commit (70ea2b9 only added BB/Supertrend/Aroon;
  9644a09 was perf-only). So current ADX/CMF/OBV == the build-time formulas.
- Yet regeneration with current code **disagrees** with existing values in specific regions →
  the disagreement is NOT a formula-version change; it is a **mixed build**:
  - historical backfill rows (full-history),
  - recent rows appended by the **live pipeline using a truncated ~110-day OHLCV window**
    (`NOW() - INTERVAL '110 days'` fetch in gcp_dmv_tvv.py / gcp_dmv_osc.py),
  - plus per-asset **series-start initialization drift** in ADX.
- No per-row provenance column exists in the tables; the exact build-time window/code for each
  row is unrecoverable.

## 2. Diagnostic evidence (representative mismatches, raw values)

**CMF (cumulative ADL → path-dependent):** aave 2026 CMF mismatches vs existing:
- full-history regeneration: **98 / 192** (51%)
- 110-day-window regeneration: **8 / 87** (9%)
→ the 2026 rows were written by the live pipeline from a truncated window.

**OBV (cumulative → path-dependent):** 266 mismatches, **252 in 2026 + 14 in 2025** — same
live-window region.

**ADX (Wilder `ewm(alpha=1/14, adjust=False)`):** 455 mismatches (bitcoin/ethereum/litecoin):
- by year: {2013:220, 2015:137, 2016:41, 2025:9, 2026:48}
- NOT boundary cases: median |ADX−20| = **78** (ADX ≈ 90–98), |+DI − −DI| median = 1641.
- Example bitcoin 2013-05-01: regenerated +DI=1.27, −DI=9.41 → bin **−1** (correct per
  formula); existing bin **+1** → the existing early rows used a **different ADX
  initialization**.
- 2013/2015/2016/2020-21 dates coincide with each asset's **series start** (bitcoin/LTC 2013,
  ethereum 2015-16, aave 2020-21).

## 3. Quantified affected intervals (mixed-build boundary)

| Interval | Cause | Affected signals |
|---|---|---|
| Per-asset **series-start** (~first ~1 year of each asset's history) | ADX initialization version drift in the build that wrote those rows | ADX bins |
| **Recent ~last 1–1.5 years** (≈2025-06 → 2026-08; live-pipeline append region) | live 110-day truncated window (cumulative path-dependence) | CMF, OBV |
| Throughout | current generators emit `0` where legacy tables had NULL | (regen-val/exist-NULL = 6,679; explained) |

These intervals **overlap and have no per-row provenance** → the tables cannot be reproduced
exactly by any single pinned code version.

## 4. Verdict

- Outcome **2 (mixed-build boundary)** applies: the existing FE_*_SIGNALS tables are NOT one
  reproducible historical methodology. Path A (recover one exact implementation) **cannot**
  reach zero genuine mismatches.
- **Recommendation: Path B** — version the current-main regeneration as a new canonical
  methodology (`cp011_pit_v1` stays the methodology label; the regeneration layer
  `pit/regenerate.py` is the canonical signal source) and **rebuild the entire universe**
  (covered + uncovered) under one consistent methodology. Path C remains unsuitable (deliberate
  definition mixing). Path A is only valid if a future exact-build reproduction reaches zero
  genuine mismatches (not the case here).

## 5. Not done (as directed)

No Stage-1.5 benchmark, no shadow build, no push/PR/merge/backtest/promotion. Canonical tables
+ Phase C dry-run schema untouched.
