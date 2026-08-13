"""CP-012 adapter unit tests (synthetic, offline)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from pit.cp012 import build_panel, evaluate, leakage_check, load_frozen_prices


def _dmv() -> pd.DataFrame:
    dates = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])
    rows = []
    for i, slug in enumerate(["aa", "bb"]):
        for d in dates:
            rows.append({"slug": slug, "date": d.date(),
                         "durability_score": 50.0 + i, "momentum_score": 10.0 + 20.0 * i,
                         "valuation_score": 20.0 + i, "d_pct_var": -0.02 - 0.01 * i,
                         "d_pct_cvar": -0.03 - 0.01 * i, "v_met_ath": 100.0 + i,
                         "v_met_atl": 50.0 + i, "d_met_ath_days": 5 + i,
                         "d_met_atl_days": 2 + i, "d_met_coin_age_d": 400 + i})
    return pd.DataFrame(rows)


def _prices() -> pd.DataFrame:
    return pd.DataFrame({
        "slug": ["aa"] * 4 + ["bb"] * 4,
        "date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 2),
        "close": [100.0, 110.0, 99.0, 105.0, 200.0, 190.0, 210.0, 205.0],
    })


def test_build_panel_forward_returns():
    panel = build_panel(_dmv(), _prices())
    assert "return_1d" in panel.columns and "return_30d" in panel.columns
    # return_1d at first date = close[t+1]/close[t]-1 for slug aa
    r0 = panel[(panel.slug == "aa") & (panel.date == pd.Timestamp("2024-01-01"))].iloc[0]
    assert abs(r0["return_1d"] - (110.0 / 100.0 - 1.0)) < 1e-9
    # last date has NaN forward return (shift -h)
    last = panel[(panel.slug == "bb") & (panel.date == pd.Timestamp("2024-01-04"))].iloc[0]
    assert pd.isna(last["return_1d"])


def test_leakage_check_passes():
    panel = build_panel(_dmv(), _prices())
    leak = leakage_check(panel)
    assert leak["duplicate_slug_date"] == 0
    assert leak["non_ascending_date_runs"] == 0
    assert leak["factors_pit"] and leak["returns_future_only"]


def test_evaluate_shape():
    panel = build_panel(_dmv(), _prices())
    panel["return_1d"] = panel["return_1d"].fillna(0.0)
    r = evaluate(panel, "momentum_score", 1, min_assets=2)
    assert r["factor"] == "momentum_score" and r["horizon"] == 1
    assert r["n_pairs"] >= 0
    assert "ic_pearson_mean" in r and "ic_spearman_mean" in r


def test_evaluate_ignores_nonfinite_returns():
    panel = build_panel(_dmv(), _prices())
    panel["return_1d"] = panel["return_1d"].fillna(0.0)
    panel.loc[0, "return_1d"] = float("inf")  # zero-close artifact
    r = evaluate(panel, "momentum_score", 1, min_assets=2)
    assert "ic_spearman_mean" in r
    assert r["ic_spearman_mean"] is not None


def test_save_report_clean_json(tmp_path):
    from pathlib import Path

    from pit.cp012 import save_report
    payload = {"x": float("nan"), "y": float("inf"), "b": True, "l": [1, float("nan")]}
    p = Path(tmp_path) / "r.json"
    save_report(payload, p)
    import json

    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["x"] is None and d["y"] is None and d["b"] is True and d["l"][1] is None


def test_evaluate_nearconstant_factor_no_nan_spearman():
    panel = build_panel(_dmv(), _prices())
    panel["return_1d"] = panel["return_1d"].fillna(0.0)
    # force all durability scores to differ by ~1e-15 (std>0 but ranks tie)
    base = 50.0
    panel["durability_score"] = base + np.arange(len(panel)) * 1e-15
    r = evaluate(panel, "durability_score", 1, min_assets=2)
    assert r.get("ic_spearman_mean") is None or r.get("ic_spearman_mean") == r.get("ic_spearman_mean")


def test_load_frozen_prices_last_bar():
    df = pd.DataFrame({
        "slug": ["aa"] * 3,
        "timestamp": pd.to_datetime(["2024-01-01 00:00", "2024-01-01 12:00", "2024-01-02 00:00"]),
        "close": [1.0, 2.0, 3.0],
    })
    df.to_parquet("/tmp/cp012_px.parquet", index=False)
    px = load_frozen_prices("/tmp/cp012_px.parquet")
    row = px[(px.slug == "aa") & (px.date == pd.Timestamp("2024-01-01"))]
    assert float(row["close"].iloc[0]) == 2.0  # last bar of the day
