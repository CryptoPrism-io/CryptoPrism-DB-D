"""CP-011 Phase D Stage 2A — component-level benchmark on a stratified 30-asset sample.

Measures each regeneration component on the multi-slug sample (the build processes
multi-slug chunks, which is what the TA functions support):
  indicator gen (momentum / oscillators / tvv)
  ratios (28d trailing)
  PIT var/cvar
  PIT metrics
  assembly (merge + scores)
Reports per-component totals + per-row rates + projected full-universe totals.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from pit.regenerate import (
    regenerate_momentum, regenerate_oscillators, regenerate_tvv, regenerate_ratios,
)
from pit.var_cvar import calculate_var_cvar_pit
from pit.metrics import calculate_metrics_pit
from pit.policy import core_bin_columns
from pit.scores import compute_scores

OUT = Path(__file__).resolve().parent.parent.parent / "report" / "cp011-phase-d"
OUT.mkdir(parents=True, exist_ok=True)

SLUGS = [
    # long-history current
    "bitcoin", "ethereum", "litecoin", "dogecoin", "ripple", "bitcoin-cash",
    "cardano", "polkadot", "chainlink", "solana",
    # mid current
    "uniswap", "aave", "yearn-finance", "compound", "enjin-coin", "decentraland",
    # short current
    "playzap", "onomy-protocol", "joystream", "aevo",
    # delisted
    "vgx-token", "salt", "blockv", "six", "newton", "mcoin1",
    # edge / low-row
    "coinex-token", "kinic", "nodeops", "kubecoin",
]


async def main() -> None:
    from pit.db import connect

    conn = await connect("cp_backtest")
    slug_sql = ",".join(f"'{s}'" for s in SLUGS)
    rows = await conn.fetch(
        f"""SELECT slug, timestamp, open, high, low, close, volume
            FROM \"1K_coins_ohlcv\" WHERE slug IN ({slug_sql}) ORDER BY slug, timestamp"""
    )
    await conn.close()
    df = pd.DataFrame([dict(r) for r in rows])
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    bench = df[df["slug"] == "bitcoin"]
    n_rows = len(df)
    n_slugs = df["slug"].nunique()
    print(f"sample: {n_slugs} slugs, {n_rows} rows, dates={df['timestamp'].nunique()}")

    def t(label, fn):
        t0 = time.time()
        out = fn()
        dt = time.time() - t0
        per_row = dt / n_rows * 1e6  # microseconds/row
        print(f"  {label:16s}: {dt:8.1f}s  ({per_row:.1f} us/row)")
        return dt

    comps = {}
    comps["momentum"] = t("momentum", lambda: regenerate_momentum(df))
    comps["oscillators"] = t("oscillators", lambda: regenerate_oscillators(df))
    comps["tvv"] = t("tvv", lambda: regenerate_tvv(df))
    comps["ratios"] = t("ratios", lambda: regenerate_ratios(df, bench))
    comps["var_cvar"] = t("var_cvar", lambda: calculate_var_cvar_pit(df, window_days=365, min_obs=252, confidence=0.95))
    comps["metrics"] = t("metrics", lambda: calculate_metrics_pit(df))

    def assemble():
        core = core_bin_columns()
        sig = regenerate_momentum(df).merge(regenerate_oscillators(df), on=["slug", "timestamp"], how="outer")
        sig = sig.merge(regenerate_tvv(df), on=["slug", "timestamp"], how="outer")
        sig = sig.merge(regenerate_ratios(df, bench), on=["slug", "timestamp"], how="outer")
        base = df.merge(sig, on=["slug", "timestamp"], how="left")
        for c in core:
            if c not in base.columns:
                base[c] = float("nan")
        compute_scores(
            base,
            durability_cols=[c for c in core if c.startswith("d_")],
            momentum_cols=[c for c in core if c.startswith("m_")],
            valuation_cols=[c for c in core if c.startswith("v_")],
            validate=False,
        )
        return sig

    comps["assemble"] = t("assemble", assemble)

    total = sum(comps.values())
    print(f"  TOTAL: {total:.1f}s  ({total/n_rows*1e6:.1f} us/row)")

    payload = {
        "sample_slugs": len(SLUGS), "sample_rows": n_rows, "sample_dates": int(df["timestamp"].nunique()),
        "components_sec": comps,
        "per_row_us": {k: v / n_rows * 1e6 for k, v in comps.items()},
        "projected_2592976_rows_sec": {k: round(v / n_rows * 2592976, 0) for k, v in comps.items()},
        "projected_total_sec": round(total / n_rows * 2592976, 0),
        "projected_total_hrs": round(total / n_rows * 2592976 / 3600, 1),
    }
    with (OUT / "benchmark_30.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"saved -> {OUT / 'benchmark_30.json'}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
