-- Phase 3: Dow Theory price-action patterns.
-- Idempotent.

CREATE TABLE IF NOT EXISTS "FE_DOW_PATTERNS" (
    id bigint,
    slug text NOT NULL,
    "timestamp" timestamptz NOT NULL,
    m_dow_support             double precision,
    m_dow_resistance          double precision,
    m_dow_dbl_top_bin         bigint,
    m_dow_dbl_bot_bin         bigint,
    m_dow_trpl_top_bin        bigint,
    m_dow_trpl_bot_bin        bigint,
    m_dow_range_break_up_bin  bigint,
    m_dow_range_break_dn_bin  bigint,
    m_dow_flag_bin            bigint,
    PRIMARY KEY (slug, "timestamp")
);

ALTER TABLE "FE_DMV_ALL"
    ADD COLUMN IF NOT EXISTS m_dow_dbl_top_bin         bigint,
    ADD COLUMN IF NOT EXISTS m_dow_dbl_bot_bin         bigint,
    ADD COLUMN IF NOT EXISTS m_dow_trpl_top_bin        bigint,
    ADD COLUMN IF NOT EXISTS m_dow_trpl_bot_bin        bigint,
    ADD COLUMN IF NOT EXISTS m_dow_range_break_up_bin  bigint,
    ADD COLUMN IF NOT EXISTS m_dow_range_break_dn_bin  bigint,
    ADD COLUMN IF NOT EXISTS m_dow_flag_bin            bigint;
