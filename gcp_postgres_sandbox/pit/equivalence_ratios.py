"""CP-011 Phase D Stage 2A — ratios fast-path equivalence proof.

Freezes a stratified 30-asset OHLCV snapshot from cp_backtest to a parquet file,
then runs the reference regenerate_ratios and the indexed regenerate_ratios_fast
on the SAME frozen input and proves byte-identical output:

  - same row count / key set / key uniqueness
  - identical null patterns
  - identical raw float values (all 11 ratio families + d_rat_beta)
  - identical assigned bins

Output: report/cp011-phase-d/ratios_equivalence.json + frozen snapshot parquet.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from pit.regenerate import regenerate_ratios, regenerate_ratios_fast

OUT = Path(__file__).resolve().parent.parent.parent / "report" / "cp011-phase-d"
SNAP = Path(__file__).resolve().parent / "frozen_sample_30.parquet"

SLUGS = [
    "bitcoin", "ethereum", "litecoin", "dogecoin", "ripple", "bitcoin-cash",
    "cardano", "polkadot", "chainlink", "solana",
    "uniswap", "aave", "yearn-finance", "compound", "enjin-coin", "decentraland",
    "playzap", "onomy-protocol", "joystream", "aevo",
    "vgx-token", "salt", "blockv", "six", "newton", "mcoin1",
    "coinex-token", "kinic", "nodeops", "kubecoin",
]

RATIO_COLS = [
    "m_rat_alpha", "d_rat_beta", "v_rat_sharpe", "v_rat_sortino",
    "v_rat_teynor", "v_rat_common_sense", "v_rat_information",
    "v_rat_win_loss", "m_rat_win_rate", "m_rat_ror", "d_rat_pain",
]
BIN_COLS = [c + "_bin" for c in RATIO_COLS]


def _load_from_db() -> pd.DataFrame:
    from pit.db import connect

    async def _go():
        conn = await connect("cp_backtest")
        slug_sql = ",".join(f"'{s}'" for s in SLUGS)
        rows = await conn.fetch(
            f"""SELECT slug, timestamp, open, high, low, close, volume
                FROM \"1K_coins_ohlcv\" WHERE slug IN ({slug_sql}) ORDER BY slug, timestamp"""
        )
        await conn.close()
        return rows

    rows = asyncio.run(_go())
    df = pd.DataFrame([dict(r) for r in rows])
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    df = df.sort_values(["slug", "timestamp"]).reset_index(drop=True)
    df["m_pct_1d"] = df.groupby("slug")["close"].pct_change()
    return df


