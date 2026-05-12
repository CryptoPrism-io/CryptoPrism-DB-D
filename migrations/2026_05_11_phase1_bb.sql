-- Phase 1: Bollinger Bands columns
-- Idempotent. Safe to re-run.
-- Run BEFORE deploying the updated gcp_dmv_tvv.py.
-- Targets: dbcp (primary) and cp_backtest (historical).

ALTER TABLE "FE_TVV"
    ADD COLUMN IF NOT EXISTS "BB_Mid"   double precision,
    ADD COLUMN IF NOT EXISTS "BB_Upper" double precision,
    ADD COLUMN IF NOT EXISTS "BB_Lower" double precision,
    ADD COLUMN IF NOT EXISTS "BB_Width" double precision,
    ADD COLUMN IF NOT EXISTS "BB_Pct_B" double precision;

ALTER TABLE "FE_TVV_SIGNALS"
    ADD COLUMN IF NOT EXISTS m_tvv_bb_bin double precision;
