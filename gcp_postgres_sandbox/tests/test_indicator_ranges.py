"""cpio-mcp#3: published Indicators and DMV scores stay inside their valid ranges.

No database. Expected values are worked by hand from the published definitions and
written as literals, so a formula bug can't cancel itself out:

  VWAP  sum(typical_price * volume) / sum(volume)
  CMF   sum(MFM * volume, n) / sum(volume, n),  MFM = ((C-L) - (H-C)) / (H-L)
        (Chaikin; a bar with H == L contributes 0, as in TA-Lib's AD)
  TSI   100 * EMA(EMA(dP, long), short) / EMA(EMA(|dP|, long), short)  (Blau)
"""

import logging

import numpy as np
import pandas as pd
import pytest

from dmv_ranges import compute_dmv_scores
from gcp_dmv_mom import (
    calculate_cmo, calculate_rsi, calculate_roc, calculate_smi, calculate_tsi,
    calculate_williams_r,
)
from gcp_dmv_osc import (
    calculate_adx, calculate_awesome_oscillator, calculate_cci, calculate_macd,
    calculate_trix, calculate_ultimate_oscillator,
)
from gcp_dmv_rat import (
    calculate_alpha, calculate_pct_change, calculate_risk_of_ruin,
    calculate_sharpe_ratio, calculate_sortino_ratio, calculate_win_rate,
)
from gcp_dmv_tvv import (
    calculate_atr, calculate_cmf, calculate_obv, calculate_vwap,
    generate_binary_signals,
)


def bars(rows, slug="bitcoin"):
    """rows of (high, low, close, volume) -> one coin's daily OHLCV frame."""
    df = pd.DataFrame(rows, columns=["high", "low", "close", "volume"], dtype=float)
    df.insert(0, "slug", slug)
    df.insert(1, "timestamp", pd.date_range("2026-01-01", periods=len(df), freq="D"))
    df["open"] = df["close"]
    return df


def closes(values, slug="bitcoin"):
    return bars([(c, c, c, 1.0) for c in values], slug)


def synthetic_market(seed):
    """220 days for three coins at very different price scales: a rally into a crash,
    plus the awkward bars real feeds contain (flat bars and zero-volume days)."""
    rng = np.random.default_rng(seed)
    frames = []
    for slug, base in [("bitcoin", 60000.0), ("ethereum", 1500.0), ("microcap", 0.0001)]:
        drift = np.r_[np.full(110, 0.01), np.full(60, -0.03), np.full(50, 0.002)]
        close = base * np.exp(np.cumsum(drift + 0.03 * rng.standard_normal(220)))
        high = close * (1 + np.abs(0.02 * rng.standard_normal(220)))
        low = close * (1 - np.abs(0.02 * rng.standard_normal(220)))
        volume = base * 1e6 * rng.lognormal(0, 1.5, 220)
        high[[40, 41]] = low[[40, 41]] = close[[40, 41]]
        volume[[90, 91]] = 0.0
        frames.append(bars(np.c_[high, low, close, volume], slug))
    return pd.concat(frames, ignore_index=True)


# ── VWAP ──────────────────────────────────────────────────────────────────
def test_vwap_matches_worked_example():
    # typical prices 10, 21, 30 on volumes 100, 300, 100
    df = calculate_vwap(bars([(12, 8, 10, 100), (23, 19, 21, 300), (32, 28, 30, 100)]))

    assert df["VWAP"].tolist() == pytest.approx([10.0, 18.25, 20.6])


def test_vwap_is_a_price_not_the_running_sum_of_prices():
    # The live bug: 110 days of ~75k BTC published as a VWAP of about 8.2M.
    df = calculate_vwap(bars([(76000, 74000, 75000, 1000 + i) for i in range(110)]))

    assert df["VWAP"].iloc[-1] == pytest.approx(75000.0)


