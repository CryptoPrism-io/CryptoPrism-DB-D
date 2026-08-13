# CP-018 — DB / runners / pipeline analysis & API endpoint candidates

Date: 2026-08-13 · Read-only audit of AWS + RDS.

## 1. Is the onchain pipeline live?

**YES — daily one-off Fargate tasks via EventBridge Scheduler** (the ECS *service* is
paused at `desiredCount=0` for cost, but the schedulers still run daily tasks):

| Scheduler | Schedule | Target | Feeds |
|---|---|---|---|
| `cryptoprism-onchain-daily` | 06:00 UTC | run-task `cryptoprism-onchain:3` | onchain_* metrics (btc+eth daily_full) |
| `cryptoprism-onchain-cp006-daily` | 06:30 UTC | run-task `cryptoprism-onchain:3` | cp006 BTC CDD/SOPR |
| `cryptoprism-onchain-materialize-daily` | 07:30 UTC | run-task `cryptoprism-onchain:3` | FE_* current tables (top-1000) |

- `onchain_pipeline_runs` shows btc+eth `daily_full` running **every day at 06:00**,
  `status=success`, ~15-20s, ~$0.005-0.012/day each.
- **⚠️ btc daily_full is failing 3 metrics**: `cdd`, `mvrv`, `supply_distribution`
  — all with the same Athena error: *"backquoted identifiers are not supported;
  use double quotes"*. This is a **BigQuery→Athena SQL migration bug** (backticks).
  eth is clean. This explains the sparse/stale `cdd` (2026-01+), `sopr`, and
  monthly `mvrv`/`realized_cap`.
- Schedulers run the **`latest`** image tag; newest task def `cryptoprism-onchain:4`
  pins digest `641f802c…` — drift risk.

## 2. Database map

### cp_backtest (research/backtest) — fresh to 2026-08-12
- `1K_coins_ohlcv` 2.6M · full `FE_*` history ~1.19M each (all 8 families)
- **NEW** `pit_dmv_cp011_v2_20260812_1315.dmv_rows` **2,593,976** (PIT DMV + scores + VaR/CVaR + metrics)
- **NEW** `cp018_serving.dmv_latest_top100` (100 rows, latest top-100 snapshot)

### dbcp (production/app) — mixed freshness
- Live `1K_coins_ohlcv` 2.6M + `crypto_listings_latest_1000` (current top-1000)
- FE_* current tables (1,000 rows) — refreshed daily 07:30
- Onchain (06:00 daily): `onchain_daily_metrics` 74k, `onchain_whale_txs` 7.7k,
  `onchain_exchange_flow`, `onchain_scores` — **fresh daily**; `onchain_utxo_metrics`
  (mvrv/realized_cap) **monthly**; `utxo_advanced_metrics`/`supply_distribution`
  **stale 2026-08-07**; `defi_metrics`/`derivatives_metrics`/`liquidation_heatmap`
  **stale 2026-06-12**
- ML: `ML_SIGNALS_V2` 109k, `ML_LABELS` 1.0M, `ML_MODEL_REGISTRY` (fresh to 08-12)
- News: `FE_NEWS_SIGNALS` 44k, `cc_news` 389k, `FE_NEWS_SENTIMENT` 245k (fresh 08-13)
- App: users, saarthi, economic_calendar, global latest

## 3. Cache / API hosting
- **No Redis/ElastiCache** provisioned in AWS.
- cryptoprism-api is **not** on this ECS cluster (only `cryptoprism-onchain`);
  assumed Cloud Run — needs `gcloud` re-auth to confirm.

## 4. API endpoint candidates (grouped by readiness)

### A. DMV / serving layer (NEW — ready now)
- `GET /api/v1/dmv/latest` → top-N (default 100) latest scores + VaR/CVaR + confidence (from `cp018_serving`)
- `GET /api/v1/dmv/{slug}` → single-asset latest DMV
- `GET /api/v1/dmv/history/{slug}?from&to` → historical PIT scores/var/metrics (shadow)
- `GET /api/v1/dmv/top?metric=momentum|valuation|durability` → ranked screener

### B. Live signals / market (dbcp FE_*, daily)
- `GET /api/v1/signals/{slug}` → current signal bins (momentum/osc/tvv/ratios)
- `GET /api/v1/market/screener?top=100` → ranked by aggregate score across top-1000

### C. Onchain metrics (dbcp — blocked on btc bug)
- `GET /api/v1/onchain/{metric}?chain=btc&from&to` (mvrv, realized_cap, cdd, sopr, whale_txs, exchange_flow)
- `GET /api/v1/onchain/freshness` → `v_metric_freshness`
- **Blocked**: btc cdd/mvrv/supply_distribution (Athena backtick bug)

### D. ML / alpha (dbcp)
- `GET /api/v1/ml/signals/{slug}` · `GET /api/v1/ml/labels` · `GET /api/v1/ml/models`

### E. News / sentiment (dbcp)
- `GET /api/v1/news/recent` · `GET /api/v1/news/sentiment/{slug}`

### F. App (dbcp)
- global latest, economic calendar, saarthi, fear&greed

## 5. Recommendations
1. **Fix the Athena backtick-SQL bug** (cdd/mvrv/supply_distribution) — restores daily
   BTC onchain data; would unblock `mvrv` validation + `cdd`/`sopr` factor research.
2. **Point the 3 schedulers at the pinned image** (task def `cryptoprism-onchain:4`)
   to stop `latest` drift.
3. **Provision Redis** (ElastiCache) if the serving API needs a cache layer.
4. **Confirm cryptoprism-api hosting** (gcloud re-auth) before wiring routes.
5. **Build order**: dmv/latest (+ dmv/{slug}, history) → signals screener → onchain
   (after bug fix) → ML/news.
