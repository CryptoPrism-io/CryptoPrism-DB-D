"""CP-018 — materialize the latest DMV serving snapshot for the top-100 assets.

Reads the validated CP-011 shadow (pit_dmv_cp011_v2_*.dmv_rows, cp_backtest) +
the frozen OHLCV snapshot, and writes a small, fast-serving table:

  cp018_serving.dmv_latest_top100   (database cp_backtest)

  slug, date, durability_score, momentum_score, valuation_score,
  d_pct_var, d_pct_cvar, incomplete, reason, confidence,
  methodology_version, universe_method, generated_at

Selection: top-100 assets by trailing 30d volume; latest dmv row per asset.
Incomplete rows keep NULL scores with an explicit reason. confidence =
fraction of the 4 core fields (3 scores + CVaR) that are non-null.

The full historical panel stays in the research shadow / S3 layer — this table is
only the current snapshot for fast serving (100 rows -> sub-100ms queries; Redis
cache + API route are the follow-on integration).

Idempotent: CREATE SCHEMA/TABLE IF NOT EXISTS + DELETE/INSERT keyed on generated_at.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent.parent
SNAPSHOT = ROOT / "report" / "cp011-phase-d" / "frozen_ohlcv_cp011_v2.parquet"
SCHEMA = "cp018_serving"
TABLE = "dmv_latest_top100"
SOURCE_SCHEMA = "pit_dmv_cp011_v2_20260812_1315"
METHOD = "cp011_pit_v2_current_main_full_regen"
TOP_N = 100
CORE_FIELDS = ["durability_score", "momentum_score", "valuation_score", "d_pct_cvar"]


def top_assets_by_volume(snapshot: Path, n: int = TOP_N) -> list[str]:
    df = pd.read_parquet(snapshot, columns=["slug", "timestamp", "volume"])
    df["date"] = pd.to_datetime(df["timestamp"]).dt.normalize()
    last = df["date"].max()
    trailing = df[df["date"] >= last - pd.Timedelta(days=30)]
    vol = trailing.groupby("slug")["volume"].sum().sort_values(ascending=False)
    return vol.head(n).index.tolist()


async def build_serving(slugs: list[str], conn) -> pd.DataFrame:
    slug_sql = ",".join(f"'{s}'" for s in slugs)
    rows = await conn.fetch(
        f"""SELECT slug, date, durability_score, momentum_score, valuation_score,
                   d_pct_var, d_pct_cvar, incomplete
            FROM "{SOURCE_SCHEMA}".dmv_rows
            WHERE methodology_version = $1
              AND slug IN ({slug_sql})""", METHOD)
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    # latest row per slug
    latest = df.sort_values("date").groupby("slug").tail(1).copy()
    latest = latest.reset_index(drop=True)
    # confidence = fraction of core fields present
    latest["confidence"] = latest[CORE_FIELDS].notna().mean(axis=1)
    latest["reason"] = latest["incomplete"].map(
        {True: "INCOMPLETE_CORE_SIGNALS", False: ""})
    latest["methodology_version"] = METHOD
    latest["universe_method"] = "PIT_APPROX"
    latest["generated_at"] = datetime.now(timezone.utc)
    return latest


async def write_serving(conn, df: pd.DataFrame) -> int:
    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS "{SCHEMA}".{TABLE} (
            slug text NOT NULL,
            date date NOT NULL,
            durability_score double precision,
            momentum_score double precision,
            valuation_score double precision,
            d_pct_var double precision,
            d_pct_cvar double precision,
            incomplete boolean NOT NULL,
            reason text,
            confidence double precision,
            methodology_version text NOT NULL,
            universe_method text NOT NULL,
            generated_at timestamptz NOT NULL,
            PRIMARY KEY (slug, generated_at)
        )""")
    cols = ["slug", "date", "durability_score", "momentum_score", "valuation_score",
            "d_pct_var", "d_pct_cvar", "incomplete", "reason", "confidence",
            "methodology_version", "universe_method", "generated_at"]
    recs = df[cols].to_dict("records")

    def _nullify(v):
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        return v

    args = [tuple(_nullify(r[c]) for c in cols) for r in recs]
    insert_sql = (
        f"""INSERT INTO "{SCHEMA}".{TABLE}
            (slug, date, durability_score, momentum_score, valuation_score,
             d_pct_var, d_pct_cvar, incomplete, reason, confidence,
             methodology_version, universe_method, generated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""")
    async with conn.transaction():
        await conn.executemany(insert_sql, args)
    return len(recs)


async def main_async() -> dict:
    from pit.db import connect

    slugs = top_assets_by_volume(SNAPSHOT)
    print(f"top {len(slugs)} assets by trailing 30d volume")
    conn = await connect("cp_backtest", timeout=120)
    try:
        df = await build_serving(slugs, conn)
        print(f"latest rows: {len(df)} assets, dates {df['date'].min().date()}..{df['date'].max().date()}")
        written = await write_serving(conn, df)
        # verify
        n = await conn.fetchval(f'SELECT COUNT(*) FROM "{SCHEMA}".{TABLE}')
        return {
            "top_n": len(slugs), "written": written, "rows_in_table": int(n),
            "generated_at": df["generated_at"].iloc[0].isoformat(),
            "schema_table": f"{SCHEMA}.{TABLE}", "database": "cp_backtest",
            "incomplete_count": int(df["incomplete"].sum()),
        }
    finally:
        await conn.close()


def main() -> int:
    import asyncio

    res = asyncio.run(main_async())
    print(json.dumps(res, indent=2, default=str))
    (ROOT / "report" / "cp011-phase-d" / "cp018_serving.json").write_text(
        json.dumps(res, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