def test_vwap_does_not_mix_coins():
    two_coins = pd.concat([
        bars([(12, 8, 10, 100), (12, 8, 10, 100)], "bitcoin"),
        bars([(3, 1, 2, 100), (5, 3, 4, 300)], "ethereum"),
    ], ignore_index=True)

    df = calculate_vwap(two_coins)

    assert df["VWAP"].tolist() == pytest.approx([10.0, 10.0, 2.0, 3.5])


# ── CMF ───────────────────────────────────────────────────────────────────
def test_cmf_matches_worked_example():
    # multipliers +1, 0, -1, +0.5 on volumes 100, 200, 100, 400; 3-bar window
    df = calculate_cmf(bars([
        (10, 0, 10, 100), (10, 0, 5, 200), (10, 0, 0, 100), (10, 0, 7.5, 400),
    ]), period=3)

    assert df["CMF"].tolist() == pytest.approx([1.0, 100 / 300, 0.0, 100 / 700])


def test_cmf_of_persistent_buying_stays_within_one():
    # The live bug: summing the cumulative A/D line gave BTC a CMF of 5.18.
    df = calculate_cmf(bars([(10, 0, 8, 1000) for _ in range(110)]))

    assert df["CMF"].iloc[-1] == pytest.approx(0.6)


def test_cmf_treats_a_flat_bar_as_no_money_flow():
    df = calculate_cmf(bars([(10, 0, 10, 100), (5, 5, 5, 100)]), period=2)

    assert df["CMF"].tolist() == pytest.approx([1.0, 0.5])


def test_cmf_clamps_and_warns_when_the_close_is_outside_the_bar(caplog):
    with caplog.at_level(logging.WARNING):
        df = calculate_cmf(bars([(10, 0, 25, 100)]))

    assert df["CMF"].tolist() == pytest.approx([1.0])
    assert "CMF money flow multiplier" in caplog.text


def test_cmf_warns_on_a_bar_with_high_below_low(caplog):
    with caplog.at_level(logging.WARNING):
        df = calculate_cmf(bars([(10, 0, 10, 100), (4, 6, 5, 100)]), period=2)

    assert df["CMF"].tolist() == pytest.approx([1.0, 0.5])
    assert "high below low" in caplog.text


def test_cmf_signal_follows_recent_flow_not_flow_since_the_window_opened():
    # 100 days of selling then a month of buying: recent money flow is positive even
    # though the running A/D line is still deeply negative.
    selling = [(10, 0, 2, 1000)] * 100
    buying = [(10, 0, 8, 1000)] * 30

    df = generate_binary_signals(_with_signal_inputs(calculate_cmf(bars(selling + buying))))

    assert df["m_tvv_cmf"].iloc[-1] == 1


def _with_signal_inputs(df):
    """generate_binary_signals also reads columns unrelated to CMF."""
    for col in ["m_tvv_obv_1d", "SMA9", "SMA18", "EMA9", "EMA18", "SMA21", "SMA108",
                "EMA21", "EMA108", "BB_Lower", "BB_Upper", "BB_Mid"]:
        df[col] = 0.0
    return df


# ── TSI ───────────────────────────────────────────────────────────────────
def test_tsi_matches_worked_example():
    # moves +2, -1, +3. long span 3 (alpha 1/2), short span 2 (alpha 2/3), seeded on
    # the first move:
    #   signed   EMA long 2, .5, 1.75   then short 2, 1, 1.5
    #   absolute EMA long 2, 1.5, 2.25  then short 2, 5/3, 37/18
    df = calculate_tsi(closes([100, 102, 101, 104]), short_period=2, long_period=3)

    assert df["m_mom_tsi"].tolist()[1:] == pytest.approx([100.0, 60.0, 100 * 27 / 37])


@pytest.mark.parametrize("step, expected", [(1, 100.0), (-1, -100.0)])
def test_tsi_of_a_one_way_market_is_the_extreme(step, expected):
    df = calculate_tsi(closes([1000 + step * i for i in range(60)]))

    assert df["m_mom_tsi"].iloc[-1] == pytest.approx(expected)


