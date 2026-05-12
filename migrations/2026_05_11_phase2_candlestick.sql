-- Phase 2: Candlestick signal table + FE_DMV_ALL extension.
-- Idempotent. Safe to re-run on dbcp and cp_backtest.

CREATE TABLE IF NOT EXISTS "FE_CANDLESTICK_SIGNALS" (
    id bigint,
    slug text NOT NULL,
    "timestamp" timestamptz NOT NULL,
    m_cnd_marubozu_bin       bigint,
    m_cnd_hammer_bin         bigint,
    m_cnd_hanging_man_bin    bigint,
    m_cnd_shooting_star_bin  bigint,
    m_cnd_engulfing_bin      bigint,
    m_cnd_harami_bin         bigint,
    m_cnd_piercing_bin       bigint,
    m_cnd_dark_cloud_bin     bigint,
    m_cnd_star_bin           bigint,
    PRIMARY KEY (slug, "timestamp")
);

ALTER TABLE "FE_DMV_ALL"
    ADD COLUMN IF NOT EXISTS m_cnd_marubozu_bin       bigint,
    ADD COLUMN IF NOT EXISTS m_cnd_hammer_bin         bigint,
    ADD COLUMN IF NOT EXISTS m_cnd_hanging_man_bin    bigint,
    ADD COLUMN IF NOT EXISTS m_cnd_shooting_star_bin  bigint,
    ADD COLUMN IF NOT EXISTS m_cnd_engulfing_bin      bigint,
    ADD COLUMN IF NOT EXISTS m_cnd_harami_bin         bigint,
    ADD COLUMN IF NOT EXISTS m_cnd_piercing_bin       bigint,
    ADD COLUMN IF NOT EXISTS m_cnd_dark_cloud_bin     bigint,
    ADD COLUMN IF NOT EXISTS m_cnd_star_bin           bigint;
