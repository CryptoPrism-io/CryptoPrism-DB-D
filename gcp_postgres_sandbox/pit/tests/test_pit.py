"""CP-011 Phase B — tests for the PIT-safe DMV layer.

Self-contained (no DB): synthetic deterministic daily OHLCV for 3 assets
(bitcoin, ethereum, plus a *delisted* asset whose series ends early) drives all
tests, including the future-row mutation test.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pit.var_cvar import calculate_var_cvar_pit, var_cvar_old_fullsample
from pit.metrics import calculate_metrics_pit
from pit.universe import PITUniverse
from pit.scores import compute_scores, validate_bin_columns
from pit.policy import CORE_FAMILIES, SIGNAL_FAMILIES, core_bin_columns


# ── synthetic deterministic data ──────────────────────────────────────────
def make_ohlcv(n_days: int = 700, seed: int = 7) -> pd.DataFrame:
    """Daily OHLCV for bitcoin + ethereum (full span) and 'deadcoin' (delisted
    at day 400). Deterministic via seeded RNG."""
    rng = np.random.default_rng(seed)
    rows = []
    for slug, start, base, vol in [
        ("bitcoin", 0, 20000.0, 2.0),
        ("ethereum", 0, 1500.0, 3.0),
        ("deadcoin", 0, 1.0, 5.0),
    ]:
        span = n_days if slug != "deadcoin" else 400
        for i in range(span):
            date = pd.Timestamp("2024-01-01") + pd.Timedelta(days=i)
            close = base * (1 + 0.002 * rng.standard_normal())
            high = close * (1 + abs(0.01 * rng.standard_normal()))
            low = close * (1 - abs(0.01 * rng.standard_normal()))
            rows.append({
                "slug": slug, "timestamp": date, "open": low,
                "high": high, "low": low, "close": close,
                "volume": vol * base * (1 + 0.1 * rng.standard_normal()),
            })
    df = pd.DataFrame(rows)
    df = df.sort_values(["slug", "timestamp"]).reset_index(drop=True)
    df["m_pct_1d"] = df.groupby("slug")["close"].pct_change()
    return df


def make_signal_bins(df: pd.DataFrame, families=None) -> pd.DataFrame:
    """Deterministic -1/0/1 bins for the declared signal families."""
    families = families or SIGNAL_FAMILIES
    out = df.copy()
    idx = out.index.to_numpy()
    for fam, cols in families.items():
        for c in cols:
            # pattern depends on slug + position so it is deterministic
            src = (idx + out["slug"].astype("category").cat.codes.to_numpy() * 13) % 3
            out[c] = (src - 1)  # -> -1,0,1
    return out


def make_returns(df: pd.DataFrame) -> pd.DataFrame:
    """Returns frame with a deterministic wide tail for VaR (as if real)."""
    out = df.copy()
    rng = np.random.default_rng(21)
    base = 0.0005 * rng.standard_normal(len(out))
    # inject some fat tails deterministically
    tail_mask = (np.arange(len(out)) % 37) == 0
    base[tail_mask] = -0.06 - 0.02 * (np.arange(len(out))[tail_mask] % 5)
    out["m_pct_1d"] = base
    return out


# ── 1. test_var_cvar_pit ─────────────────────────────────────────────────
def test_var_cvar_pit_only_uses_past_rows():
    df = make_returns(make_ohlcv(n_days=700))
    res = calculate_var_cvar_pit(df, window_days=365, min_obs=252)
    # property: for sampled rows, d_pct_var equals trailing quantile over rows <= t
    rng = np.random.default_rng(0)
    for slug in ("bitcoin", "ethereum", "deadcoin"):
        g = res[res["slug"] == slug].sort_values("timestamp").reset_index(drop=True)
        probes = rng.choice(len(g) - 1, size=25, replace=False)
        dates = g["timestamp"].to_numpy()
        rets = g["m_pct_1d"].to_numpy(dtype=float)
        for i in probes:
            t = dates[i]
            window_mask = (dates <= t) & (dates >= t - np.timedelta64(365, "D"))
            win = rets[window_mask]
            win = win[~np.isnan(win)]
            if win.size < 252:
                assert np.isnan(g.at[i, "d_pct_var"])
            else:
                expected = np.quantile(win, 0.05)
                assert g.at[i, "d_pct_var"] == pytest.approx(float(expected), abs=1e-12)


def test_var_cvar_pit_null_before_min_history():
    df = make_returns(make_ohlcv(n_days=400))
    res = calculate_var_cvar_pit(df, window_days=365, min_obs=252)
    first = res[res["slug"] == "bitcoin"].sort_values("timestamp").iloc[0]
    assert np.isnan(first["d_pct_var"]) and np.isnan(first["d_pct_cvar"])


# ── 2. test_universe_pit (corrected) ─────────────────────────────────────
def test_universe_pit_includes_delisted_on_active_dates():
    df = make_ohlcv(n_days=700)  # deadcoin ends at day 400
    uni = PITUniverse.from_ohlcv(df)
    assert uni.source == "PIT_APPROX"
    # active during deadcoin's life
    d300 = pd.Timestamp("2024-01-01") + pd.Timedelta(days=300)
    assert "deadcoin" in uni.active(d300)
    # NOT active after delisting
    d500 = pd.Timestamp("2024-01-01") + pd.Timedelta(days=500)
    assert "deadcoin" not in uni.active(d500)
    # bitcoin active across the whole span
    assert "bitcoin" in uni.active(d500)
    # every row's (slug,date) must satisfy universe membership at that date
    for slug, g in df.groupby("slug"):
        for _, row in g.iterrows():
            assert slug in uni.active(row["timestamp"]), (slug, row["timestamp"])


# ── 3. test_score_range ─────────────────────────────────────────────────
def test_score_range_within_bounds():
    df = make_signal_bins(make_ohlcv(n_days=400))
    core = core_bin_columns()
    m_cols = [c for c in core if c.startswith("m_")]
    d_cols = [c for c in core if c.startswith("d_")]
    v_cols = [c for c in core if c.startswith("v_")]
    res = compute_scores(df, durability_cols=d_cols, momentum_cols=m_cols, valuation_cols=v_cols)
    for c in ("Durability_Score", "Momentum_Score", "Valuation_Score"):
        vals = res[c].dropna()
        assert ((vals >= -100) & (vals <= 100)).all(), c


def test_score_range_rejects_non_bin_columns():
    df = make_signal_bins(make_ohlcv(n_days=100))
    # inject a leaked non-bin value (the Phase-A contamination)
    df["m_mom_roc_bin"] = 101.97
    with pytest.raises(ValueError):
        validate_bin_columns(df, ["m_mom_roc_bin"])


def test_incomplete_rows_are_nan_not_zero():
    df = make_signal_bins(make_ohlcv(n_days=400))
    # blank out one whole core family -> every row incomplete for that family
    df.loc[df.index % 2 == 0, "m_mom_roc_bin"] = np.nan
    core = core_bin_columns()
    m_cols = [c for c in core if c.startswith("m_")]
    res = compute_scores(df, durability_cols=[c for c in core if c.startswith("d_")],
                         momentum_cols=m_cols, valuation_cols=[c for c in core if c.startswith("v_")])
    nan_rows = res[res.index % 2 == 0]
    assert nan_rows["Momentum_Score"].isna().all()
    assert nan_rows["incomplete"].all()


# ── 4. test_neutral_fill ────────────────────────────────────────────────
def test_neutral_fill_never_applied():
    # VaR/CVaR NULL before min history stays NULL (bitcoin has only 100 rows < 252)
    df = make_returns(make_ohlcv(n_days=100))
    res = calculate_var_cvar_pit(df, window_days=365, min_obs=252)
    btc = res[res["slug"] == "bitcoin"]
    assert btc["d_pct_var"].isna().all()
    assert btc["d_pct_cvar"].isna().all()
    # incomplete scores stay NaN, not 0 (blank a durability bin column)
    df2 = make_signal_bins(make_ohlcv(n_days=100))
    df2.loc[df2.index % 3 == 0, "d_tvv_sma9_18"] = np.nan
    core = core_bin_columns()
    res2 = compute_scores(df2, durability_cols=[c for c in core if c.startswith("d_")],
                          momentum_cols=[c for c in core if c.startswith("m_")],
                          valuation_cols=[c for c in core if c.startswith("v_")])
    assert (res2.loc[df2.index % 3 == 0, "Durability_Score"].isna().all())
    assert not res2["Durability_Score"].isna().all()  # other rows have scores


# ── 5. test_timestamp_alignment ─────────────────────────────────────────
def test_timestamp_alignment_incomplete_not_misaligned():
    # Simulate 4 core signal tables with slightly different date sets (warmup)
    df = make_signal_bins(make_ohlcv(n_days=500))
    core = core_bin_columns()
    # zero out first 20 rows of the oscillators family (warmup drop)
    fam = CORE_FAMILIES[0]
    cols = SIGNAL_FAMILIES[fam]
    df.loc[df["timestamp"] < pd.Timestamp("2024-01-21"), cols] = np.nan
    # the dates present in EVERY core family = intersection
    m_cols = [c for c in core if c.startswith("m_")]
    d_cols = [c for c in core if c.startswith("d_")]
    v_cols = [c for c in core if c.startswith("v_")]
    res = compute_scores(df, durability_cols=d_cols, momentum_cols=m_cols, valuation_cols=v_cols)
    # rows before 2024-01-21 are incomplete (oscillators missing), not dropped/misaligned
    early = res[res["timestamp"] < pd.Timestamp("2024-01-21")]
    assert early["incomplete"].all()
    late = res[res["timestamp"] >= pd.Timestamp("2024-01-21")]
    assert not late["incomplete"].all()


# ── 6. test_live_backfill_comparability ─────────────────────────────────
def test_live_backfill_share_policy():
    from pit.policy import CORE_FAMILIES as CF, OPTIONAL_FAMILIES as OF, SIGNAL_FAMILIES as SF
    # both paths must call the same compute_scores with the same declared families
    assert set(CF).isdisjoint(set(OF))
    for fam in CF + OF:
        assert set(SF[fam]), f"family {fam} has no columns"
    # the score function is the single shared entry point (import equality)
    from pit.scores import compute_scores as cs
    assert cs is compute_scores


# ── 7. test_determinism ─────────────────────────────────────────────────
def test_determinism_same_input_same_output():
    df = make_returns(make_ohlcv(n_days=600))
    a = calculate_var_cvar_pit(df)
    b = calculate_var_cvar_pit(df)
    pd.testing.assert_frame_equal(a, b)
    m1 = calculate_metrics_pit(make_ohlcv(n_days=600))
    m2 = calculate_metrics_pit(make_ohlcv(n_days=600))
    pd.testing.assert_frame_equal(m1, m2)


def test_coin_age_pit_row_date_minus_first_seen():
    df = make_ohlcv(n_days=400)
    m = calculate_metrics_pit(df)
    btc = m[m["slug"] == "bitcoin"].sort_values("timestamp")
    first = btc["timestamp"].iloc[0]
    assert btc["d_met_coin_age_d"].iloc[0] == 0
    assert btc["d_met_coin_age_d"].iloc[100] == (btc["timestamp"].iloc[100] - first).days
    assert btc["d_met_coin_age_d"].gt(0).any()  # grows over time, no now()/negative


def test_duplicate_dates_deterministic_and_in_window():
    df = make_returns(make_ohlcv(n_days=400))
    dup = pd.concat([df, df.iloc[5:8]], ignore_index=True)  # duplicate 3 rows
    res = calculate_var_cvar_pit(dup)
    res2 = calculate_var_cvar_pit(dup)
    pd.testing.assert_frame_equal(res, res2)
    assert len(res) == len(dup)


# ── 8. future-row mutation test ─────────────────────────────────────────
def test_future_row_mutation_does_not_change_output_at_d():
    """Changing any input after date d must not alter DMV output at d."""
    df = make_returns(make_ohlcv(n_days=600))
    core = core_bin_columns()
    df = make_signal_bins(df)

    def _layer(frame):
        v = calculate_var_cvar_pit(frame[["slug", "timestamp", "m_pct_1d"]], window_days=365, min_obs=252)
        m = calculate_metrics_pit(frame[["slug", "timestamp", "high", "low"]])
        sc = compute_scores(frame,
                            durability_cols=[c for c in core if c.startswith("d_")],
                            momentum_cols=[c for c in core if c.startswith("m_")],
                            valuation_cols=[c for c in core if c.startswith("v_")])
        v = v.set_index(["slug", "timestamp"])
        m = m.set_index(["slug", "timestamp"])
        sc = sc[["slug", "timestamp", "Durability_Score", "Momentum_Score", "Valuation_Score", "incomplete"]].set_index(["slug", "timestamp"])
        return v.join(m, how="left").join(sc, how="left")

    base = _layer(df)
    d0 = pd.Timestamp("2025-06-01")
    row0 = base.loc[("bitcoin", d0)].copy()

    # mutate a FUTURE return massively
    future = df.copy()
    mask = (future["slug"] == "bitcoin") & (future["timestamp"] > d0)
    future.loc[mask, "m_pct_1d"] = -0.9
    future.loc[mask, "close"] = future.loc[mask, "close"] * 0.1
    future.loc[mask, "high"] = future.loc[mask, "high"] * 10
    future.loc[mask, "low"] = future.loc[mask, "low"] * 0.01
    future.loc[mask, "m_mom_roc_bin"] = -1

    mutated = _layer(future)
    row_m = mutated.loc[("bitcoin", d0)]
    pd.testing.assert_series_equal(row0, row_m, check_names=False)

    # ALSO: the OLD full-sample var DOES change when future data changes (proof the test catches the legacy leak)
    old_a = var_cvar_old_fullsample(df)
    old_b = var_cvar_old_fullsample(future)
    oa = old_a[(old_a["slug"] == "bitcoin") & (old_a["timestamp"] == d0)]["d_pct_var_old"].iloc[0]
    ob = old_b[(old_b["slug"] == "bitcoin") & (old_b["timestamp"] == d0)]["d_pct_var_old"].iloc[0]
    assert oa != ob  # legacy approach leaks future data