def test_tsi_stays_in_range_when_the_trend_reverses():
    # The live bug: the old denominator was a signed EMA, so it crossed zero on a
    # reversal and the ratio blew up (BTC 217).
    up = [1000 + 5 * i for i in range(40)]
    down = [up[-1] - 4 * i for i in range(1, 40)]

    tsi = calculate_tsi(closes(up + down))["m_mom_tsi"].dropna()

    assert tsi.between(-100, 100).all()


def test_tsi_does_not_inherit_state_from_the_previous_coin():
    second = closes([50, 51, 49, 52, 53, 51, 54], "solana")
    alone = calculate_tsi(second.copy())["m_mom_tsi"]

    # The previous coin's step must differ from solana's first move, or a leak is invisible.
    together = pd.concat([closes([9000 + 7 * i for i in range(30)]), second], ignore_index=True)
    after_another = calculate_tsi(together)
    after_another = after_another[after_another["slug"] == "solana"]["m_mom_tsi"]

    assert after_another.tolist()[1:] == pytest.approx(alone.tolist()[1:])


def test_tsi_of_a_flat_market_is_undefined_not_infinite():
    tsi = calculate_tsi(closes([100.0] * 30))["m_mom_tsi"]

    assert tsi.isna().all()


# ── every published Indicator, across synthetic markets ───────────────────
def published_indicators(df):
    df = df.sort_values(["slug", "timestamp"]).reset_index(drop=True)
    for step in (calculate_rsi, calculate_roc, calculate_williams_r, calculate_smi,
                 calculate_cmo, calculate_tsi, calculate_macd, calculate_cci,
                 calculate_adx, calculate_ultimate_oscillator,
                 calculate_awesome_oscillator, calculate_trix, calculate_obv,
                 calculate_atr, calculate_vwap, calculate_cmf):
        df = step(df)
    return df


BOUNDED = {
    "m_mom_rsi_9": (0, 100), "m_mom_rsi_18": (0, 100), "m_mom_rsi_27": (0, 100),
    "m_mom_williams_%": (-100, 0), "m_mom_smi": (-100, 100), "m_mom_cmo": (-100, 100),
    "m_mom_tsi": (-100, 100), "m_mom_roc": (-100, np.inf),
    "ADX": (0, 100), "UO": (0, 100), "CMF": (-1, 1), "ATR": (0, np.inf),
}
UNBOUNDED = ["MACD", "Signal", "CCI", "AO", "TRIX", "obv"]


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("column", sorted(BOUNDED))
def test_bounded_indicator_stays_in_range(seed, column):
    low, high = BOUNDED[column]

    values = published_indicators(synthetic_market(seed))[column].dropna()

    assert len(values) > 0
    assert values.between(low, high).all(), values[~values.between(low, high)].head()


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("column", UNBOUNDED)
def test_unbounded_indicator_is_never_infinite(seed, column):
    values = published_indicators(synthetic_market(seed))[column].dropna()

    assert len(values) > 0
    assert np.isfinite(values).all()


@pytest.mark.parametrize("seed", range(5))
def test_vwap_stays_inside_the_coin_traded_range(seed):
    df = published_indicators(synthetic_market(seed))

    for _, coin in df.groupby("slug"):
        assert coin["VWAP"].dropna().between(coin["low"].min(), coin["high"].max()).all()


def test_win_rate_is_a_fraction_of_days():
    group = closes([100] * 4)
    group["m_pct_1d"] = [0.01, -0.01, 0.02, 0.03]

    assert calculate_win_rate(group)["m_rat_win_rate"] == pytest.approx(0.75)


# ── ratios ────────────────────────────────────────────────────────────────
def with_returns(returns):
    group = closes([100.0] * len(returns))
    group["m_pct_1d"] = np.array(returns, dtype=float)
    return group


def returns_with_win_rate(wins, days):
    return [0.01] * wins + [-0.01] * (days - wins)


def ror(returns):
    return calculate_risk_of_ruin(with_returns(returns))["m_rat_ror"]


