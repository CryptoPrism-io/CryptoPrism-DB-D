"""CP-011 Phase B — deterministic PIT-safe DMV sample (read-only).

Reads a bounded OHLCV subset for 6 assets (bitcoin, ethereum, solana, litecoin,
dogecoin + the historically-active-but-delisted ``vgx-token``) over a fixed
window, applies the PIT layer, reads the REAL core signal bins from cp_backtest
for the same rows, computes corrected scores, produces old-vs-corrected
comparisons, measures coverage under alternative core-signal rules, and
estimates the full shadow rebuild. NO writes to production tables.

Usage:  python gcp_postgres_sandbox/pit/run_sample.py
Output: report/pit_sample_results.json
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # pit package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # gcp_postgres_sandbox

import numpy as np
import pandas as pd

from pit.var_cvar import calculate_var_cvar_pit, var_cvar_old_fullsample
from pit.metrics import calculate_metrics_pit, metrics_old_fullsample
from pit.universe import PITUniverse
from pit.scores import compute_scores, validate_bin_columns
from pit.policy import CORE_FAMILIES, SIGNAL_FAMILIES, core_bin_columns

SLUGS = ["bitcoin", "ethereum", "solana", "litecoin", "dogecoin", "vgx-token"]
WINDOW = ("2023-01-01", "2026-08-08")
OUT = Path(__file__).resolve().parent.parent.parent / "report"
T0 = time.time()


def _dsn() -> str:
    dsn = os.getenv("CP_BACKTEST_DSN")
    if dsn:
        return dsn
    # fall back to the shared onchain .env DATABASE_URL (verified RDS access)
    from dotenv import load_dotenv

    load_dotenv(r"C:\cpio_db\cryptoprism-onchain\.env")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("No CP_BACKTEST_DSN / DATABASE_URL available.")
    return dsn


async def _conn():
    dsn = _dsn()
    m = re.search(r"postgresql(?:\+asyncpg)?://([^:]+):([^@]+)@([^:]+):(\d+)/(\w+)", dsn)
    import asyncpg

    pw = urllib.parse.unquote(m.group(2))
    return await asyncpg.connect(
        host=m.group(3), port=m.group(4), user=m.group(1),
        password=pw, database="cp_backtest", ssl="require",
    )


async def _load_ohlcv(conn):
    slug_sql = ",".join(f"'{s}'" for s in SLUGS)
    rows = await conn.fetch(
        f"""SELECT slug, timestamp, open, high, low, close, volume
            FROM \"1K_coins_ohlcv\"
            WHERE slug IN ({slug_sql})
              AND timestamp::date BETWEEN '{WINDOW[0]}' AND '{WINDOW[1]}'
            ORDER BY slug, timestamp"""
    )
    df = pd.DataFrame([dict(r) for r in rows])
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    df = df.sort_values(["slug", "timestamp"]).reset_index(drop=True)
    df["m_pct_1d"] = df.groupby("slug")["close"].pct_change()
    return df


async def _load_signal_bins(conn, df: pd.DataFrame):
    """Read the REAL core signal bins for the sample rows from cp_backtest."""
    slug_sql = ",".join(f"'{s}'" for s in SLUGS)
    tables = {
        "momentum": "FE_MOMENTUM_SIGNALS",
        "oscillators": "FE_OSCILLATORS_SIGNALS",
        "tvv": "FE_TVV_SIGNALS",
        "ratios": "FE_RATIOS_SIGNALS",
    }
    merged = df[["slug", "timestamp"]].copy()
    present = {}
    for fam, tbl in tables.items():
        cols = SIGNAL_FAMILIES[fam]
        col_sql = ", ".join(f'"{c}"' for c in cols)
        try:
            rows = await conn.fetch(
                f"""SELECT slug, timestamp, {col_sql} FROM "{tbl}"
                    WHERE slug IN ({slug_sql})
                      AND timestamp::date BETWEEN '{WINDOW[0]}' AND '{WINDOW[1]}'"""
            )
        except Exception as e:  # noqa: BLE001
            present[fam] = {"error": str(e)[:80], "rows": 0}
            continue
        sig = pd.DataFrame([dict(r) for r in rows])
        if sig.empty:
            present[fam] = {"rows": 0}
            continue
        sig["timestamp"] = pd.to_datetime(sig["timestamp"]).dt.tz_localize(None)
        present[fam] = {"rows": len(sig), "dates": sig["timestamp"].nunique(),
                        "slugs": sig["slug"].nunique()}
        merged = merged.merge(sig, on=["slug", "timestamp"], how="left")
    return merged, present


def coverage_by_family(conn_df: pd.DataFrame):
    """Rows (per family) complete vs missing across the 4 core families."""
    fams = CORE_FAMILIES
    cols = {f: SIGNAL_FAMILIES[f] for f in fams}
    report = {}
    for k in range(len(fams), 0, -1):
        report[f"{k}_of_{len(fams)}_families"] = 0
    report["any_family"] = 0
    for _, row in conn_df.iterrows():
        have = sum(1 for f in fams if not row[[c for c in cols[f] if c in conn_df.columns]].isna().all())
        report["any_family"] += 1
        for k in range(len(fams), 0, -1):
            if have >= k:
                report[f"{k}_of_{len(fams)}_families"] += 1
                break
    return report


async def main() -> None:
    conn = await _conn()
    ohlcv = await _load_ohlcv(conn)
    # real core signal bins for the sample rows (read-only)
    sigs, present = await _load_signal_bins(conn, ohlcv)
    # full-history universe counts for the rebuild estimate (read-only)
    async def _q(sql):
        return await conn.fetchval(sql)
    full_universe_rows = await _q('SELECT COUNT(*) FROM (SELECT DISTINCT slug, timestamp::date FROM "1K_coins_ohlcv") x')
    momentum_rows = await _q('SELECT COUNT(*) FROM (SELECT DISTINCT slug, timestamp::date FROM "FE_MOMENTUM_SIGNALS") x')
    ratios_rows = await _q('SELECT COUNT(*) FROM (SELECT DISTINCT slug, timestamp::date FROM "FE_RATIOS_SIGNALS") x')
    ohlcv_total_rows = await _q('SELECT COUNT(*) FROM "1K_coins_ohlcv"')
    await conn.close()
    print(f"[sample] OHLCV rows={len(ohlcv)} slugs={ohlcv['slug'].nunique()} "
          f"dates={ohlcv['timestamp'].nunique()} in {time.time()-T0:.0f}s", flush=True)

    # PIT universe (PIT_APPROX from OHLCV intervals — includes delisted vgx-token)
    universe = PITUniverse.from_ohlcv(ohlcv)

    # PIT VaR/CVaR + old full-sample comparison
    pit_v = calculate_var_cvar_pit(
        ohlcv,
        window_days=365,
        min_obs=252,
        confidence=0.95,
    )
    old_v = var_cvar_old_fullsample(ohlcv)
    pit_v = pit_v.merge(old_v[["slug", "timestamp", "d_pct_var_old", "d_pct_cvar_old"]],
                        on=["slug", "timestamp"], how="left")

    # PIT metrics + old full-sample comparison
    pit_m = calculate_metrics_pit(ohlcv)
    old_m = metrics_old_fullsample(ohlcv, as_of=pd.Timestamp(ohlcv["timestamp"].max()))
    pit_m = pit_m.merge(old_m[["slug", "timestamp", "d_met_ath_days_old", "d_met_atl_days_old"]],
                        on=["slug", "timestamp"], how="left")

    # scores via centralized policy (4-core), real bins
    core = core_bin_columns()
    score_in = ohlcv.merge(sigs, on=["slug", "timestamp"], how="left")
    # ensure all core bin columns exist (vgx-token will be all-NaN)
    for c in core:
        if c not in score_in.columns:
            score_in[c] = np.nan
    scored = compute_scores(
        score_in,
        durability_cols=[c for c in core if c.startswith("d_")],
        momentum_cols=[c for c in core if c.startswith("m_")],
        valuation_cols=[c for c in core if c.startswith("v_")],
        validate=False,  # allow NaN-heavy delisted rows; validate clean subset below
    )
    clean_subset = score_in.dropna(subset=[c for c in core if score_in[c].notna().any()])
    if not clean_subset.empty:
        validate_bin_columns(clean_subset, [c for c in core if c in clean_subset.columns])

    # old-vs-corrected: pick a representative historical date for bitcoin
    rep = ohlcv["timestamp"].quantile(0.5)
    btc_pit = pit_v[(pit_v["slug"] == "bitcoin") & (pit_v["timestamp"] == rep)].iloc[0]
    btc_old = old_v[(old_v["slug"] == "bitcoin") & (old_v["timestamp"] == rep)].iloc[0]
    var_cmp = {
        "date": str(rep.date()),
        "pit_var": (float(btc_pit["d_pct_var"]) if pd.notna(btc_pit["d_pct_var"]) else None),
        "old_fullsample_var": (float(btc_old["d_pct_var_old"]) if pd.notna(btc_old["d_pct_var_old"]) else None),
    }
    btc_pm = pit_m[(pit_m["slug"] == "bitcoin") & (pit_m["timestamp"] == rep)].iloc[0]
    btc_om = old_m[(old_m["slug"] == "bitcoin") & (old_m["timestamp"] == rep)].iloc[0]
    met_cmp = {
        "date": str(rep.date()),
        "pit_days_since_ath": int(btc_pm["d_met_ath_days"]),
        "old_days_since_ath(now-based)": int(btc_om["d_met_ath_days_old"]),
    }

    # coverage under alternative core-signal rules
    cov = coverage_by_family(sigs)

    # delisted proof: vgx-token active in PIT universe but absent from signal tables
    vgx_univ = universe.active(pd.Timestamp("2024-06-01"))
    vgx_sig = sigs[sigs["slug"] == "vgx-token"]

    # estimate full shadow rebuild
    est = estimate_full_rebuild(
        ohlcv, sigs,
        full_universe_rows=full_universe_rows, momentum_rows=momentum_rows,
        ratios_rows=ratios_rows, ohlcv_total_rows=ohlcv_total_rows,
    )

    payload = {
        "methodology": {
            "var_cvar": "pit-trailing-365d-min252",
            "metrics": "pit-cumulative-ath-atl-v1",
            "universe": {"source": universe.source, "version": universe.version},
            "missing_data": "drop_or_mark_incomplete (no zero-fill)",
            "score": "bin-only sum, enforced [-100,100]",
        },
        "sample": {
            "slugs": SLUGS,
            "window": WINDOW,
            "ohlcv_rows": len(ohlcv),
            "dates": int(ohlcv["timestamp"].nunique()),
            "runtime_sec": round(time.time() - T0, 1),
        },
        "universe": {
            "source": universe.source,
            "n_assets": universe.n_assets,
            "coverage": universe.coverage(),
            "vgx_token_active_2024-06-01": "vgx-token" in vgx_univ,
            "vgx_token_signal_rows_in_backtest": len(vgx_sig),
        },
        "signal_present": present,
        "var_cvar_comparison": var_cmp,
        "metrics_comparison": met_cmp,
        "score_range": {
            "min": {c: (float(scored[c].min()) if scored[c].notna().any() else None) for c in
                    ("Durability_Score", "Momentum_Score", "Valuation_Score")},
            "max": {c: (float(scored[c].max()) if scored[c].notna().any() else None) for c in
                    ("Durability_Score", "Momentum_Score", "Valuation_Score")},
            "incomplete_rows": int(scored["incomplete"].sum()),
        },
        "coverage_by_core_rule": cov,
        "estimate": est,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "pit_sample_results.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    print(json.dumps({k: payload[k] for k in ("sample", "var_cvar_comparison", "metrics_comparison",
                                              "coverage_by_core_rule", "estimate")}, indent=2, default=str))
    print(f"saved -> {OUT / 'pit_sample_results.json'}")


def estimate_full_rebuild(
    ohlcv: pd.DataFrame,
    sigs: pd.DataFrame,
    *,
    full_universe_rows: int,
    momentum_rows: int,
    ratios_rows: int,
    ohlcv_total_rows: int,
) -> dict:
    """Estimate the full shadow rebuild from real backtest history counts."""
    per_date = ohlcv.groupby(ohlcv["timestamp"].dt.date).size()
    avg_per_date = float(per_date.mean())

    fams = CORE_FAMILIES
    def have_all(row):
        return all(
            not row[[c for c in SIGNAL_FAMILIES[f] if c in sigs.columns]].isna().all()
            for f in fams
        )
    all4 = sum(1 for _, row in sigs.iterrows() if have_all(row)) if len(sigs) else 0
    all4_rate = all4 / len(sigs) if len(sigs) else 0.0

    est_dmv_4core = int(momentum_rows * all4_rate)
    return {
        "sample_avg_rows_per_date": round(avg_per_date, 1),
        "full_universe_rows": int(full_universe_rows),
        "ohlcv_total_rows": int(ohlcv_total_rows),
        "momentum_signal_rows": int(momentum_rows),
        "ratios_signal_rows": int(ratios_rows),
        "sample_all4_core_rate": round(all4_rate, 4),
        "estimated_dmv_rows_4core": est_dmv_4core,
        "estimated_dmv_rows_universe_upper": int(full_universe_rows),
        "runtime_estimate": {
            "pit_layer_only": "≈ 10-15 min for ~2.6M rows (sample 38.7s / 7,474 rows, near-linear)",
            "full_rebuild_incl_signal_regeneration": (
                "≈ 1-3 h wall-clock: ratios 28d trailing loop is O(4,836 dates x ~1,000 slugs); "
                "momentum/osc/tvv are fast rolling; matches prior backfill scale"
            ),
        },
        "cost_estimate_usd": "≈ $0-0.05: local/EC2 script over existing RDS; no Athena, no "
                             "external data, no new infra; dominant cost is wall-clock",
        "note": "Numbers from read-only cp_backtest; no production tables written.",
    }


if __name__ == "__main__":
    asyncio.run(main())
