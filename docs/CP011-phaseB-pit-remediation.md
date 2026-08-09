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
| `tests/test_pit.py` | 13 tests (see §2). |

Methodology versions: `pit-dmv-v1` · `pit-trailing-365d-min252` · `pit-cumulative-ath-atl-v1` · `pit-approx-v1`.

## 2. Signal policy + coverage decision (measured, not assumed)

`policy.py` declares CORE = oscillators, momentum, tvv, ratios; OPTIONAL = metrics. Read-only
distinct `(slug, date)` intersections from cp_backtest:

| Rule | Distinct (slug,date) | vs ~1.19M universe |
|---|---|---|
| All-8 intersection | **94,714** | −92% (metrics bottleneck) |
| **Core-4 intersection (chosen)** | **1,177,746** | **98.8%** |
| Core-4 + metrics | **95,713** | −92% |

`FE_METRICS_SIGNAL` has only ~113 historical dates, so all-8 and core+metrics collapse to
~95k. **The least destructive defensible rule is the 4-core intersection** — recorded in
`policy.COVERAGE_MEASURED` and used by both live and backfill.

## 3. Tests — 16/16 pass (ruff + mypy clean)

- `test_var_cvar_pit` — property: d_pct_var == trailing-window 5th percentile over rows ≤ t (linear interpolation).
- `test_var_cvar_pit_null_before_min_history` — NULL before 252 obs.
- `test_universe_pit_includes_delisted_on_active_dates` — delisted asset active on its dates, absent after; every (slug,date) satisfies universe membership.
- `test_score_range_within_bounds` / `test_score_range_rejects_non_bin_columns` / `test_incomplete_rows_are_nan_not_zero`.
- `test_neutral_fill_never_applied` — missing VaR stays NaN; incomplete scores NaN not 0.
- `test_timestamp_alignment_incomplete_not_misaligned` — validates the DECLARED core-signal policy (not all-8); warmup-missing rows marked incomplete.
- `test_live_backfill_comparability` — one shared policy/signal set + one score entry point.
- `test_determinism_same_input_same_output`.
- `test_coin_age_pit_row_date_minus_first_seen` — coin age = row date − first valid date (no now()).
- `test_duplicate_dates_deterministic_and_in_window` — duplicate timestamps all in same window; deterministic.
- **`test_future_row_mutation_does_not_change_output_at_d`** — mutating future returns/bins leaves the row at date d byte-identical (PIT layer); AND the legacy full-sample VaR **does** change under the same mutation (proves the test catches the old leak).

## 4. Deterministic sample (read-only cp_backtest; `report/pit_sample_results.json`)

- **Assets (6):** bitcoin, ethereum, solana, litecoin, dogecoin + **vgx-token** (delisted; OHLCV ends 2025-06-15).
- **Window:** 2023-01-01..2026-08-08 · **7,474 OHLCV rows · 1,315 dates · ~39 s runtime.**

| Check | Result |
|---|---|
| PIT universe | `PIT_APPROX`; `vgx_token_active_2024-06-01 = true`; active_by_year 6,6,6,5 (drops to 5 in 2026 after delist) — **delisted asset included on active dates** |
| VaR old-vs-corrected @2024-09-14 | PIT **-0.0418** vs old full-sample **-0.0359** — differ (corrected uses trailing 365d) |
| Metrics old-vs-corrected @2024-09-14 | PIT days-since-ATH **184** (row-date; ATH date 2024-03-14) vs old now-based **306**; PIT days-since-ATL 622 vs old 1315; PIT coin-age **622 d** (row date − first-seen) — all PIT-safe, no `now()` |
| Score range | D -66.7..100 · M -81.25..81.25 · V -100..100 — **all within [-100,100]** |
| Completeness | corrected marks **2,337 incomplete (31.2%)** as NaN; old silently `fillna(0)` (no incompleteness recorded) |
| Core-signal coverage | 4/4 families: **5,144 rows (68.8%)** · 3/4: 1,286 · 1/4: 36 · incomplete marked, not zero-filled |

## 5. Count reconciliation + full shadow rebuild estimate (read-only cp_backtest)

**Exact reconciliation (all measured, read-only):**

| Step | Distinct (slug,date) rows | Note |
|---|---|---|
| PIT_APPROX universe (`1K_coins_ohlcv`) | **2,590,975** | eligibility universe incl. 1,257 delisted slugs |
| Core-4 presence intersection (in all 4 core tables) | **1,177,746** | 45.5% of universe (survivorship-filtered old tables + early sparsity) |
| Incomplete core rows | **4,095 (0.35%)** | present but ≥1 required bin NULL (warmup) |
| **Core-4 complete (valid scored)** | **1,173,651** | all required bins non-null in all 4 families |
| Old canonical `FE_DMV_ALL` rows | 99,203 | bitcoin-only pre-2026 (metrics bottleneck) |

