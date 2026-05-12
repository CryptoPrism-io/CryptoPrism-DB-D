# Apply Phase 2 candlestick migration on dbcp and cp_backtest.
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
DB_NAME = os.getenv("DB_NAME", "dbcp")
DB_NAME_BT = os.getenv("DB_NAME_BT", "cp_backtest")

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "2026_05_11_phase2_candlestick.sql"

EXPECTED_TABLE = "FE_CANDLESTICK_SIGNALS"
EXPECTED_BINS = [
    "m_cnd_marubozu_bin", "m_cnd_hammer_bin", "m_cnd_hanging_man_bin",
    "m_cnd_shooting_star_bin", "m_cnd_engulfing_bin", "m_cnd_harami_bin",
    "m_cnd_piercing_bin", "m_cnd_dark_cloud_bin", "m_cnd_star_bin",
]


def apply(db_name):
    url = f"postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{db_name}"
    eng = create_engine(url, pool_pre_ping=True)
    try:
        sql_text = MIGRATION.read_text(encoding="utf-8")
        with eng.begin() as conn:
            conn.execute(text(sql_text))
        with eng.connect() as conn:
            tbl = conn.execute(text(
                "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
            ), {"t": EXPECTED_TABLE}).fetchone()
            if not tbl:
                logger.error(f"[{db_name}] {EXPECTED_TABLE} not created")
                sys.exit(1)
            rows = conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'FE_DMV_ALL' AND column_name = ANY(:cols)
            """), {"cols": EXPECTED_BINS}).fetchall()
            found = {r[0] for r in rows}
            missing_cols = [c for c in EXPECTED_BINS if c not in found]
            if missing_cols:
                logger.error(f"[{db_name}] FE_DMV_ALL missing: {missing_cols}")
                sys.exit(1)
        logger.info(f"[{db_name}] Phase 2 migration applied OK.")
    finally:
        eng.dispose()


if __name__ == "__main__":
    apply(DB_NAME)
    apply(DB_NAME_BT)
    logger.info("Phase 2 migration applied to both DBs.")
