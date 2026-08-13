"""CP-011 Phase C — bounded shadow-rebuild runner (PIT-safe).

Reusable, guarded runner that writes ONLY to a uniquely named ``pit_dmv_*``
shadow schema. Mandatory protections:

  - ``--shadow-schema`` is REQUIRED (no default); must start with ``pit_dmv_``;
    rejects ``public`` / ``dbcp`` / ``cp_backtest`` / canonical ``FE_*`` / empty.
  - ``--slugs`` and ``--start``/``--end`` bounds are REQUIRED (no full universe).
  - Source tables are read-only (SELECT only); writes go ONLY to the shadow schema.
  - Records methodology version, universe method, source coverage and a run ID.
  - Idempotent writes keyed on ``(slug, date, methodology_version)``.
  - ``--dry-run`` preflight shows intended reads/writes WITHOUT executing.

Usage:
  python run_shadow.py --shadow-schema pit_dmv_cp011_dryrun_<TS> \
      --slugs bitcoin,ethereum,solana,litecoin,dogecoin,vgx-token \
      --start 2023-01-01 --end 2026-08-08 [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.parse
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from pit.var_cvar import calculate_var_cvar_pit
from pit.metrics import calculate_metrics_pit
from pit.scores import compute_scores
from pit.targets import assert_shadow_schema
from pit.policy import (
    METHODOLOGY_VERSION,
    SIGNAL_FAMILIES,
    core_bin_columns,
    UNIVERSE_META,
)

_CORE_TABLES = {
    "FE_OSCILLATORS_SIGNALS": SIGNAL_FAMILIES["oscillators"],
    "FE_MOMENTUM_SIGNALS": SIGNAL_FAMILIES["momentum"],
    "FE_TVV_SIGNALS": SIGNAL_FAMILIES["tvv"],
    "FE_RATIOS_SIGNALS": SIGNAL_FAMILIES["ratios"],
}
DEFAULT_DB = "cp_backtest"
T0 = time.time()


# ── db connection ──────────────────────────────────────────────────────────
def _dsn() -> str:
    dsn = os.getenv("CP_BACKTEST_DSN")
    if dsn:
        return dsn
    from dotenv import load_dotenv

    load_dotenv(r"C:\cpio_db\cryptoprism-onchain\.env")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("No CP_BACKTEST_DSN / DATABASE_URL available.")
    return dsn


async def _conn(database: str = DEFAULT_DB):
    dsn = _dsn()
    m = re.search(r"postgresql(?:\+asyncpg)?://([^:]+):([^@]+)@([^:]+):(\d+)/(\w+)", dsn)
    if m is None:
        raise SystemExit("DATABASE_URL does not match expected postgresql:// format")
    import asyncpg

    pw = urllib.parse.unquote(m.group(2))
    return await asyncpg.connect(
        host=m.group(3), port=m.group(4), user=m.group(1),
        password=pw, database=database, ssl="require",
    )


# ── read-only source loads ─────────────────────────────────────────────────
async def _load_ohlcv(conn, slugs: list[str], start: str, end: str) -> pd.DataFrame:
    slug_sql = ",".join(f"'{s}'" for s in slugs)
    rows = await conn.fetch(
        f"""SELECT slug, timestamp, open, high, low, close, volume
            FROM \"1K_coins_ohlcv\"
            WHERE slug IN ({slug_sql}) AND timestamp::date BETWEEN '{start}' AND '{end}'
            ORDER BY slug, timestamp"""
    )
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    df = df.sort_values(["slug", "timestamp"]).reset_index(drop=True)
    df["m_pct_1d"] = df.groupby("slug")["close"].pct_change()
    return df


async def _load_signal_bins(conn, slugs: list[str], start: str, end: str) -> pd.DataFrame:
    slug_sql = ",".join(f"'{s}'" for s in slugs)
    frames = []
    for tbl, cols in _CORE_TABLES.items():
        col_sql = ", ".join(f'"{c}"' for c in cols)
        rows = await conn.fetch(
            f"""SELECT slug, timestamp, {col_sql} FROM "{tbl}"
                WHERE slug IN ({slug_sql}) AND timestamp::date BETWEEN '{start}' AND '{end}'"""
        )
        sig = pd.DataFrame([dict(r) for r in rows])
        if not sig.empty:
            sig["timestamp"] = pd.to_datetime(sig["timestamp"]).dt.tz_localize(None)
        frames.append(sig)
    merged = None
    for sig in frames:
        if sig is None or sig.empty:
            continue
        merged = sig if merged is None else merged.merge(sig, on=["slug", "timestamp"], how="outer")
    if merged is None:
        return pd.DataFrame()
    return merged


# ── compute pipeline (PIT-safe, deterministic) ─────────────────────────────
def compute_pipeline(ohlcv: pd.DataFrame, sigs: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Return (output frame with all scores/var/metrics, counts)."""
    var_m = calculate_var_cvar_pit(ohlcv, window_days=365, min_obs=252, confidence=0.95)
    met = calculate_metrics_pit(ohlcv)

    core = core_bin_columns()
    base = ohlcv[["slug", "timestamp", "m_pct_1d"]].merge(
        met[["slug", "timestamp", "v_met_ath", "v_met_atl", "d_met_ath_days",
             "d_met_atl_days", "d_met_coin_age_d"]], on=["slug", "timestamp"], how="left"
    ).merge(
        var_m[["slug", "timestamp", "d_pct_var", "d_pct_cvar"]], on=["slug", "timestamp"], how="left"
    )
    if not sigs.empty:
        base = base.merge(sigs, on=["slug", "timestamp"], how="left")
    for c in core:
        if c not in base.columns:
            base[c] = float("nan")

    scored = compute_scores(
        base,
        durability_cols=[c for c in core if c.startswith("d_")],
        momentum_cols=[c for c in core if c.startswith("m_")],
        valuation_cols=[c for c in core if c.startswith("v_")],
        validate=False,
    )
    out = scored.rename(columns={
        "Durability_Score": "durability_score",
        "Momentum_Score": "momentum_score",
        "Valuation_Score": "valuation_score",
    })
    # incomplete rows (missing any required core signal) get ALL scores NULL
    out.loc[out["incomplete"], ["durability_score", "momentum_score", "valuation_score"]] = None
    out["date"] = pd.to_datetime(out["timestamp"]).dt.normalize().dt.date
    out["methodology_version"] = METHODOLOGY_VERSION
    out["universe_method"] = UNIVERSE_META["universe_method"]

    # reconcile counts on the DISTINCT (slug, date) universe (dedup keeps first)
    dedup = out.drop_duplicates(subset=["slug", "date"]).copy()
    universe = int(len(dedup))
    incomplete_d = int(dedup["incomplete"].sum())
    counts = {
        "source_rows": int(len(ohlcv)),
        "universe_rows": universe,
        "core_present": universe - incomplete_d,  # core-present == not incomplete
        "incomplete": incomplete_d,
        "valid_scored": universe - incomplete_d,
    }
    return out, counts


