# Phase 1 self-test: Supertrend on real bitcoin OHLCV pulled from dbcp on 2026-05-11.
# Standalone, no DB connection. Mirrors calculate_supertrend() from gcp_dmv_osc.py.
# Run: python scripts/test_phase1_supertrend.py
import pandas as pd
import numpy as np

# 60 days of BTC OHLCV (UTC date, high, low, close) from public.1K_coins_ohlcv on 2026-05-11
DATA = [
    ("2026-03-11", 70775.8287693097, 69230.1524009183, 70493.4585812722),
    ("2026-03-12", 73927.3249086237, 70410.7245334026, 70968.2676726541),
    ("2026-03-13", 71291.2049812356, 70339.5892291591, 71214.6282085293),
    ("2026-03-14", 73173.0060792367, 70882.4185598605, 72789.9172674119),
    ("2026-03-15", 74901.8576682633, 72300.6292224037, 74861.0836737225),
    ("2026-03-16", 75988.4005176101, 73444.2277093054, 73922.4740106774),
    ("2026-03-17", 74658.9789664408, 70503.8568323131, 71245.5785428252),
    ("2026-03-18", 71598.847301536,  68805.5247229895, 69912.787776857),
    ("2026-03-19", 71346.7983068919, 69398.8832141247, 70522.5839539436),
    ("2026-03-20", 71051.2704293867, 68602.9160106586, 68711.5271735613),
    ("2026-03-21", 69561.776956947,  67372.8737891374, 67845.2139798662),
    ("2026-03-22", 71782.2571259657, 67458.8453257102, 70914.8629915279),
    ("2026-03-23", 71371.3022191824, 68920.6944215992, 70517.8595482218),
    ("2026-03-24", 71985.7443400234, 70383.5970555375, 71309.8802324984),
    ("2026-03-25", 71410.3921155481, 68118.3491384535, 68791.6225102162),
    ("2026-03-26", 69117.5309967598, 65532.5691132184, 66338.3712323883),
    ("2026-03-27", 67232.8580364171, 65906.745704211,  66319.6974511113),
    ("2026-03-28", 67052.9542472237, 64971.706029593,  65954.9192574161),
    ("2026-03-29", 68087.2915838648, 65759.8018473819, 66691.4425240951),
    ("2026-03-30", 68495.2715329758, 65950.4397363917, 68233.3155611907),
    ("2026-03-31", 69230.3563256967, 67555.3580158907, 68078.5531589104),
    ("2026-04-01", 68633.1476005835, 65725.2568497465, 66888.5721018824),
    ("2026-04-02", 67296.2321437084, 66281.5413708288, 66931.099247444),
    ("2026-04-03", 67515.0175187123, 66769.6383776165, 67290.5181488701),
    ("2026-04-04", 69087.6560235339, 66610.6352570417, 68981.9007921881),
    ("2026-04-05", 70305.4196705928, 68347.0796078277, 68859.8263950077),
    ("2026-04-06", 72732.4295111994, 67740.509420096,  71940.7012947185),
    ("2026-04-07", 72825.1868213183, 70707.4679152796, 71123.3584710525),
    ("2026-04-08", 73107.267057311,  70486.3599945852, 71767.8251278026),
    ("2026-04-09", 73440.1136767158, 71434.8258341856, 72979.0503270674),
    ("2026-04-10", 73784.232471385,  72556.3353290528, 73054.2696888107),
    ("2026-04-11", 73154.0307462684, 70540.5683837987, 70753.4083340869),
    ("2026-04-12", 74896.3145800265, 70588.5228261962, 74484.6377504138),
    ("2026-04-13", 76061.7584050303, 73877.2037211508, 74181.6116510095),
    ("2026-04-14", 75409.2727483105, 73549.2039919501, 74805.075436482),
    ("2026-04-15", 75506.572001178,  73346.2632492192, 75152.1293763418),
    ("2026-04-16", 78320.6779221093, 74558.5996906887, 77126.8785598601),
    ("2026-04-17", 77416.7059323005, 75504.9423654053, 75726.2075561642),
    ("2026-04-18", 76243.0898668773, 73802.3849900433, 73856.3547881724),
    ("2026-04-19", 76575.3577810419, 73775.570201968,  75872.5247841284),
    ("2026-04-20", 76881.4801088633, 74852.6686288004, 76352.7769427102),
    ("2026-04-21", 79468.0022571901, 76159.578471048,  78203.0998567642),
    ("2026-04-22", 78676.9390812129, 77014.4525079851, 78268.9541080163),
    ("2026-04-23", 78554.0949333971, 77318.446411886,  77455.3156179439),
    ("2026-04-24", 77882.6422881316, 77184.6619747726, 77612.0178747246),
    ("2026-04-25", 78923.5629659828, 77334.8885586006, 78657.5394229309),
    ("2026-04-26", 79488.1719641042, 76481.3413991915, 77366.6234255091),
    ("2026-04-27", 77483.867714646,  75673.6023684618, 76350.6738565676),
    ("2026-04-28", 77884.9716567078, 74958.5697831686, 75776.1365543869),
    ("2026-04-29", 76611.4819515849, 75318.9846706173, 76304.3187968028),
    ("2026-04-30", 78894.9778763649, 76294.6927689367, 78179.0037206241),
    ("2026-05-01", 79119.7882971012, 78031.9640461038, 78657.2485661039),
    ("2026-05-02", 79402.360206911,  78073.0765663892, 78538.2233852504),
    ("2026-05-03", 80742.3592799869, 78217.9607239472, 79827.9060948068),
    ("2026-05-04", 81751.4505995564, 79787.5748087484, 80927.0516494072),
    ("2026-05-05", 82792.2106473945, 80751.0238113488, 81427.532969035),
    ("2026-05-06", 81684.9552092344, 79522.6593059377, 80009.9937755169),
    ("2026-05-07", 80447.2634023348, 79205.5173099239, 80186.7622844399),
    ("2026-05-08", 81030.065026448,  80119.9279796661, 80664.3676914766),
    ("2026-05-09", 82430.1738277522, 80274.1305766761, 82138.9313945505),
]