**Shadow rebuild estimate (corrected):**
| Item | Value |
|---|---|
| Estimated scored shadow rows | **≈ 1,173,651 (measured core-4 complete)**; expected range **[1.17M .. 2.59M]** (PIT rebuild may add delisted slugs' rows) |
| Universe upper bound | 2,590,975 |
| Correction vs earlier ~817k | The 817k was an underestimate: it multiplied `momentum_rows` (1,187,473) by the **sample** all-4-core rate (0.688), which is inflated by the delisted asset (no rows in the old listings-joined signal tables → 31.2% sample incompleteness). The full-history core-4 intersection is 99.65% complete. Correct base = core-4 complete = **1,173,651**. |
| Runtime | PIT layer ≈ 10–15 min for ~2.6M rows; full rebuild incl. signal regeneration ≈ 1–3 h (ratios 28d trailing loop dominates) |
| Athena scan | **0 GB** — rebuild reads RDS OHLCV only; canonical Athena/UTXO tables untouched |
| RDS write volume | ≈ 1.2–3.5M rows across shadow `FE_*_SIGNALS` + `FE_DMV_ALL/SCORES` (~0.5–1 GB), to a **shadow schema** |
| AWS cost | **≈ $0–0.05** (local/EC2 over existing RDS; no Athena; no external data; no new infra) |
| Rollback | DROP the versioned shadow schema (`pit_dmv_<version>.*`); canonical `FE_*` history never overwritten → fully reversible |

**Optional metrics cannot remove core rows:** core scoring is independent of metrics;
metrics is never required for a core row to be scored. Requiring metrics would collapse
the intersection to 95,713 (−92%, measured), so it stays optional and core rows are
unaffected by its presence/absence.

**Shadow-target guard:** `pit/targets.py` `assert_shadow_schema()` rejects canonical names
(`public`, `FE_DMV_ALL`, `FE_DMV_SCORES`, `dbcp`, `cp_backtest`, …) and any schema not
prefixed `pit_dmv_`; `shadow_schema(version)` yields a unique versioned name. No config
default resolves to canonical `FE_*` (the pit layer writes nothing; the sample is read-only).

**PIT_APPROX exact label (`policy.UNIVERSE_META`):** `universe_method=PIT_APPROX`,
"OHLCV-observed eligibility universe" — source `1K_coins_ohlcv` (2013-04-28..2026-08-08),
interval rule first_seen ≤ d ≤ last_seen, sparse-gap limitation documented, upgrade path
`CMC_SNAPSHOT`, **no historical rank or market-cap eligibility claim**.

## 6. Methodology breaks fixed vs Phase A

1. Full-sample VaR/CVaR → trailing 365d / min 252 (NULL before).
2. Full-series ATH/ATL `idxmax/idxmin` + `now()` days-since → cumulative ATH/ATL + row-date.
3. `crypto_listings_latest_1000` (current) join → PIT_APPROX universe (delisted kept).
4. Prefix-sweep score sum (could exceed 100) → explicit approved bin columns + `[-100,100]` validation.
5. Neutral-fill `fillna(0)` → mark-incomplete policy (no zero-fill).
6. Live(8-table)/backfill(5-table) divergence → one central signal policy; metrics deferred to optional (113-date history gap measured, not assumed).

## 7. Blocked items / decisions still needed (unchanged from Phase A)

- PIT universe source: no dated CMC snapshots exist → **PIT_APPROX** in use; upgrade to `CMC_SNAPSHOT` if/when snapshots are ingested.
- Historical metrics reconstruction (per-date ATH/ATL is now available in the layer; the FE_METRICS history gap requires a regenerate-from-OHLCV pass).
- Backfill start date (early years ~5–30 surviving slugs) and whether the 4-core set is sufficient.

## 8. Commit + PR

- Local commit: `ce347cc`, then PR-gate commit `63f29af` on `feat/cp011-dmv-pit-recon` (base `927be95`).
- PR: **https://github.com/CryptoPrism-io/CryptoPrism-DB-D/pull/44** (repo renamed from `CryptoPrism-DB` → `CryptoPrism-DB-D`; base `main`, head SHA `63f29af`).
- Checks: **GitGuardian pass** · **claude-review fail** (repo's `anthropics/claude-code-action` produced no review/comment — infra/token issue, not a code finding; documented separately).
- Pre-existing repo test limitations (documented separately): `test_backtest_mom_data.py` cannot run without prod DB creds (SystemExit at import); `test_phase1_supertrend.py` collects 0 tests.
- **Not merged.** No rebuild, backtest, deploy, or production write.
