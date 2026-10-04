import time

import numpy as np
import pandas as pd
import pytest

from deskmind.strategies.tas.loader import get_tick_size, load_ohlcv
from deskmind.strategies.tas.tas_strategy import (
    TASParams,
    _hma,
    _linreg,
    _rma,
    _wma,
    compute_indicators,
    resample,
    run_backtest,
)


def synth(n=26000, seed=1):
    rng = np.random.default_rng(seed)
    # trending regimes + noise so the strategy actually trades
    drift = np.repeat(rng.choice([-1, 1], n // 500 + 1) * 0.0004, 500)[:n]
    ret = drift + rng.normal(0, 0.006, n)
    close = 2000 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, n)))
    t = pd.date_range("2022-12-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open_time": t,
            "open": open_,
            "high": hi,
            "low": lo,
            "close": close,
            "volume": 1.0,
        }
    )


def test_tas():
    df1h = synth()
    df4h = resample(df1h, "4h")
    assert len(df4h) == len(df1h) // 4, len(df4h)

    # 1. indicator sanity
    x = np.arange(1, 30, dtype=float)
    assert abs(_wma(x, 3)[-1] - (27 * 1 + 28 * 2 + 29 * 3) / 6) < 1e-9
    assert abs(_linreg(x, 10)[-1] - 29) < 1e-9  # perfect line -> last value
    r = _rma(np.r_[np.full(13, 2.0), 2.0, 4.0], 14)
    assert abs(r[13] - 2.0) < 1e-12 and abs(r[14] - (4 / 14 + 2 * 13 / 14)) < 1e-12
    assert _hma(x, 9) is not None

    # 2. full run
    t0 = time.time()
    res = run_backtest(df4h, tick=0.01, start="2023-01-01", end="2024-12-31")
    print(f"runtime {time.time()-t0:.1f}s for {len(df4h)} bars")
    for k, v in res["stats"].items():
        print(f"  {k:24s} {v}")
    tr, ps = res["trades"], res["positions"]
    print(tr["exit_reason"].value_counts().to_dict())

    # 3. accounting: position pnl == sum of its trade rows
    assert abs(tr["pnl"].sum() - ps["net_pnl"].sum()) < 1e-6
    # no overlapping positions
    pp = ps.sort_values("entry_time")
    assert (pp["entry_time"].iloc[1:].values >= pp["exit_time"].iloc[:-1].values).all()
    # entries only inside window
    assert (ps["entry_time"] >= pd.Timestamp("2023-01-01", tz="UTC")).all()
    # stop on losing side, targets on winning side
    assert ((ps["side"] == "long") == (ps["init_stop"] < ps["plan_entry"])).all()

    # 4. NO LOOK-AHEAD: truncate data; every position closed before the cut
    # must be identical, and indicators before the cut must be identical
    cut = len(df4h) * 2 // 3
    res_t = run_backtest(df4h.iloc[:cut], tick=0.01, start="2023-01-01", end="2024-12-31")
    cut_time = df4h["open_time"].iloc[cut - 1]
    a = ps[ps["exit_time"] < cut_time].reset_index(drop=True)
    b = res_t["positions"][res_t["positions"]["exit_time"] < cut_time].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    ia = compute_indicators(df4h, TASParams()).iloc[:cut]
    ib = compute_indicators(df4h.iloc[:cut], TASParams())
    for col in ["trk", "tdir", "eq", "rsi", "rsi_ma", "adx", "score"]:
        np.testing.assert_allclose(ia[col].to_numpy(float), ib[col].to_numpy(float), equal_nan=True)
    print("ALL TESTS PASSED", len(a), "positions identical before cut")


def test_loader():
    tick = get_tick_size("ETHUSDT")
    assert tick == 0.01

    df = load_ohlcv("ETHUSDT", "4h")
    assert len(df) > 1000
    assert "open_time" in df.columns
    assert "close" in df.columns

    with pytest.raises(KeyError):
        get_tick_size("NON_EXISTENT_SYMBOL")
