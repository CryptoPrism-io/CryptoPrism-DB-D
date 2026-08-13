# CP-011 — Sibling implementation reconciliation

Date: 2026-08-13 · Branch: `feat/cp011-phase-d-hybrid-rebuild` · Commit: `758ac199`

## Context

Two separate implementations exist for CP-011. This document records the
reconciliation (prerequisite gate before any CP-012/CP-020 work). Neither
implementation was run/imported/migrated/combined into the other during this
reconciliation.

## 1. The two implementations

| | **A — Validated (authoritative)** | **B — Sibling** |
|---|---|---|
| Repo / branch | `CryptoPrism-DB-cp011c` / `feat/cp011-phase-d-hybrid-rebuild` | `cryptoprism-onchain-cp011` / `feat/cp011-pit-shadow-backtest` |
| Commit | `758ac199` (full run + fixes) | `0f8bf33` ("local safety commit, NOT for merge") + `bbe43a5`,`92f4fea` (CP-012 design docs) |
| Status | **COMPLETE** — Stage 2B on AWS Fargate; Stage 3 validation **PASS** | Committed but **never executed** |
| Target database | **cp_backtest** | **dbcp** |
| Source tables | `1K_coins_ohlcv` (raw OHLCV regeneration) | `onchain_utxo_metrics`, `onchain_daily_metrics`, `FE_NEWS_SIGNALS`, `1K_coins_ohlcv` (BTC price) |
| Output schema | `pit_dmv_cp011_v2_20260812_1315` | `cp011_shadow` |
| Output tables | `dmv_rows` (2,593,976), `shadow_run_meta`, `chunk_progress` | `research_frame` (0 rows), `frozen_manifest` (0), `partition_log` (0), `reason_codes` (3) |
| Purpose | PIT-safe DMV (Core-4 signal bins + VaR/CVaR + metrics + scores) | BTC factor-research frame (mvrv / realized_cap / cdd / sopr + news sentiment + forward returns) |

## 2. Findings

- **Databases are NOT interchangeable.** A (`cp_backtest`) and B (`dbcp`) are
  different databases on the same RDS endpoint; source tables differ; outputs
  differ. Confirmed read-only: `dbcp` has the `cp011_shadow` schema but
  **`research_frame` = 0 rows / `frozen_manifest` = 0** — the sibling rebuild was
  **never run** (only migration `008`'s `reason_codes` INSERT landed).
- **No live data conflict.** A wrote 2,593,976 rows to `pit_dmv_cp011_v2_*` in
  `cp_backtest`; B left `cp011_shadow` empty in `dbcp`.
- **B's own CP-012 design already points at A's output.** Commit `bbe43a5`
  ("CP-012 cross-sectional adapter design — consume `pit_dmv_*` `dmv_rows`") and
  `92f4fea` ("lock CP-012 forward-return source = frozen snapshot") establish that
  CP-012 consumes the **validated `pit_dmv_*` shadow** — i.e. the sibling's
  research-frame approach is superseded for CP-012.
- **Naming collision.** Both use the string `cp011_pit_v2_current_main_full_regen`
  (A = `methodology_version`; B = default `run_id`). No functional conflict while
  B is unrun; disambiguate B's run-id if it is ever executed.

## 3. Reconciliation decision

1. **Authoritative for CP-011/CP-012:** Implementation A — `pit_dmv_cp011_v2_20260812_1315`
   in `cp_backtest` — is the CP-011 deliverable and the CP-012 forward-return source.
2. **Sibling B is parked.** Do not merge (its own commit message). If the BTC
   factor-research frame (mvrv/cdd/sopr/news IC backtests → CP-020) is explicitly
   requested later, run it against `dbcp` → `cp011_shadow` under a **disambiguated
   run-id** (e.g., `cp011_factor_research_v2`), never against `cp_backtest`, never
   pointed at `pit_dmv_*` tables, and never combined with implementation A.
3. **No changes made to B** during reconciliation (not run/imported/migrated/combined).
4. **Empty `cp011_shadow` shell in `dbcp`** left untouched; drop it only with the
   sibling repo owner's approval.

## 4. Gate status

- CP-011 Stage 3: **PASS** (`stage3_validation.json` all checks true).
- Sibling reconciliation: **COMPLETE** (`sibling_reconciliation.json`).
- **CP-012 / CP-020 may now proceed** with the frozen snapshot / `pit_dmv_*` as source.
