# CP-011 Phase B — PIT-Safe DMV Remediation (implementation + sample)

**Status:** PHASE B IMPLEMENTED (2026-08-08) — committed locally, NOT pushed/merged
**Branch:** `feat/cp011-dmv-pit-recon` · worktree `C:\cpio_db\CryptoPrism-DB-cp011`
**Base:** `927be95` (Phase-A PIT audit) · **CP-009:** frozen at `e5d76a3` (PRs #33/#36 untouched)
**Scope guards honored:** no full rebuild, no writes to canonical FE_ tables, no backtests,
no alpha claims, no schedule/config changes, no push/PR/merge/deploy, no external-data spend.

---

## 1. What was implemented — `gcp_postgres_sandbox/pit/`

| Module | Purpose |
|---|---|
| `var_cvar.py` | `calculate_var_cvar_pit` — trailing 365-calendar-day window, min 252 obs, only rows ≤ output date, NULL before min history, never zero-filled. `var_cvar_old_fullsample` kept ONLY for comparison/mutation-proof. |
| `metrics.py` | `calculate_metrics_pit` — cumulative ATH/ATL as of each row date + row-date days-since (no `now()`, no full-series `idxmax/idxmin`). `metrics_old_fullsample` kept for comparison only. |
| `universe.py` | `PITUniverse` — per-asset `[first_seen, last_seen]` intervals; `from_ohlcv` = **PIT_APPROX**; `from_cmc_snapshots` reserved. No join to today's listings. |
| `scores.py` | Bin-only score sum, `[-100,100]` enforced + validated (raises on non-bin values), no z-scores, incomplete rows marked NaN not zero. |
| `policy.py` | Central `SIGNAL_FAMILIES`, `CORE_FAMILIES` (oscillators/momentum/tvv/ratios), `OPTIONAL_FAMILIES` (metrics), missing-data policy, methodology versions — shared by live + backfill. |
| `run_sample.py` | Read-only deterministic sample (see §3) + rebuild estimate. |
| `tests/test_pit.py` | 11 tests (see §2). |

Methodology versions: `pit-dmv-v1` · `pit-trailing-365d-min252` · `pit-cumulative-ath-atl-v1` · `pit-approx-v1`.

## 2. Tests — 11/11 pass (ruff + mypy clean)

- `test_var_cvar_pit` — property: d_pct_var == trailing-window 5th percentile over rows ≤ t.
- `test_var_cvar_pit_null_before_min_history` — NULL before 252 obs.
- `test_universe_pit_includes_delisted_on_active_dates` — delisted asset active on its dates, absent after; every (slug,date) satisfies universe membership.
- `test_score_range_within_bounds` / `test_score_range_rejects_non_bin_columns` / `test_incomplete_rows_are_nan_not_zero`.
- `test_neutral_fill_never_applied` — missing VaR stays NaN; incomplete scores NaN not 0.
- `test_timestamp_alignment_incomplete_not_misaligned` — warmup-missing rows marked incomplete, not dropped/misaligned.
- `test_live_backfill_comparability` — one shared policy/signal set + one score entry point.
- `test_determinism_same_input_same_output`.
- **`test_future_row_mutation_does_not_change_output_at_d`** — mutating future returns/bins leaves the row at date d byte-identical (PIT layer); AND the legacy full-sample VaR **does** change under the same mutation (proves the test catches the old leak).

## 3. Deterministic sample (read-only cp_backtest; `report/pit_sample_results.json`)

- **Assets (6):** bitcoin, ethereum, solana, litecoin, dogecoin + **vgx-token** (delisted; OHLCV ends 2025-06-15).
- **Window:** 2023-01-01..2026-08-08 · **7,474 OHLCV rows · 1,315 dates · ~39 s runtime.**

| Check | Result |
|---|---|
| PIT universe | `PIT_APPROX`; `vgx_token_active_2024-06-01 = true`; active_by_year 6,6,6,5 (drops to 5 in 2026 after delist) — **delisted asset included on active dates** |
| VaR old-vs-corrected @2024-09-14 | PIT **-0.0418** vs old full-sample **-0.0359** — differ (corrected uses trailing 365d) |
| Metrics old-vs-corrected @2024-09-14 | PIT days-since-ATH **184** (row-date) vs old now-based **306** — differ |
| Score range | D -66.7..100 · M -81.25..81.25 · V -100..100 — **all within [-100,100]** |
| Core-signal coverage | 4/4 families: **5,144 rows (68.8%)** · 3/4: 1,286 · 1/4: 36 · incomplete marked, not zero-filled |

## 4. Full shadow rebuild estimate (from real cp_backtest counts, read-only)

| Item | Value |
|---|---|
| PIT-universe rows (distinct slug,date in `1K_coins_ohlcv`) | **2,590,975** |
| Raw OHLCV rows | 2,600,882 |
| Momentum signal rows (history) | 1,187,473 |
| Ratios signal rows (history) | 1,194,961 |
| Sample all-4-core rate | 0.688 |
| **Estimated 4-core DMV rows** | **≈ 817,000** (momentum_rows × all4_rate) |
| Runtime | PIT layer ≈ 10–15 min for ~2.6M rows; full rebuild incl. signal regeneration ≈ 1–3 h (ratios 28d trailing loop dominates) |
| AWS cost | **≈ $0–0.05** (local/EC2 over existing RDS; no Athena; no external data; no new infra) |

## 5. Methodology breaks fixed vs Phase A

1. Full-sample VaR/CVaR → trailing 365d / min 252 (NULL before).
2. Full-series ATH/ATL `idxmax/idxmin` + `now()` days-since → cumulative ATH/ATL + row-date.
3. `crypto_listings_latest_1000` (current) join → PIT_APPROX universe (delisted kept).
4. Prefix-sweep score sum (could exceed 100) → explicit approved bin columns + `[-100,100]` validation.
5. Neutral-fill `fillna(0)` → mark-incomplete policy (no zero-fill).
6. Live(8-table)/backfill(5-table) divergence → one central signal policy; metrics deferred to optional (113-date history gap measured, not assumed).

## 6. Blocked items / decisions still needed (unchanged from Phase A)

- PIT universe source: no dated CMC snapshots exist → **PIT_APPROX** in use; upgrade to `CMC_SNAPSHOT` if/when snapshots are ingested.
- Historical metrics reconstruction (per-date ATH/ATL is now available in the layer; the FE_METRICS history gap requires a regenerate-from-OHLCV pass).
- Backfill start date (early years ~5–30 surviving slugs) and whether the 4-core set is sufficient.

## 7. Commit

See `git log` (this branch). **Not pushed; no PR; no merge.**
