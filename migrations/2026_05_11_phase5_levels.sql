-- Phase 5: Fibonacci retracement levels + ATR bands.
-- Builds on Phase 3 swing detection. Idempotent.

CREATE TABLE IF NOT EXISTS "FE_PRICE_LEVELS" (
    id bigint,
    slug text NOT NULL,
    "timestamp" timestamptz NOT NULL,
    -- Fibonacci levels anchored to the most recent swing low (0%) and high (100%)
    fib_swing_low            double precision,
    fib_swing_high           double precision,
    fib_0_236                double precision,
    fib_0_382                double precision,
    fib_0_500                double precision,
    fib_0_618                double precision,
    fib_0_786                double precision,
    -- ATR-anchored envelope (5-period MA close +/- 3 * ATR)
    atr_band_mid             double precision,
    atr_band_upper           double precision,
    atr_band_lower           double precision,
    -- Directional bins (FE_OSCILLATORS_SIGNALS bigint convention, default 0)
    m_lvl_fib_proximity_bin  bigint,
    m_lvl_atr_band_bin       bigint,
    PRIMARY KEY (slug, "timestamp")
);

ALTER TABLE "FE_DMV_ALL"
    ADD COLUMN IF NOT EXISTS m_lvl_fib_proximity_bin   bigint,
    ADD COLUMN IF NOT EXISTS m_lvl_atr_band_bin        bigint;
