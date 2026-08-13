# CP-011 Initiative Summary — PIT-safe DMV Shadow Rebuild + Validation

Branch: `feat/cp011-phase-d-hybrid-rebuild` · Repo: `CryptoPrism-DB` · Final commit: `bb7e87a`

## What CP-011 delivered

A **point-in-time-safe Daily Market Value (DMV) dataset** regenerated from raw
`1K_coins_ohlcv`, built as a versioned shadow schema (canonical `FE_*`/production
tables untouched), then validated for predictive power on forward returns.

### 1. Stage 2B — shadow rebuild (executed on AWS Fargate, 2026-08-12)
| Item | Value |
|---|---|
| Shadow schema | `pit_dmv_cp011_v2_20260812_1315` (database **cp_backtest**) |
| Methodology | `cp011_pit_v2_current_main_full_regen` (current-main TA functions, pinned pandas 2.3.1) |
| Frozen snapshot | 2,603,883 rows / **3,587 assets** / 2,593,976 distinct (slug,date), 2013-04-28…2026-08-11 |
| **dmv_rows written** | **2,593,976** (exactly = frozen universe; 0 duplicate PKs) |
| Incomplete | 30,809 (1.19%) — delisted/sparse/early-history, NULL not zero-filled |
| Scores | durability / momentum / valuation in [-100, 100] |
| Risk | PIT VaR/CVaR (trailing 365d, min 252 obs; NULL before) |
| Stage 3 validation | **PASS** (all 7 checks) |
| Canonical writes | **none** |

Artifacts: `s3://cryptoprism-cp011-artifacts/cp011/stage2b/` (frozen parquet+manifest,
osc_bins, stage3, backtests, recommendations, robustness, simulation, logs) +
CloudWatch `/ecs/cryptoprism-onchain/cp011-stage2b`.

### 2. CP-012 — forward-return backtests (AWS Fargate 16 vCPU/64GB)
- Panel 2,024,772 (slug,date) rows; leakage-safe; 30 experiments (10 PIT factors × 1/7/30d).
- 1 material effect: **`d_pct_cvar` h30** (IC −0.054, CI [−0.059,−0.048], FDR-sig).

### 3. CP-020 — promotion recommendation (recommendation only)
- 1 PROMOTION_CANDIDATE (`d_pct_cvar` h30), 29 NEEDS_MORE_VALIDATION, 0 REJECTED.

### 4. CP-018 — robustness + decision phase
- **`d_pct_cvar` h30**: IC robust OOS + regime-consistent, BUT liquid-universe long/short
  simulation (top-200, 30d rebalance, 10bps/side, 105 periods) shows **median −3.6%/mo,
  Sharpe 0.26, hit-rate 38%** → real **risk intelligence, not tradable alpha**.
- **`mvrv`**: dbcp has only monthly realized-cap/mvrv (231 rows/12y); no daily source;
  not computable from available data → **held until sufficient daily history exists**.
- **Decision: do NOT promote the full 2.59M-row table to canonical yet.**

### 5. Sibling reconciliation
Two distinct implementations reconciled: the validated `pit_dmv_*` shadow (cp_backtest)
is the authoritative CP-011/CP-012 source; the sibling factor-research frame (dbcp)
was never executed, its empty `cp011_shadow` shell was dropped. See
`docs/CP011-sibling-reconciliation.md`.

## Standing gates
- **No canonical promotion** of the DMV table without explicit approval + a consumer.
- No push/PR of this branch until this close-out (now being executed).

## Next (separate CP-018 serving-layer initiative)
Materialize the **latest** DMV values for the top-100 assets (scores + CVaR risk +
freshness + methodology + confidence; incomplete → NULL with reasons) into a small
fast-serving snapshot in RDS/cache, target API P95 < 2s; full historical panel stays
in the research/S3 layer.