def test_risk_of_ruin_matches_gamblers_ruin_for_an_edge():
    # win rate 3/4, stake 4 units: ((1 - 3/4) / (3/4)) ** 4 = (1/3) ** 4
    assert ror(returns_with_win_rate(3, 4)) == pytest.approx(1 / 81)


@pytest.mark.parametrize("wins, days", [(2, 4), (1, 4), (0, 4), (45, 100)])
def test_risk_of_ruin_is_certain_without_an_edge(wins, days):
    assert ror(returns_with_win_rate(wins, days)) == 1.0


def test_risk_of_ruin_does_not_explode_on_a_long_losing_window():
    # The live bug: ((1 - w) / w) ** n is 3.9e9 at w = 0.45, n = 110.
    assert ror(returns_with_win_rate(45, 100) + [0.01, 0.01, -0.01, -0.01, -0.01] * 2) == 1.0


def test_risk_of_ruin_is_zero_when_every_day_wins():
    assert ror([0.01] * 5) == 0.0


def test_risk_of_ruin_is_undefined_without_data():
    # A newly listed coin has no returns yet; that isn't the same as certain ruin.
    assert np.isnan(ror([np.nan, np.nan]))


def sharpe(returns):
    return calculate_sharpe_ratio(with_returns(returns))["v_rat_sharpe"]


def sortino(returns):
    return calculate_sortino_ratio(with_returns(returns))["v_rat_sortino"]


def alpha(returns, benchmark_avg_return):
    return calculate_alpha(with_returns(returns), benchmark_avg_return)["m_rat_alpha"]


def test_sharpe_matches_worked_example():
    # mean 0.02, sample standard deviation 0.01
    assert sharpe([0.01, 0.02, 0.03]) == pytest.approx(2.0)


def test_sortino_matches_worked_example():
    # mean 0.0025; the two losing days -0.01 and -0.03 have sample deviation sqrt(2)/100
    assert sortino([0.02, -0.01, 0.03, -0.03]) == pytest.approx(0.25 / 2 ** 0.5)


def test_alpha_matches_worked_example():
    # mean 0.02 against a benchmark mean of 0.005
    assert alpha([0.01, 0.02, 0.03], 0.005) == pytest.approx(0.015)


@pytest.mark.parametrize("returns", [
    pytest.param([0.01] * 10, id="zero-variance"),
    pytest.param([0.0] * 10, id="flat"),
    pytest.param([0.01, 0.02, 0.03], id="zero-downside"),
    pytest.param([-0.02, -0.02, 0.01], id="identical-losses"),
    pytest.param([0.01], id="single-day"),
    pytest.param([np.nan, np.nan], id="no-data"),
])
def test_sharpe_sortino_alpha_are_finite_or_null(returns):
    for value in (sharpe(returns), sortino(returns), alpha(returns, 0.0)):
        assert not np.isinf(value)


def test_a_move_from_a_zero_close_is_undefined_not_infinite():
    # A pct change from a zero close divides by zero; it must not reach the ratios.
    df = calculate_pct_change(bars([(1, 1, 1, 1), (0, 0, 0, 1), (1, 1, 1, 1), (2, 2, 2, 1)]))

    assert not np.isinf(df["m_pct_1d"]).any()
    assert not np.isinf(calculate_alpha(df, 0.0)["m_rat_alpha"])


def window_ratios(df):
    """One row of ratios per coin over the trailing 28 days, as the daily job does."""
    df = calculate_pct_change(df.sort_values(["slug", "timestamp"]).reset_index(drop=True))
    df = df[df["timestamp"] >= df["timestamp"].max() - pd.Timedelta(days=28)]
    benchmark = df[df["slug"] == "bitcoin"]["m_pct_1d"].mean()
    return pd.DataFrame([
        pd.concat([calculate_alpha(g, benchmark), calculate_sharpe_ratio(g),
                   calculate_sortino_ratio(g), calculate_risk_of_ruin(g)])
        for _, g in df.groupby("slug")
    ])


