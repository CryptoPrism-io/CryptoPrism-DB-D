# CP-011 Phase D — Stage 1 Gate Report (EQUIVALENCE FAILED — hard stop)

**Status:** STAGE 1 **FAILED** at 1.2 (signal equivalence) → hard stop. No shadow/canonical
writes. Stage 2 not authorized.
**Branch:** `feat/cp011-phase-d-hybrid-rebuild` (local) · main `c5cbfb8` · CP-009 frozen `e5d76a3`

## 1.1 — Methodology frozen (`pit/regenerate.py` + `pit/stage1_gate.py::methodology_freeze`)

Core-4 regeneration from raw OHLCV via the repo TA functions (`cp011_pit_v1`):
- momentum: RSI[9,18,27,54,108], SMA14, ROC9, Williams%R14, SMI14/3/3, CMO14, MOM10, TSI13/25.
- oscillators: MACD12/26/9, CCI20, ADX14, UO7/14/28, AO, TRIX15, Supertrend10/3, Aroon25.
- tvv: OBV, SMA/EMA{9,18,21,108}, ATR21, channels, Bollinger20/2, VWAP, CMF21.
- ratios: 28-day trailing window, min 5 days, bitcoin benchmark.
Only declared `SIGNAL_FAMILIES` bins are selected for scoring; extra bins (supertrend/aroon/
bollinger) computed but not scored. Parameters come from the current code, **not invented**.

## 1.2 — Signal equivalence vs existing FE_*_SIGNALS (stratified 9-slug sample)

Sample: aave, bitcoin, cardano, chainlink, dogecoin, ethereum, litecoin, uniswap,
yearn-finance (long+medium history, all covered). Full-history OHLCV.

| Family | exact | both-null | **mismatch** | regen-val/exist-null | regen-null/exist-val |
|---|---|---|---|---|---|
| oscillators | 185,465 | — | **775** | 1,674 | 0 |
| momentum | 155,135 | — | **65** | 1,395 | 0 |
| tvv | 183,802 | — | **1,672** | 1,674 | 0 |
| ratios | 288,917 | — | **108** | 1,936 | 0 |
| **total** | **813,319** | 54,342 | **2,620 (0.30%)** | 6,679 | 0 |

- `regen_null_exist_val = 0` everywhere → regeneration never misses an existing value.
- `regen_val_exist_null` (6,679) → current generators fill values where the old tables have
  NULL (warm-up/`default=0` convention) — this is an **explained** difference.
- **`mismatch` (2,620) is NOT warm-up/NULL — it is genuine value disagreement**, systematic
  across ALL sample slugs (237–378 each), concentrated in:
  - `m_osc_adx_bin` **726**, `m_tvv_cmf` **838**, `m_tvv_obv_1d_binary` **711**, plus small
    counts in momentum/ratios/other bins.

**Root cause (CORRECTED 2026-08-09):** the earlier claim that `70ea2b9` changed ADX/CMF/OBV is **disproven**. `70ea2b9` ("v4.8.0 expansion") only **added** Bollinger (tvv), Supertrend and Aroon (osc) — `git diff 4223a87..HEAD` confirms `calculate_adx`/`calculate_cmf`/`calculate_obv` were unchanged by it or any later commit. The genuine value mismatches instead reflect a **mixed build** of the existing tables (see `docs/CP011-phaseD-stage1_5-diagnosis.md`): per-asset series-start ADX initialization drift, plus recent live-pipeline 110-day-window appends (CMF/OBV), none of which is a single reproducible historical methodology.

**Verdict: equivalence FAILS** — the mismatches are material and not explainable as
warm-up/NULL. Per the mission's hard stop ("material unexplained differences exist"), Stage 1
stops here.

## 1.3 — Coverage partition (from DB, read-only)

| Partition | rows | notes |
|---|---|---|
| Universe (distinct slug,date in `1K_coins_ohlcv`) | **2,590,975** | target |
| Covered (core-4 presence intersection of existing tables) | 1,177,746 | ~45.5% |
| Regeneration-required | 1,413,229 | universe minus covered; includes delisted slugs |
| Core-4 complete (existing) | 1,173,651 | all required bins non-null |
| Incomplete (existing core-4) | 4,095 | reason: missing required bin(s) |
| Delisted/dropped slugs retained in universe | 1,257 | in OHLCV, no existing signal rows |

## 1.4 — Benchmark: NOT RUN (gate already failed at 1.2)

Measured regeneration cost (earlier benchmark): ratios 283 s per 6-slug full-history batch
→ **~47 h projected for the full 3,585-slug universe** on this machine (plus ~1 h PIT layer).
Even if equivalence passed, the ratios regeneration cost is a further feasibility concern for
the delisted-only partition (~1.75–2.5 h) vs full regeneration (~47 h).

## Stop

Stage 1 did not pass → **no Stage 2**. No shadow schema created; canonical tables + Phase C
dry-run schema untouched; nothing pushed/PR'd/merged. Evidence:
`report/cp011-phase-d/stage1_equivalence.json`; this report.

## Recommended decision (requires approval)

To make equivalence pass, pick one:
- **A:** Identify/pin the exact historical TA code version that built the existing
  `FE_*_SIGNALS` (pre-v4.8.0 ADX/CMF/OBV), regenerate with THAT, and verify equivalence.
- **B:** Declare current-main regeneration as the canonical signal definition, document the
  existing tables as legacy (their ADX/CMF/OBV are deprecated), and rebuild signals for the
  covered rows too — a methodology change requiring approval.
- **C:** Use existing `FE_*_SIGNALS` verbatim for covered rows (legacy definitions) and only
  regenerate the uncovered/delisted partition — accepts a two-definition signal layer.
