"""CP-011 Phase D, Stage 1 — equivalence + coverage-partition gate (no writes).

Stage 1 requirements:
  1.1 freeze methodology (indicator params + bin definitions, from the TA code)
  1.2 prove signal equivalence vs existing FE_*_SIGNALS on a stratified sample
  1.3 build the exact coverage partition (covered / regen-required / warm-up /
      incomplete-with-reason)
  1.4 benchmark regeneration-required slugs (median/p90/projected; stop if >6h)

This script performs NO shadow or canonical writes.
"""
from __future__ import annotations

from typing import cast

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from pit.regenerate import regenerate_core4
from pit.policy import SIGNAL_FAMILIES, CORE_FAMILIES
from pit.db import connect

OUT = Path(__file__).resolve().parent.parent.parent / "report" / "cp011-phase-d"
OUT.mkdir(parents=True, exist_ok=True)
T0 = time.time()

# Stratified equivalence sample (covered assets, varying history lengths)
EQ_SLUGS = [
    "bitcoin", "ethereum", "litecoin", "dogecoin", "ripple", "cardano",
    "polkadot", "chainlink", "uniswap", "aave", "sushi", "yearn-finance",
]

_FAM_TABLES = {
    "momentum": "FE_MOMENTUM_SIGNALS",
    "oscillators": "FE_OSCILLATORS_SIGNALS",
    "tvv": "FE_TVV_SIGNALS",
    "ratios": "FE_RATIOS_SIGNALS",
}


async def _conn():
    return await connect("cp_backtest")


def methodology_freeze() -> dict:
    """Freeze the regeneration methodology from the current TA code (documented)."""
    return {
        "methodology_version": "cp011_pit_v1",
        "signal_source": "raw OHLCV regeneration via repo TA functions (gcp_dmv_*)",
        "indicators": {
            "momentum": {"rsi_periods": [9, 18, 27, 54, 108], "sma": 14, "roc": 9,
                         "williams": 14, "smi": {"period": 14, "k": 3, "d": 3},
                         "cmo": 14, "mom": 10, "tsi": {"short": 13, "long": 25}},
            "oscillators": {"macd": [12, 26, 9], "cci": 20, "adx": 14, "uo": [7, 14, 28],
                            "supertrend": {"atr": 10, "mult": 3.0}, "aroon": 25},
            "tvv": {"sma": [9, 18, 21, 108], "ema": [9, 18, 21, 108], "atr": 21,
                    "bollinger": {"period": 20, "std": 2.0}, "cmf": 21},
            "ratios": {"window_days": 28, "min_days": 5},
        },
        "bin_definitions": {
            "m_mom_roc_bin": "sign(m_mom_roc) [>0=1,<0=-1]",
            "m_mom_williams_%_bin": "> -50=1, < -50=-1",
            "m_mom_smi_bin": ">=25=1, <=-25=-1",
            "m_mom_cmo_bin": ">40=1, <-40=-1",
            "m_mom_mom_bin": ">4000=1, <-4000=-1",
            "m_osc_macd_crossover_bin": "MACD>Signal=1, MACD<Signal=-1",
            "m_osc_cci_bin": "CCI>108=1, CCI<-108=-1",
            "m_osc_uo_bin": "UO<33=1, UO>67=-1",
            "m_osc_ao_bin": "AO>0=1, AO<0=-1",
            "m_osc_trix_bin": "TRIX>0=1, TRIX<0=-1",
            "m_tvv_obv_1d_binary": "sign(OBV 1d change)",
            "d_tvv_sma9_18": "sign(SMA9-SMA18)", "d_tvv_ema9_18": "sign(EMA9-EMA18)",
            "d_tvv_sma21_108": "sign(SMA21-SMA108)", "d_tvv_ema21_108": "sign(EMA21-EMA108)",
            "m_tvv_cmf": "sign(CMF)",
            "ratios_bins": "sign-based on 28d ratios (alpha/beta/omega/sharpe/sortino/"
                           "treynor/common_sense/information/win_loss/win_rate/ror/pain)",
        },
        "note": "Declared Core-4 bins selected from regeneration; extra bins "
                "(supertrend/aroon/bollinger) computed but not scored.",
    }


