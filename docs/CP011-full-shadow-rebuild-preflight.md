# CP-011 Full Shadow Rebuild — Preflight + Stop-Condition Report

**Status:** FULL EXECUTION **STOPPED at preflight** (stop conditions triggered) — no full
rebuild run, no production writes, no shadow schema created.
**Branch:** `feat/cp011-shadow-dryrun` (local) · **main:** `c5cbfb8` (Phase C merged)
**CP-009:** frozen at `e5d76a3` · Phase C dry-run schema preserved.

## 1. Preflight evidence (Step 1)

- main at `c5cbfb8` ✓ · worktree clean ✓ · PIT tests **16/16** ✓ · ruff ✓ · mypy ✓ (11 files).
- Guard: `assert_shadow_schema` accepted `pit_dmv_cp011_v1_*` and rejects canonical
  `public`/`dbcp`/`cp_backtest`/`FE_*`/empty (tested). Preflight dry-run with full universe
  completed with **no writes**:
  - schema `pit_dmv_cp011_v1_20260809t123755z` (dry-run only, not created)
  - `--all-slugs` = **3,585** assets · `2013-04-28..2026-08-08` · source rows **2,600,882**
  - `methodology_version=cp011_pit_v1` · `universe_method=PIT_APPROX`

**Canonical BEFORE (counts + deterministic md5 fingerprint):**
| Table | rows | slugs | fingerprint (md5 of count|slugs|min|max) |
|---|---|---|---|---|
| FE_DMV_ALL | 99,203 | 1,305 | 06e4a2e4… |
| FE_DMV_SCORES | 99,203 | 1,305 | 06e4a2e4… |
| FE_PCT_CHANGE | 1,187,741 | 1,305 | a916da6e… |
| FE_MOMENTUM_SIGNALS | 1,190,026 | 1,315 | 52e2ce11… |
| FE_OSCILLATORS_SIGNALS | 1,191,120 | 1,316 | a361a482… |
| FE_TVV_SIGNALS | 1,190,136 | 1,316 | adeac906… |
| FE_RATIOS_SIGNALS | 1,197,519 | 1,318 | 691aab31… |
| Universe (distinct slug,date in `1K_coins_ohlcv`) | **2,590,975** | — | — |

## 2. Measured feasibility (the reason for the stop)

Full Core-4 signal regeneration from raw OHLCV was prototyped (`pit/regenerate.py`,
reusing the repo's TA functions) and measured on real full-history data:

| Family | 6-slug time | Notes |
|---|---|---|
| momentum | 0.2 s | rolling indicators |
| oscillators | 1.4 s | requires Supertrend/Aroon (added v4.8.0) |
| tvv | 0.2 s | requires Bollinger Bands (added v4.8.0) |
| **ratios (28d trailing)** | **283 s** | dominant — ~10 ms per (date,slug) window |
| Core-4 total | ~296 s | |

**Extrapolation:** ratios for the full 3,585-slug universe ≈ 283 s × (3,585/6) ≈ **47 h** on
this machine. Combined with ~1 h PIT var/cvar + metrics over 2.6 M rows, full raw
regeneration is **infeasible here** — this materially exceeds any reasonable preflight
estimate (stop condition 8).

**Methodology blocker:** the current TA code on `main` is internally inconsistent — its
`generate_binary_signals_*` generators require Supertrend/Aroon/Bollinger indicators that
the repo's own `backfill_cp_backtest.py` does not compute (its oscillator/tvv phases fail
with `KeyError: Supertrend_Dir` / `KeyError: BB_Lower`). Regenerating the declared Core-4
bins therefore requires implementation fixes and a methodology decision on the signal
source (stop condition 7).

## 3. Funnel (measured from existing tables; full-universe target)

| Step | Count |
|---|---|
| OHLCV-observed universe (distinct slug,date) | **2,590,975** |
| Core-4 presence intersection (existing tables) | 1,177,746 |
| Core-4 complete (all required bins non-null) | 1,173,651 |
| Incomplete core rows | 4,095 (0.35%) |
| Old canonical `FE_DMV_ALL` rows | 99,203 |

The universe (2,590,975) exceeds the existing-table core-4 intersection (1,177,746) by
1,413,229 rows — almost entirely delisted/dropped slugs (1,257 assets) that the old
current-universe signal tables excluded, plus early-year sparsity and warmup. A full
rebuild that retains those delisted rows (marked incomplete where core signals are not
regenerated) would write **≈ 2.59 M rows**.

## 4. Recommended path (requires approval — methodology decision)

Use the **existing `FE_*_SIGNALS` tables** (which ARE raw-OHLCV-regenerated signals matching
the declared Core-4 bins, produced by the backfill) as the signal source for the rows they
cover, and **regenerate signals from raw OHLCV for the delisted/uncovered slugs only**
(bounded: ~2–3 h for their ratios + fast momentum/osc/tvv). PIT var/cvar + metrics are
computed from raw OHLCV for the full universe (~1 h). This yields a full-universe shadow
schema with delisted assets retained, without the ~47 h full-regeneration cost. This is a
methodology choice (use-existing-tables vs pure-regeneration) that the run was not approved
to make silently.

## 5. Stop conditions triggered

- Runtime/cost materially exceeds the preflight estimate (ratios ≈ 47 h full regeneration).
- A methodology change is required (signal source decision; TA-code inconsistency).

No shadow schema was created; canonical tables untouched; Phase C dry-run schema untouched;
nothing pushed/PR'd/merged.
