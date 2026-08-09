# CP-011 Phase C — Bounded Shadow-Rebuild Dry Run

**Status:** BOUNDED DRY RUN COMPLETE (2026-08-09) — full rebuild NOT run; no backtest;
no production writes; not pushed/PR'd.
**Branch:** `feat/cp011-shadow-dryrun` (based on merged `main` `21e837d` = PR #44 squash)
**CP-009:** frozen at `e5d76a3`. **Canonical tables:** UNCHANGED.

## Runner (`gcp_postgres_sandbox/pit/run_shadow.py`)

Reusable, guarded PIT-safe shadow runner:
- `--shadow-schema` **required** (no default); `assert_shadow_schema()` rejects `public`,
  `dbcp`, `cp_backtest`, canonical `FE_*`, empty, and any non-`pit_dmv_*` name.
- `--slugs` and `--start`/`--end` **required** (no full universe).
- Source tables (`1K_coins_ohlcv`, `FE_*_SIGNALS`) read-only.
- Records `methodology_version`, `universe_method`, source coverage and a run ID in
  `shadow_run_meta`.
- Idempotent writes on `(slug, date, methodology_version)` (ON CONFLICT DO UPDATE).
- `--dry-run` preflight shows intended reads/writes without executing.
- NaN → SQL NULL on write (Postgres NaN would corrupt MAX()/NULL semantics).

## Bounded run

Schema (preserved for review): **`pit_dmv_cp011_dryrun_20260809t112823z`**
DB target: `cp_backtest` on the shared AWS RDS (dbcp) instance (no creds disclosed).
Sample: bitcoin, ethereum, solana, litecoin, dogecoin, **vgx-token** (delisted);
**2023-01-01..2026-08-08**.

| Count | Value |
|---|---|
| Source rows (OHLCV) | 7,474 (incl. 29 duplicate-date pairs) |
| Universe (distinct slug,date) | 7,445 |
| Core-present / valid scored | 5,120 |
| Incomplete rows | 2,325 (31.2%) |
| Written (idempotent, dedup) | 7,445 |
| Runtime | 33.1 s |

## Acceptance checks — all PASS

1. **Canonical unchanged** — FE_DMV_ALL 99,203 · FE_DMV_SCORES 99,203 · FE_PCT_CHANGE 1,187,741 · FE_MOMENTUM_SIGNALS 1,190,026 (match Phase-A baseline).
2. **Write scope** — only `pit_dmv_cp011_dryrun_*` schemas created; failed artifacts dropped; canonical untouched.
3. **No duplicates** — `(slug,date,methodology_version)` distinct == row count (0 dupes).
4. **Re-run idempotent** — re-run on same schema: 7,445 rows unchanged, 0 dupes; counts identical.
5. **Scores in [-100,100]** — D[-66.67,100] · M[-81.25,81.25] · V[-100,100].
6. **VaR/CVaR NULL before 252 obs** — bitcoin has exactly **252 NULL** var rows; first non-null 2023-09-10 (≈2023-01-01 + 252 trading days).
7. **Missing core signals → incomplete with NULL scores** — `incomplete_but_scored = 0` (all three scores NULL).
8. **Optional metrics do not remove core rows** — 4,704 valid scored rows exist on dates BEFORE metrics history (2026-03-29); min scored date 2023-01-01.
9. **Delisted asset interval** — vgx-token rows only `2023-01-01..2025-06-15` (890 rows; last observed OHLCV).
10. **Future data cannot change earlier output** — proven by `test_future_row_mutation` (16/16 pit tests); runner uses the same PIT functions.
11. **ATH/ATL PIT** — `d_met_ath_days`/`d_met_atl_days`/`d_met_coin_age_d` all ≥ 0 (row-date-based cumulative).
12. **Exact reconciliation** — universe 7,445 = incomplete 2,325 + valid scored 5,120.
13. **Telemetry recorded** — `shadow_run_meta`: run_id, pit-dmv-v1, PIT_APPROX, source_rows 7,474, universe 7,445, core_present 5,120, incomplete 2,325, valid_scored 5,120, runtime 33.1 s. RDS reads ≈ source rows (7,474); writes = 7,445; AWS cost ≈ $0 (existing RDS; no Athena; no new infra).
14. **Rollback** — DROP the uniquely named shadow schema; canonical history untouched (already exercised by cleaning failed artifacts).

## Hard prohibitions honored
No full 2.59M-row rebuild · no canonical `FE_*` writes · no return/IC/alpha/portfolio
backtest · no promotion · CP-009/CP-016/ECS/EventBridge untouched · no push/PR/merge of
this branch.

## Commit
Local commit on `feat/cp011-shadow-dryrun` (this report + `run_shadow.py` + tests).
**Not pushed; no PR.**
