# Apply Phase 4 FE_DMV_ALL column additions on dbcp and cp_backtest.
# Idempotent. Safe to re-run.
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

if not os.getenv("GITHUB_ACTIONS"):
    if Path(".env").exists():
        load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "dbcp")
DB_NAME_BT = os.getenv("DB_NAME_BT", "cp_backtest")

missing = [v for v in ["DB_HOST", "DB_USER", "DB_PASSWORD"] if not os.getenv(v)]
if missing:
    logger.error(f"Missing env vars: {missing}")
    sys.exit(1)

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "2026_05_11_phase4_dmv_all.sql"
EXPECTED = ["m_tvv_bb_bin", "m_osc_supertrend_bin", "m_osc_aroon_bin"]


def apply(db_name):
    url = f"postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{db_name}"
    eng = create_engine(url, pool_pre_ping=True)
    try:
        sql_text = MIGRATION.read_text(encoding="utf-8")
        with eng.begin() as conn:
            conn.execute(text(sql_text))
        with eng.connect() as conn:
            rows = conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'FE_DMV_ALL'
                  AND column_name IN ('m_tvv_bb_bin','m_osc_supertrend_bin','m_osc_aroon_bin')
            """)).fetchall()
            found = {r[0] for r in rows}
        missing_cols = [c for c in EXPECTED if c not in found]
        if missing_cols:
            logger.error(f"[{db_name}] MISSING: {missing_cols}")
            sys.exit(1)
        logger.info(f"[{db_name}] FE_DMV_ALL extended with 3 Phase 1 bin columns. OK.")
    finally:
        eng.dispose()


if __name__ == "__main__":
    apply(DB_NAME)
    apply(DB_NAME_BT)
    logger.info("Phase 4 FE_DMV_ALL migration applied to both DBs.")
