# CP-020 — Final Promotion Recommendation (RECOMMENDATION ONLY)

Date: 2026-08-13 · Source: `report/cp011-phase-d/cp020_recommendation.json` (combined)
Status: **FINAL — decision recorded; no materialization without separate CP-018 approval.**

## Verdict (by candidate)

| Factor | Type | Horizons | Evidence | Verdict |
|---|---|---|---|---|
| **mvrv** (`mvrv_ratio`, daily) | BTC time-series | 7d / 30d | IC **−0.52 / −0.68**, CI excludes 0, FDR-significant, **OOS-consistent** (−0.34 / −0.85) | **PROMOTION CANDIDATE** — the strongest validated factor in the suite |
| **d_pct_cvar** | DMV cross-sectional | 30d | IC −0.054, CI [−0.059, −0.048], FDR-sig | **PROMOTION CANDIDATE** — validated **risk intelligence, not tradable alpha** (median negative / Sharpe 0.26 in long-short sim) |
| realized_cap_usd | BTC time-series | 7d | IC −0.26 but **OOS sign flips** (+0.20) | **REJECTED (OOS not robust)** |
| cdd, sopr, news | BTC time-series | 1/7/30d | not material / not FDR-sig | NEEDS_MORE_VALIDATION |
| durability/momentum/valuation scores, ath/atl, coin age | DMV cross-sectional | 1/7/30d | not material | NEEDS_MORE_VALIDATION |

Summary: **PROMOTION_CANDIDATE = 3** (mvrv×2, d_pct_cvar×1) · NEEDS_MORE_VALIDATION = 42 · REJECTED = 1.

## Recommended action

1. **mvrv** — promote as a **BTC market-timing / risk factor** (high MVRV ⇒ lower forward
   returns). Strong, daily, OOS-consistent. Recommended use: regime/timing overlay, not
   a stand-alone long/short (IC magnitude is strong but single-asset).
2. **d_pct_cvar** — promote as a **cross-sectional risk factor** (tail-risk dimension).
   Not tradable alpha alone; use as an input to multi-factor risk models.
3. **Do not promote** the remaining factors on current evidence.

## Caveats / conditions before production materialization (CP-018)

- mvrv daily sample = 145 points (2026-01-20 → 2026-08-13) — strong but one regime span;
  re-validate quarterly as the daily series accumulates.
- Any promotion into a live signal/API (CP-018) requires a separate explicit approval:
  signal spec, target table/serving path, refresh cadence, and a monitoring/rollback plan.
- This document and the JSON are **recommendation-only**; no canonical/RDS/cache changes were made.
