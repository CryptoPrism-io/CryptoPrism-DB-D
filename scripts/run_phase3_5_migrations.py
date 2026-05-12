# Apply Phase 3 (Dow patterns) and Phase 5 (price levels) migrations on dbcp and cp_backtest.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = [
    REPO_ROOT / "migrations" / "2026_05_11_phase3_dow.sql",
    REPO_ROOT / "migrations" / "2026_05_11_phase5_levels.sql",
]


def apply(db_name):
    url = f"postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{db_name}"
    eng = create_engine(url, pool_pre_ping=True)
    try:
        for m in MIGRATIONS:
            sql_text = m.read_text(encoding="utf-8")
            with eng.begin() as conn:
                conn.execute(text(sql_text))
            logger.info(f"[{db_name}] applied {m.name}")
        # Verify table presence
        with eng.connect() as conn:
            for tbl in ("FE_DOW_PATTERNS", "FE_PRICE_LEVELS"):
                exists = conn.execute(text(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
                ), {"t": tbl}).fetchone()
                if not exists:
                    logger.error(f"[{db_name}] {tbl} was not created")
                    sys.exit(1)
            logger.info(f"[{db_name}] FE_DOW_PATTERNS + FE_PRICE_LEVELS verified.")
    finally:
        eng.dispose()


if __name__ == "__main__":
    apply(DB_NAME)
    apply(DB_NAME_BT)
    logger.info("Phase 3 + Phase 5 migrations applied on both DBs.")
