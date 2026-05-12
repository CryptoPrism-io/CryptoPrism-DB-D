-- Validate the Phase 1 historical backfill on cp_backtest.
-- Compare bitcoin's 2026-05-09 row against the dbcp self-test values from earlier today.
-- Expected: BB_Mid ~78645, BB_Pct_B ~0.977, Supertrend_Dir = 1.0, Aroon_Up = 84, Aroon_Down = 4.

\echo '--- Bitcoin 2026-05-09 row in cp_backtest FE_TVV ---'
SELECT slug, "timestamp"::date AS dt, close,
       "BB_Mid", "BB_Upper", "BB_Lower", "BB_Width", "BB_Pct_B"
FROM "FE_TVV"
WHERE slug = 'bitcoin' AND "timestamp"::date = '2026-05-09';

\echo '--- Bitcoin 2026-05-09 row in cp_backtest FE_TVV_SIGNALS ---'
SELECT slug, "timestamp"::date AS dt, m_tvv_bb_bin
FROM "FE_TVV_SIGNALS"
WHERE slug = 'bitcoin' AND "timestamp"::date = '2026-05-09';

\echo '--- Bitcoin 2026-05-09 row in cp_backtest FE_OSCILLATOR ---'
SELECT slug, "timestamp"::date AS dt, close,
       "Supertrend_Line", "Supertrend_Dir", "Aroon_Up", "Aroon_Down", "Aroon_Osc"
FROM "FE_OSCILLATOR"
WHERE slug = 'bitcoin' AND "timestamp"::date = '2026-05-09';

\echo '--- Bitcoin 2026-05-09 row in cp_backtest FE_OSCILLATORS_SIGNALS ---'
SELECT slug, "timestamp"::date AS dt, m_osc_supertrend_bin, m_osc_aroon_bin
FROM "FE_OSCILLATORS_SIGNALS"
WHERE slug = 'bitcoin' AND "timestamp"::date = '2026-05-09';

\echo '--- NULL count for the 3 new bin columns ---'
SELECT 'm_tvv_bb_bin (FE_TVV_SIGNALS, double prec)' AS col,
       COUNT(*) AS total, COUNT(m_tvv_bb_bin) AS non_null,
       COUNT(*) - COUNT(m_tvv_bb_bin) AS null_count
FROM "FE_TVV_SIGNALS"
UNION ALL
SELECT 'm_osc_supertrend_bin (FE_OSCILLATORS_SIGNALS, bigint)',
       COUNT(*), COUNT(m_osc_supertrend_bin),
       COUNT(*) - COUNT(m_osc_supertrend_bin)
FROM "FE_OSCILLATORS_SIGNALS"
UNION ALL
SELECT 'm_osc_aroon_bin (FE_OSCILLATORS_SIGNALS, bigint)',
       COUNT(*), COUNT(m_osc_aroon_bin),
       COUNT(*) - COUNT(m_osc_aroon_bin)
FROM "FE_OSCILLATORS_SIGNALS";
