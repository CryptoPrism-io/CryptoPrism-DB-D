-- Phase 1: Supertrend and Aroon columns
-- Idempotent. Safe to re-run.
-- Run BEFORE deploying the updated gcp_dmv_osc.py.
-- Targets: dbcp (primary) and cp_backtest (historical).

ALTER TABLE "FE_OSCILLATOR"
    ADD COLUMN IF NOT EXISTS "Supertrend_Line" double precision,
    ADD COLUMN IF NOT EXISTS "Supertrend_Dir"  double precision,
    ADD COLUMN IF NOT EXISTS "Aroon_Up"        double precision,
    ADD COLUMN IF NOT EXISTS "Aroon_Down"      double precision,
    ADD COLUMN IF NOT EXISTS "Aroon_Osc"       double precision;

ALTER TABLE "FE_OSCILLATORS_SIGNALS"
    ADD COLUMN IF NOT EXISTS m_osc_supertrend_bin bigint,
    ADD COLUMN IF NOT EXISTS m_osc_aroon_bin      bigint;
