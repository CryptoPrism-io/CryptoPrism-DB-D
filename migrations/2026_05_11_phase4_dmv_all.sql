-- Phase 4: extend FE_DMV_ALL with Phase 1 bin columns so gcp_dmv_core.py
-- can to_sql('FE_DMV_ALL', if_exists='append') without column-mismatch errors.
-- Idempotent. Safe to re-run.
-- Run BEFORE the next daily DMV pipeline execution.

ALTER TABLE "FE_DMV_ALL"
    ADD COLUMN IF NOT EXISTS m_tvv_bb_bin         double precision,
    ADD COLUMN IF NOT EXISTS m_osc_supertrend_bin bigint,
    ADD COLUMN IF NOT EXISTS m_osc_aroon_bin      bigint;
