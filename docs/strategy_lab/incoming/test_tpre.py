import time, numpy as np, pandas as pd
from tpre_strategy import run_backtest, compute_indicators, TPREParams, ema, rsi, barssince

def synth(n=8000, seed=3, freq="4h"):
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-1, 1], n // 400 + 1) * 0.002, 400)[:n]
    ret = drift + rng.normal(0, 0.015, n)
    c = 2000 * np.exp(np.cumsum(ret))
    o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.001, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, n)))
    t = pd.date_range("2020-06-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open_time": t, "open": o, "high": h, "low": l, "close": c})

# indicator unit checks
x = np.arange(1, 50, dtype=float)
e = ema(x, 5); assert abs(e[4] - 3.0) < 1e-12 and abs(e[5] - (6*2/6 + 3*4/6)) < 1e-12
assert rsi(x, 5)[-1] == 100.0
b = barssince(np.array([False, True, False, False, True, False]))
assert np.isnan(b[0]) and list(b[1:]) == [0, 1, 2, 0, 1]

df = synth()
p = TPREParams()
t0 = time.time()
res = run_backtest(df, tick=0.01, start="2022-01-01", end="2024-12-31")
print(f"runtime {time.time()-t0:.2f}s, {len(df)} bars")
for k, v in res["stats"].items(): print(f"  {k:18s} {v}")
tr = res["trades"]
print(tr["exit_reason"].value_counts().to_dict())

# 1 position at a time, entries inside window
assert (tr["entry_time"].iloc[1:].values >= tr["exit_time"].iloc[:-1].values).all()
assert (tr["entry_time"] >= pd.Timestamp("2022-01-01", tz="UTC")).all()
# stop on losing side
assert ((tr["side"] == "long") == (tr["init_stop"] < tr["plan_entry"])).all()
# risk sizing: 0.5% of equity at risk unless capped at 1x notional
eq_at = 10000 + tr["pnl"].cumsum().shift(fill_value=0)
risk_pct = tr["risk_usd"] / eq_at * 100
notional_x = tr["qty"] * tr["plan_entry"] / eq_at
assert (((risk_pct - 0.5).abs() < 1e-6) | ((notional_x - 1.0).abs() < 1e-6)).all()
# worst loss cannot be much beyond -1R except gaps
print("min R", round(tr["r_multiple"].min(), 3), " max R", round(tr["r_multiple"].max(), 3))
# accounting
assert abs(res["equity"].iloc[-1] - (10000 + tr["pnl"].sum())) < 1e-6

# no look-ahead: truncate
cut = len(df) * 3 // 4
rt = run_backtest(df.iloc[:cut], tick=0.01, start="2022-01-01", end="2024-12-31")
ct = df["open_time"].iloc[cut - 1]
a = tr[tr["exit_time"] < ct].reset_index(drop=True)
bb = rt["trades"][rt["trades"]["exit_time"] < ct].reset_index(drop=True)
pd.testing.assert_frame_equal(a, bb)
ia = compute_indicators(df, p, 0.01).iloc[:cut]
ib = compute_indicators(df.iloc[:cut], p, 0.01)
for col in ["ema_f", "ema_s", "rsi_p", "atr", "since_dip", "since_pop", "dip_low", "pop_high"]:
    np.testing.assert_allclose(ia[col].to_numpy(float), ib[col].to_numpy(float), equal_nan=True)
# determinism
r2 = run_backtest(df, tick=0.01, start="2022-01-01", end="2024-12-31")
pd.testing.assert_frame_equal(tr, r2["trades"])
print("ALL TESTS PASSED;", len(a), "trades identical before cut")