@pytest.mark.parametrize("seed", range(5))
def test_ratios_stay_valid_across_synthetic_markets(seed):
    ratios = window_ratios(synthetic_market(seed))

    assert ratios["m_rat_ror"].between(0, 1).all()
    assert np.isfinite(ratios[["m_rat_alpha", "v_rat_sharpe", "v_rat_sortino"]]).all().all()


# ── DMV scores ────────────────────────────────────────────────────────────
DURABILITY_BINS = ["d_pct_cum_ret_signal", "d_tvv_sma9_18", "d_tvv_ema9_18",
                   "d_tvv_sma21_108", "d_tvv_ema21_108", "d_rat_beta_bin", "d_rat_pain_bin"]


def durability_row(*, bins=1.0, market_cap=1.0, coin_age=0.9375, ath_signal):
    row = {c: bins for c in DURABILITY_BINS}
    row.update(d_market_cap_signal=market_cap, d_met_coin_age_y_signal=coin_age,
               d_met_ath_month_signal=ath_signal)
    return row


def durability(row):
    return compute_dmv_scores(pd.DataFrame([row]))["Durability_Score"].iloc[0]


def test_durability_of_a_row_like_the_live_102_is_back_in_range():
    # 35 months since ATH -> signal 1.3 on its 0..2 scale. Summed raw that row is
    # (7 + 1 + 0.9375 + 1.3) / 10 = 102.375; scaled to 0.65 it is 95.875.
    assert durability(durability_row(ath_signal=1.3)) == pytest.approx(95.875)


def test_durability_cannot_pass_100_at_a_fresh_ath():
    assert durability(durability_row(coin_age=1.0, ath_signal=2.0)) == pytest.approx(100.0)


def test_durability_ath_signal_keeps_its_neutral_point():
    # 100 months since ATH is 0 on the column's own scale, and stays 0 in the score.
    row = durability_row(bins=0.0, market_cap=0.0, coin_age=0.0, ath_signal=0.0)

    assert durability(row) == pytest.approx(0.0)


def test_durability_clamps_and_warns_on_an_ath_older_than_the_scale(caplog):
    # 300 months since ATH is -4 raw, past -1 even after scaling.
    row = durability_row(bins=0.0, market_cap=0.0, coin_age=0.0, ath_signal=-4.0)

    with caplog.at_level(logging.WARNING):
        score = durability(row)

    assert score == pytest.approx(-10.0)
    assert "d_met_ath_month_signal" in caplog.text


@pytest.mark.parametrize("value, expected", [(1, 100.0), (-1, -100.0), (0, 0.0)])
def test_momentum_and_valuation_span_their_documented_range(value, expected):
    row = {"m_tvv_cmf": value, "m_mom_roc_bin": value, "v_rat_sharpe_bin": value}

    scores = compute_dmv_scores(pd.DataFrame([row])).iloc[0]

    assert scores["Momentum_Score"] == pytest.approx(expected)
    assert scores["Valuation_Score"] == pytest.approx(expected)


def test_a_non_bin_input_cannot_push_a_score_out_of_range(caplog):
    row = {"m_tvv_cmf": 5.18, "m_mom_roc_bin": 1, "v_rat_sharpe_bin": -7}

    with caplog.at_level(logging.WARNING):
        scores = compute_dmv_scores(pd.DataFrame([row])).iloc[0]

    assert scores["Momentum_Score"] == pytest.approx(100.0)
    assert scores["Valuation_Score"] == pytest.approx(-100.0)
    assert "m_tvv_cmf" in caplog.text
    assert "v_rat_sharpe_bin" in caplog.text


def test_scoring_leaves_the_input_signals_untouched():
    # FE_DMV_ALL and the bullish/bearish counts keep the ATH column's 0..2 scale.
    df = pd.DataFrame([durability_row(ath_signal=1.3)])

    compute_dmv_scores(df)

    assert df["d_met_ath_month_signal"].iloc[0] == 1.3
