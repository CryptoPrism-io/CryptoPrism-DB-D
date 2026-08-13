"""CP-012 — read-only forward-return backtests on the validated CP-011 shadow.

Usage:
  python gcp_postgres_sandbox/pit/run_cp012.py --mode dry-run \
      --snapshot report/cp011-phase-d/frozen_ohlcv_cp011_v2.parquet \
      --start 2023-01-01 --end 2024-06-30 \
      --out report/cp011-phase-d/cp012_dryrun.json
  python gcp_postgres_sandbox/pit/run_cp012.py --mode full \
      --snapshot report/cp011-phase-d/frozen_ohlcv_cp011_v2.parquet \
      --out report/cp011-phase-d/cp012_backtests.json

Read-only: loads dmv_rows (SELECT) + frozen snapshot parquet; writes only the
report JSON. No DB writes.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from pit.cp012 import (
    FACTORS, HORIZONS, build_panel, finalize, leakage_check, load_frozen_prices,
    print_summary, run_all, save_report,
)

DEFAULT_SCHEMA = "pit_dmv_cp011_v2_20260812_1315"
DEFAULT_METHOD = "cp011_pit_v2_current_main_full_regen"


async def _load_dmv(schema: str, method: str) -> pd.DataFrame:
    from pit.db import connect

    conn = await connect("cp_backtest", timeout=60)
    try:
        rows = await conn.fetch(
            f"""SELECT slug, date, durability_score, momentum_score, valuation_score,
                       d_pct_var, d_pct_cvar, v_met_ath, v_met_atl,
                       d_met_ath_days, d_met_atl_days, d_met_coin_age_d
                FROM "{schema}".dmv_rows
                WHERE methodology_version = $1 AND incomplete = FALSE
                ORDER BY slug, date""",
            method,
        )
        return pd.DataFrame([dict(r) for r in rows])
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="CP-012 forward-return backtests (read-only)")
    ap.add_argument("--mode", choices=["dry-run", "full"], required=True)
    ap.add_argument("--schema", default=DEFAULT_SCHEMA)
    ap.add_argument("--methodology-version", default=DEFAULT_METHOD)
    ap.add_argument("--snapshot", required=True, type=Path)
    ap.add_argument("--start", default=None, help="dry-run lower date bound")
    ap.add_argument("--end", default=None, help="dry-run upper date bound")
    ap.add_argument("--min-assets", type=int, default=5)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    dmv = asyncio.run(_load_dmv(args.schema, args.methodology_version))
    print(f"dmv_rows loaded: {len(dmv)} (incomplete excluded)", flush=True)
    prices = load_frozen_prices(args.snapshot)
    print(f"frozen prices: {len(prices)} (slug,date)", flush=True)

    panel = build_panel(dmv, prices)
    print(f"panel: {len(panel)} (slug,date) rows; factors={len(FACTORS)} horizons={HORIZONS}", flush=True)

    if args.mode == "dry-run":
        if args.start:
            panel = panel[panel["date"] >= pd.Timestamp(args.start)]
        if args.end:
            panel = panel[panel["date"] <= pd.Timestamp(args.end)]
        print(f"dry-run window: {panel['date'].min().date()} .. {panel['date'].max().date()} "
              f"({len(panel)} rows)", flush=True)

    leak = leakage_check(panel)
    print("leakage check:", leak, flush=True)

    results = finalize(run_all(panel))
    print_summary(results)
    print("=== leakage ===", leak)
    bh = [r for r in results if r.get("bh_significant")]
    mat = [r for r in results if r.get("material")]
    print(f"=== FDR-significant (alpha=0.05): {len(bh)} / {len(results)} ===")
    for r in bh:
        print(f"  {r['factor']:18s} h{r['horizon']:2d} ic={r['ic_pearson_mean']:+.4f} "
              f"p={r['p_value']:.4f} material={r.get('material')}")
    print(f"=== material (|ic|>=0.05 and CI excludes 0): {len(mat)} ===")

    out = Path(args.out) if args.out else Path(
        f"report/cp011-phase-d/cp012_{args.mode}.json")
    payload = {
        "mode": args.mode,
        "schema": args.schema,
        "methodology_version": args.methodology_version,
        "snapshot": str(args.snapshot),
        "min_assets": args.min_assets,
        "window": [str(panel["date"].min().date()), str(panel["date"].max().date())],
        "panel_rows": int(len(panel)),
        "leakage_check": leak,
        "experiments": results,
        "note": "READ-ONLY backtest of the validated CP-011 shadow. Factors are the "
                "PIT dmv scores/var/metrics; forward returns from frozen closes. "
                "No DB writes; no promotion decision."
    }
    save_report(payload, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
