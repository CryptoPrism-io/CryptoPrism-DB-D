# Run Phase 1 migrations on dbcp and cp_backtest.
# Idempotent: ADD COLUMN IF NOT EXISTS. Safe to re-run.
# Reads credentials from .env (local) or GitHub secrets.
# Usage: python scripts/run_phase1_migrations.py
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
        logger.info(".env loaded.")
    else:
        logger.warning(".env not found; relying on existing env vars.")

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

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = [
    REPO_ROOT / "migrations" / "2026_05_11_phase1_bb.sql",
    REPO_ROOT / "migrations" / "2026_05_11_phase1_supertrend_aroon.sql",
]

EXPECTED_COLUMNS = [
    ("FE_TVV", "BB_Mid"),
    ("FE_TVV", "BB_Upper"),
    ("FE_TVV", "BB_Lower"),
    ("FE_TVV", "BB_Width"),
    ("FE_TVV", "BB_Pct_B"),
    ("FE_TVV_SIGNALS", "m_tvv_bb_bin"),
    ("FE_OSCILLATOR", "Supertrend_Line"),
    ("FE_OSCILLATOR", "Supertrend_Dir"),
    ("FE_OSCILLATOR", "Aroon_Up"),
    ("FE_OSCILLATOR", "Aroon_Down"),
    ("FE_OSCILLATOR", "Aroon_Osc"),
    ("FE_OSCILLATORS_SIGNALS", "m_osc_supertrend_bin"),
    ("FE_OSCILLATORS_SIGNALS", "m_osc_aroon_bin"),
]


def engine_for(db_name):
    url = f"postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{db_name}"
    return create_engine(url, pool_pre_ping=True)


def run_one_migration(conn, path):
    sql_text = path.read_text(encoding="utf-8")
    logger.info(f"Applying {path.name}")
    conn.execute(text(sql_text))


def verify(conn, db_name):
    rows = conn.execute(text("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_name IN ('FE_TVV','FE_TVV_SIGNALS','FE_OSCILLATOR','FE_OSCILLATORS_SIGNALS')
          AND column_name IN (
              'BB_Mid','BB_Upper','BB_Lower','BB_Width','BB_Pct_B','m_tvv_bb_bin',
              'Supertrend_Line','Supertrend_Dir','Aroon_Up','Aroon_Down','Aroon_Osc',
              'm_osc_supertrend_bin','m_osc_aroon_bin'
          )
        ORDER BY table_name, column_name;
    """)).fetchall()
    found = {(r[0], r[1]) for r in rows}
    missing_cols = [c for c in EXPECTED_COLUMNS if c not in found]
    logger.info(f"[{db_name}] verified: {len(found)}/{len(EXPECTED_COLUMNS)} columns present")
    if missing_cols:
        logger.error(f"[{db_name}] MISSING columns: {missing_cols}")
        return False
    return True


def apply_to(db_name):
    logger.info(f"=== Connecting to {db_name} ===")
    engine = engine_for(db_name)
    try:
        with engine.begin() as conn:
            for migration in MIGRATIONS:
                run_one_migration(conn, migration)
        with engine.connect() as conn:
            ok = verify(conn, db_name)
            if not ok:
                raise SystemExit(f"Verification failed on {db_name}")
        logger.info(f"=== {db_name} done ===")
    finally:
        engine.dispose()


if __name__ == "__main__":
    apply_to(DB_NAME)
    apply_to(DB_NAME_BT)
    logger.info("All Phase 1 migrations applied and verified.")