def _canonical(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values(["slug", "timestamp"]).reset_index(drop=True)


def _null_pattern(df: pd.DataFrame) -> pd.DataFrame:
    return df[["slug", "timestamp", *RATIO_COLS]].isna()


def _report(old: pd.DataFrame, new: pd.DataFrame) -> dict:
    old = _canonical(old)
    new = _canonical(new)
    key_o = old[["slug", "timestamp"]]
    key_n = new[["slug", "timestamp"]]
    keys_o = set(zip(key_o["slug"], key_o["timestamp"]))
    keys_n = set(zip(key_n["slug"], key_n["timestamp"]))

    # row-level comparison of the 11 raw ratios (float equality, not tolerance)
    cmp_rows = old.merge(
        new, on=["slug", "timestamp"], suffixes=("_old", "_new"), how="outer"
    )
    float_mismatch = []
    for c in RATIO_COLS:
        a = cmp_rows[f"{c}_old"]
        b = cmp_rows[f"{c}_new"]
        na_ok = (a.isna() & b.isna()).all()
        if na_ok:
            continue
        mism = cmp_rows[a.fillna(np.inf) != b.fillna(np.inf)]
        if not mism.empty:
            float_mismatch.append({c: int(len(mism))})

    # null patterns per column must match
    np_o = _null_pattern(old)
    np_n = _null_pattern(new)
    null_ok = bool((np_o.fillna(0).astype(str) == np_n.fillna(0).astype(str)).all().all())
    null_diffs = []
    for c in RATIO_COLS:
        a = np_o[c]
        b = np_n[c]
        if not (a == b).all():
            null_diffs.append({c: int((a != b).sum())})

    # bins byte-identical on the shared key set
    bins_o = old[["slug", "timestamp", *BIN_COLS]].set_index(["slug", "timestamp"]).sort_index()
    bins_n = new[["slug", "timestamp", *BIN_COLS]].set_index(["slug", "timestamp"]).sort_index()
    if bins_o.shape == bins_n.shape:
        bin_diff = int((bins_o != bins_n).to_numpy().sum())
        bin_mismatch_cols = [
            c for c in BIN_COLS if (bins_o[c] != bins_n[c]).any()
        ]
    else:
        bin_diff = -1
        bin_mismatch_cols = ["shape-mismatch"]

    return {
        "old_rows": int(len(old)),
        "new_rows": int(len(new)),
        "rows_equal": len(old) == len(new),
        "keys_equal": keys_o == keys_n,
        "key_uniqueness": bool(key_o.duplicated().sum() == 0 and key_n.duplicated().sum() == 0),
        "only_old_keys": len(keys_o - keys_n),
        "only_new_keys": len(keys_n - keys_o),
        "null_patterns_equal": bool(null_ok),
        "null_diff_cols": null_diffs,
        "float_mismatch_cols": float_mismatch,
        "bin_cell_diffs": bin_diff,
        "bin_mismatch_cols": bin_mismatch_cols,
        "dtype_match": {c: str(old[c].dtype) == str(new[c].dtype) for c in RATIO_COLS},
        "inf_counts": {
            "old": {c: int(old[c].isin([np.inf, -np.inf]).sum()) for c in RATIO_COLS},
            "new": {c: int(new[c].isin([np.inf, -np.inf]).sum()) for c in RATIO_COLS},
        },
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    if not SNAP.exists():
        print("snapshot missing — freezing 30-asset sample from cp_backtest...", flush=True)
        df = _load_from_db()
        df.to_parquet(SNAP, index=False)
        print(f"  frozen {len(df)} rows / {df['slug'].nunique()} slugs -> {SNAP}", flush=True)
    else:
        df = pd.read_parquet(SNAP)
        print(f"loaded frozen snapshot: {len(df)} rows / {df['slug'].nunique()} slugs", flush=True)
    bench = df[df["slug"] == "bitcoin"]

    t_ref = time.time()
    old = regenerate_ratios(df, bench, include_raw=True)
    ref_sec = time.time() - t_ref
    print(f"reference regenerate_ratios: {ref_sec:.1f}s -> {len(old)} rows", flush=True)

    t_fast = time.time()
    new = regenerate_ratios_fast(df, bench, include_raw=True)
    fast_sec = time.time() - t_fast
    print(f"fast     regenerate_ratios_fast: {fast_sec:.1f}s -> {len(new)} rows", flush=True)

    rep = _report(old, new)
    rep["reference_sec"] = round(ref_sec, 2)
    rep["fast_sec"] = round(fast_sec, 2)
    rep["speedup"] = round(ref_sec / fast_sec, 2) if fast_sec > 0 else None
    rep["snapshot"] = str(SNAP)
    rep["rows"] = int(len(df))
    rep["slugs"] = int(df["slug"].nunique())
    rep["dates"] = int(df["timestamp"].nunique())
    rep["total_sec"] = round(time.time() - t0, 2)

    equiv_pass = (
        rep["rows_equal"]
        and rep["keys_equal"]
        and rep["key_uniqueness"]
        and rep["null_patterns_equal"]
        and not rep["float_mismatch_cols"]
        and rep["bin_cell_diffs"] == 0
        and not rep["bin_mismatch_cols"]
        and all(rep["dtype_match"].values())
    )
    rep["equiv_pass"] = bool(equiv_pass)

    with (OUT / "ratios_equivalence.json").open("w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2)
    print(json.dumps(rep, indent=2, default=str))
    print(f"saved -> {OUT / 'ratios_equivalence.json'}")
    return 0 if equiv_pass else 1


if __name__ == "__main__":
    sys.exit(main())