def compare_frames(regen: pd.DataFrame, exist: pd.DataFrame, fam: str, slug: str) -> dict:
    cols = [c for c in SIGNAL_FAMILIES[fam] if c in regen.columns]
    r = regen[["slug", "timestamp"] + cols].drop_duplicates(subset=["slug", "timestamp"])
    e = exist[["slug", "timestamp"] + cols].drop_duplicates(subset=["slug", "timestamp"])
    e = e[e["slug"] == slug]  # compare against THIS slug's existing rows only
    m = r.merge(e, on=["slug", "timestamp"], how="outer", suffixes=("_r", "_e"))
    total = len(m)
    both_null = both_ok = mismatch = regen_only = exist_only = 0
    per_col = {}
    for c in cols:
        rc, ec = m[f"{c}_r"], m[f"{c}_e"]
        eq = (rc == ec)
        both = rc.notna() & ec.notna()
        mm = int((both & ~eq).sum())
        rnull = int((rc.isna() & ec.notna()).sum())
        enull = int((rc.notna() & ec.isna()).sum())
        per_col[c] = {"match": int((both & eq).sum()), "mismatch": mm,
                      "regen_null_exist_val": rnull, "regen_val_exist_null": enull,
                      "both_null": int((rc.isna() & ec.isna()).sum())}
        both_null += per_col[c]["both_null"]
        both_ok += per_col[c]["match"]
        mismatch += mm
        regen_only += enull
        exist_only += rnull
    return {
        "slug": slug, "family": fam, "rows_compared": total,
        "exact_match": both_ok, "both_null": both_null,
        "mismatch": mismatch, "regen_val_exist_null": regen_only,
        "regen_null_exist_val": exist_only, "per_col": per_col,
    }


async def main() -> None:
    conn = await _conn()
    slug_sql = ",".join(f"'{s}'" for s in EQ_SLUGS)
    rows = await conn.fetch(
        f"""SELECT slug, timestamp, open, high, low, close, volume
            FROM \"1K_coins_ohlcv\" WHERE slug IN ({slug_sql}) ORDER BY slug, timestamp"""
    )
    existing = {}
    for fam, tbl in _FAM_TABLES.items():
        cols = ", ".join(f'"{c}"' for c in SIGNAL_FAMILIES[fam])
        er = await conn.fetch(
            f'SELECT slug, timestamp, {cols} FROM "{tbl}" WHERE slug IN ({slug_sql})'
        )
        existing[fam] = pd.DataFrame([dict(r) for r in er])
        if not existing[fam].empty:
            existing[fam]["timestamp"] = pd.to_datetime(existing[fam]["timestamp"]).dt.tz_localize(None)
    await conn.close()

    df = pd.DataFrame([dict(r) for r in rows])
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    present = sorted(df["slug"].unique())
    print(f"equivalence sample: {len(present)} slugs ({present}), {len(df)} rows")

    # regenerate the WHOLE multi-slug sample at once (TA groupby.apply needs >1 group)
    bench = df[df["slug"] == "bitcoin"]
    t_regen = time.time()
    regen_all = regenerate_core4(df, bench)
    print(f"regeneration time: {time.time()-t_regen:.1f}s rows={len(regen_all)}")

    eq_report = []
    for slug in present:
        regen = regen_all[regen_all["slug"] == slug]
        for fam in CORE_FAMILIES:
            if fam in existing and not existing[fam].empty:
                eq_report.append(compare_frames(regen, existing[fam], fam, slug))

    # aggregate equivalence summary
    agg = {"exact_match": 0, "both_null": 0, "mismatch": 0, "regen_val_exist_null": 0,
           "regen_null_exist_val": 0, "per_family": {}}
    for e in eq_report:
        agg["exact_match"] += e["exact_match"]
        agg["both_null"] += e["both_null"]
        agg["mismatch"] += e["mismatch"]
        agg["regen_val_exist_null"] += e["regen_val_exist_null"]
        agg["regen_null_exist_val"] += e["regen_null_exist_val"]
    per_family: dict[str, dict[str, int]] = {}
    for fam in CORE_FAMILIES:
        fam_rows = [e for e in eq_report if e["family"] == fam]
        per_family[fam] = {
            "exact_match": sum(int(e["exact_match"]) for e in fam_rows),
            "mismatch": sum(int(e["mismatch"]) for e in fam_rows),
            "regen_val_exist_null": sum(int(e["regen_val_exist_null"]) for e in fam_rows),
            "regen_null_exist_val": sum(int(e["regen_null_exist_val"]) for e in fam_rows),
        }
    agg["per_family"] = per_family
    compared = cast(int, agg["exact_match"]) + cast(int, agg["mismatch"]) + cast(int, agg["regen_val_exist_null"]) + cast(int, agg["regen_null_exist_val"])
    print("\n=== EQUIVALENCE (regenerated vs existing FE_*_SIGNALS) ===")
    print(f"  compared cells={compared} exact_match={agg['exact_match']} "
          f"both_null={agg['both_null']} mismatch={agg['mismatch']}")
    print(f"  regen_val_exist_null={agg['regen_val_exist_null']} "
          f"regen_null_exist_val={agg['regen_null_exist_val']}")
    print("  per_family:", json.dumps(agg["per_family"], indent=1))

    payload = {
        "methodology": methodology_freeze(),
        "equivalence_sample_slugs": EQ_SLUGS,
        "equivalence": agg,
        "equivalence_detail": eq_report,
        "runtime_sec": round(time.time() - T0, 1),
    }
    with (OUT / "stage1_equivalence.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"saved -> {OUT / 'stage1_equivalence.json'}")


if __name__ == "__main__":
    asyncio.run(main())