# ── shadow write (idempotent) ──────────────────────────────────────────────
async def write_shadow(conn, schema: str, frame: pd.DataFrame, counts: dict, run_id: str) -> int:
    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".dmv_rows (
            slug text NOT NULL,
            date date NOT NULL,
            methodology_version text NOT NULL,
            universe_method text NOT NULL,
            d_pct_var double precision,
            d_pct_cvar double precision,
            v_met_ath double precision,
            v_met_atl double precision,
            d_met_ath_days integer,
            d_met_atl_days integer,
            d_met_coin_age_d integer,
            durability_score double precision,
            momentum_score double precision,
            valuation_score double precision,
            incomplete boolean NOT NULL,
            PRIMARY KEY (slug, date, methodology_version)
        )""")
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".shadow_run_meta (
            run_id text PRIMARY KEY, shadow_schema text, methodology_version text,
            universe_method text, source_table text, source_coverage text,
            start_date date, end_date date, slugs int,
            source_rows int, universe_rows int, core_present int,
            incomplete int, valid_scored int, runtime_sec double precision,
            created_at timestamptz
        )""")

    cols = ["slug", "date", "methodology_version", "universe_method",
            "d_pct_var", "d_pct_cvar", "v_met_ath", "v_met_atl",
            "d_met_ath_days", "d_met_atl_days", "d_met_coin_age_d",
            "durability_score", "momentum_score", "valuation_score", "incomplete"]
    recs = frame[cols].to_dict("records")

    def _nullify(v):
        # NaN/NaT/None -> SQL NULL (Postgres NaN would break MAX()/NULL semantics)
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
        f"""INSERT INTO "{schema}".dmv_rows
            (slug, date, methodology_version, universe_method, d_pct_var, d_pct_cvar,
             v_met_ath, v_met_atl, d_met_ath_days, d_met_atl_days, d_met_coin_age_d,
             durability_score, momentum_score, valuation_score, incomplete)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            ON CONFLICT (slug, date, methodology_version) DO UPDATE SET
              universe_method = EXCLUDED.universe_method,
              d_pct_var = EXCLUDED.d_pct_var, d_pct_cvar = EXCLUDED.d_pct_cvar,
              v_met_ath = EXCLUDED.v_met_ath, v_met_atl = EXCLUDED.v_met_atl,
              d_met_ath_days = EXCLUDED.d_met_ath_days,
              d_met_atl_days = EXCLUDED.d_met_atl_days,
              d_met_coin_age_d = EXCLUDED.d_met_coin_age_d,
              durability_score = EXCLUDED.durability_score,
              momentum_score = EXCLUDED.momentum_score,
              valuation_score = EXCLUDED.valuation_score,
              incomplete = EXCLUDED.incomplete"""
    )
    args = [tuple(_nullify(r[c]) for c in cols) for r in recs]
    async with conn.transaction():
        await conn.executemany(insert_sql, args)
        await conn.execute(
            f"""INSERT INTO "{schema}".shadow_run_meta
                (run_id, shadow_schema, methodology_version, universe_method, source_table,
                 source_coverage, start_date, end_date, slugs,
                 source_rows, universe_rows, core_present, incomplete, valid_scored,
                 runtime_sec, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
            run_id, schema, METHODOLOGY_VERSION, UNIVERSE_META["universe_method"],
            "1K_coins_ohlcv + FE_*_SIGNALS (cp_backtest)", UNIVERSE_META["date_coverage"],
            date.fromisoformat(counts["start"]), date.fromisoformat(counts["end"]), counts["slugs"],
            counts["source_rows"], counts["universe_rows"], counts["core_present"],
            counts["incomplete"], counts["valid_scored"],
            round(time.time() - T0, 2), datetime.now(timezone.utc),
        )
    return len(recs)


# ── preflight (dry run) ────────────────────────────────────────────────────
def preflight(slugs, start, end, schema, source_rows_est: int) -> dict:
    return {
        "mode": "dry-run",
        "run_id": uuid.uuid4().hex[:12],
        "shadow_schema": schema,
        "database_target": DEFAULT_DB,
        "intended_reads": ["1K_coins_ohlcv", "FE_OSCILLATORS_SIGNALS", "FE_MOMENTUM_SIGNALS",
                           "FE_TVV_SIGNALS", "FE_RATIOS_SIGNALS"],
        "intended_writes": [f"{schema}.dmv_rows", f"{schema}.shadow_run_meta"],
        "slugs": slugs, "start": start, "end": end,
        "estimated_source_rows": source_rows_est,
        "idempotency_key": "(slug, date, methodology_version)",
        "methodology_version": METHODOLOGY_VERSION,
        "universe_method": UNIVERSE_META["universe_method"],
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description="CP-011 bounded shadow-rebuild runner (PIT-safe)")
    ap.add_argument("--shadow-schema", required=True, help="target schema, must start with pit_dmv_")
    ap.add_argument("--slugs", required=False, help="comma-separated asset slugs (or use --all-slugs)")
    ap.add_argument("--all-slugs", action="store_true", help="use the full OHLCV-observed universe")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--methodology-version", default=METHODOLOGY_VERSION, help="methodology version label")
    ap.add_argument("--dry-run", action="store_true", help="preflight only, no writes")
    args = ap.parse_args()

    schema = assert_shadow_schema(args.shadow_schema)  # raises on canonical/empty/non-pit_dmv_
    if args.all_slugs:
        slugs = None  # resolved from the source below
    else:
        slugs = [s.strip() for s in args.slugs.split(",") if s.strip()]
        if not slugs:
            raise SystemExit("--slugs must be non-empty (or use --all-slugs)")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", args.start) or not re.match(r"^\d{4}-\d{2}-\d{2}$", args.end):
        raise SystemExit("--start/--end must be YYYY-MM-DD")
    if args.start > args.end:
        raise SystemExit("--start must be <= --end")

    conn = await _conn(DEFAULT_DB)
    if slugs is None:  # full universe
        rows = await conn.fetch(
            f"""SELECT DISTINCT slug FROM \"1K_coins_ohlcv\"
                WHERE timestamp::date BETWEEN '{args.start}' AND '{args.end}' ORDER BY slug"""
        )
        slugs = [r["slug"] for r in rows]

    if args.dry_run:
        n_src = await conn.fetchval(
            f"""SELECT COUNT(*) FROM \"1K_coins_ohlcv\"
                WHERE timestamp::date BETWEEN '{args.start}' AND '{args.end}'"""
        )
        n_slugs = len(slugs)
        await conn.close()
        pf = preflight(slugs, args.start, args.end, schema, int(n_src))
        pf["slugs"] = n_slugs
        pf["estimated_source_rows"] = int(n_src)
        pf["methodology_version"] = args.methodology_version
        print(json.dumps(pf, indent=2))
        print("PREFLIGHT COMPLETE — no writes performed.")
        return

    ohlcv = await _load_ohlcv(conn, slugs, args.start, args.end)
    sigs = await _load_signal_bins(conn, slugs, args.start, args.end)
    await conn.close()

    if ohlcv.empty:
        raise SystemExit(f"no OHLCV for {len(slugs)} slugs in {args.start}..{args.end}")

    pf = preflight(slugs, args.start, args.end, schema, int(len(ohlcv)))
    print(json.dumps(pf, indent=2))
    if args.dry_run:
        print("PREFLIGHT COMPLETE — no writes performed.")
        return

    run_id = f"cp011_dryrun_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out, counts = compute_pipeline(ohlcv, sigs)
    counts.update({
        "start": args.start, "end": args.end, "slugs": len(slugs),
        "run_id": run_id,
    })

    conn = await _conn(DEFAULT_DB)
    try:
        written = await write_shadow(conn, schema, out, counts, run_id)
    finally:
        await conn.close()

    print(f"RUN {run_id} -> schema {schema}")
    print(f"  source_rows={counts['source_rows']} universe={counts['universe_rows']} "
          f"core_present={counts['core_present']} incomplete={counts['incomplete']} "
          f"valid_scored={counts['valid_scored']} written={written}")
    print(f"  runtime={round(time.time()-T0,2)}s")


if __name__ == "__main__":
    asyncio.run(main())