df = pd.DataFrame(DATA, columns=["timestamp", "high", "low", "close"])
df["slug"] = "bitcoin"
df["timestamp"] = pd.to_datetime(df["timestamp"])
df = df.sort_values(["slug", "timestamp"]).reset_index(drop=True)


# Mirror of calculate_supertrend() from gcp_dmv_osc.py
def calculate_supertrend(df, atr_period=10, multiplier=3.0):
    prev_close = df.groupby("slug")["close"].shift(1)
    tr_local = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs()
        )
    )
    atr_local = tr_local.groupby(df["slug"]).transform(
        lambda s: s.ewm(alpha=1.0 / atr_period, adjust=False, min_periods=atr_period).mean()
    )

    hl2 = (df["high"] + df["low"]) / 2.0
    basic_upper = hl2 + multiplier * atr_local
    basic_lower = hl2 - multiplier * atr_local

    st_line = np.full(len(df), np.nan)
    st_dir = np.full(len(df), np.nan)

    for slug, idx in df.groupby("slug", sort=False).indices.items():
        idx = list(idx)
        final_upper_prev = np.nan
        final_lower_prev = np.nan
        dir_prev = 1.0
        close_prev = np.nan
        first_valid = True

        for i in idx:
            bu = basic_upper.iloc[i]
            bl = basic_lower.iloc[i]
            close_i = df["close"].iloc[i]

            if np.isnan(bu) or np.isnan(bl):
                final_upper_prev = np.nan
                final_lower_prev = np.nan
                close_prev = close_i
                continue

            if np.isnan(final_upper_prev) or bu < final_upper_prev or close_prev > final_upper_prev:
                final_upper = bu
            else:
                final_upper = final_upper_prev

            if np.isnan(final_lower_prev) or bl > final_lower_prev or close_prev < final_lower_prev:
                final_lower = bl
            else:
                final_lower = final_lower_prev

            if first_valid:
                cur_dir = 1.0 if close_i >= hl2.iloc[i] else -1.0
                first_valid = False
            else:
                if close_i > final_upper_prev:
                    cur_dir = 1.0
                elif close_i < final_lower_prev:
                    cur_dir = -1.0
                else:
                    cur_dir = dir_prev

            st_line[i] = final_lower if cur_dir == 1.0 else final_upper
            st_dir[i] = cur_dir

            final_upper_prev = final_upper
            final_lower_prev = final_lower
            dir_prev = cur_dir
            close_prev = close_i

    df["Supertrend_Line"] = st_line
    df["Supertrend_Dir"] = st_dir
    return df


out = calculate_supertrend(df.copy())
last = out.tail(15)[["timestamp", "close", "Supertrend_Line", "Supertrend_Dir"]]
print("=== Bitcoin Supertrend (ATR period=10, multiplier=3.0) ===")
print(last.to_string(index=False))

latest = out.iloc[-1]
print()
print(f"Latest bar ({latest['timestamp'].date()}):")
print(f"  close           = {latest['close']:.2f}")
print(f"  Supertrend_Line = {latest['Supertrend_Line']:.2f}")
print(f"  Supertrend_Dir  = {int(latest['Supertrend_Dir'])}")

# Sanity: in an uptrend (dir == +1), line should sit below close. In downtrend, above.
if latest['Supertrend_Dir'] == 1.0:
    assert latest['Supertrend_Line'] < latest['close'], "Sanity fail: uptrend line should be below close"
    print("  SANITY OK: uptrend, line below close")
else:
    assert latest['Supertrend_Line'] > latest['close'], "Sanity fail: downtrend line should be above close"
    print("  SANITY OK: downtrend, line above close")

# m_osc_supertrend_bin
bin_val = int(latest['Supertrend_Dir'])
print(f"  m_osc_supertrend_bin = {bin_val}")
