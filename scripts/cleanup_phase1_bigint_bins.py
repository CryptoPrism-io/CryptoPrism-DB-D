# Post-backfill cleanup on cp_backtest:
# Flip NULL -> 0 in the two bigint Phase 1 bin columns of FE_OSCILLATORS_SIGNALS.
# The initial backfill ran with NaN-propagating bin logic (pre-fix), leaving NULL
# for slugs with insufficient OHLCV history. The updated source code in
# gcp_dmv_osc.py uses np.select(default=0) so future daily runs write 0 directly.
# This one-shot brings historical rows into line with the new convention.
# Idempotent.
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

if Path(".env").exists():
    load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME_BT = os.getenv("DB_NAME_BT", "cp_backtest")

missing = [v for v in ["DB_HOST", "DB_USER", "DB_PASSWORD"] if not os.getenv(v)]
if missing:
    logger.error(f"Missing env vars: {missing}")
    sys.exit(1)


def main():
    url = f"postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}"
    eng = create_engine(url, pool_pre_ping=True)
    try:
        with eng.begin() as conn:
            r1 = conn.execute(text(
                'UPDATE "FE_OSCILLATORS_SIGNALS" SET m_osc_supertrend_bin = 0 '
                'WHERE m_osc_supertrend_bin IS NULL'
            ))
            logger.info(f"m_osc_supertrend_bin: {r1.rowcount:,} rows flipped from NULL to 0")

            r2 = conn.execute(text(
                'UPDATE "FE_OSCILLATORS_SIGNALS" SET m_osc_aroon_bin = 0 '
                'WHERE m_osc_aroon_bin IS NULL'
            ))
            logger.info(f"m_osc_aroon_bin: {r2.rowcount:,} rows flipped from NULL to 0")

        # Verify
        with eng.connect() as conn:
            check = conn.execute(text("""
                SELECT
                    COUNT(*) FILTER (WHERE m_osc_supertrend_bin IS NULL) AS st_null,
                    COUNT(*) FILTER (WHERE m_osc_aroon_bin       IS NULL) AS ar_null
                FROM "FE_OSCILLATORS_SIGNALS"
            """)).fetchone()
            logger.info(f"After cleanup: m_osc_supertrend_bin NULL = {check[0]}, "
                        f"m_osc_aroon_bin NULL = {check[1]}")
            if check[0] != 0 or check[1] != 0:
                logger.error("Cleanup did not eliminate all NULLs")
                sys.exit(1)
        logger.info("Cleanup complete. Both bigint osc bins are NULL-free.")
    finally:
        eng.dispose()


if __name__ == "__main__":
    main()
