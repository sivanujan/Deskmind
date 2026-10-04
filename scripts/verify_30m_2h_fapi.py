"""Verify 20 random 30m and 20 random 2h candles on 3 coins against Binance fapi."""

import json
import random
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

PARQUET_DIR = Path("data/parquet")
TEST_COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def load_candles(symbol: str, tf: str) -> pd.DataFrame:
    p = PARQUET_DIR / symbol / "klines" / tf
    files = sorted(p.rglob("*.parquet"))
    tables = [pq.read_table(f) for f in files]
    import pyarrow as pa
    t = pa.concat_tables(tables)
    df = t.to_pandas().sort_values("open_time").reset_index(drop=True)
    return df


def fetch_fapi_kline(symbol: str, tf: str, open_time_ms: int) -> dict:
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={tf}&startTime={open_time_ms}&limit=1"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if not data:
                    raise ValueError(f"No kline returned for {symbol} {tf} at {open_time_ms}")
                row = data[0]
                return {
                    "open_time_ms": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1.0)


def verify_coin_tf(symbol: str, tf: str, n_samples: int = 20, seed: int = 42) -> list[dict]:
    df = load_candles(symbol, tf)
    rng = random.Random(seed)
    # Pick random indices avoiding the very last candle which might be live
    valid_indices = list(range(100, len(df) - 10))
    sample_indices = rng.sample(valid_indices, n_samples)

    results = []
    print(f"\n--- Verifying {symbol} [{tf}] ({n_samples} samples) ---", flush=True)
    for idx in sample_indices:
        local_row = df.iloc[idx]
        t_ms = int(local_row["open_time"].timestamp() * 1000)
        t_str = str(local_row["open_time"])

        remote = fetch_fapi_kline(symbol, tf, t_ms)

        # Check precision
        diff_open = abs(local_row["open"] - remote["open"])
        diff_high = abs(local_row["high"] - remote["high"])
        diff_low = abs(local_row["low"] - remote["low"])
        diff_close = abs(local_row["close"] - remote["close"])

        max_diff = max(diff_open, diff_high, diff_low, diff_close)
        is_match = max_diff < 1e-6

        if not is_match:
            print(f"\nMISMATCH at {t_str}: local(O={local_row['open']}, H={local_row['high']}, L={local_row['low']}, C={local_row['close']}) vs fapi(O={remote['open']}, H={remote['high']}, L={remote['low']}, C={remote['close']})", flush=True)
        assert is_match, f"Mismatch on {symbol} {tf} at {t_str}"

        results.append({
            "symbol": symbol,
            "tf": tf,
            "open_time": t_str,
            "local_o": local_row["open"],
            "remote_o": remote["open"],
            "local_h": local_row["high"],
            "remote_h": remote["high"],
            "local_l": local_row["low"],
            "remote_l": remote["low"],
            "local_c": local_row["close"],
            "remote_c": remote["close"],
            "max_diff": max_diff,
        })
        print(".", end="", flush=True)
        time.sleep(0.02)

    print(f"\nPASSED: All {n_samples} {tf} candles for {symbol} match Binance fapi exactly!", flush=True)
    return results


def main():
    all_results = []
    for coin in TEST_COINS:
        res_30m = verify_coin_tf(coin, "30m", n_samples=20, seed=42)
        res_2h = verify_coin_tf(coin, "2h", n_samples=20, seed=43)
        all_results.extend(res_30m)
        all_results.extend(res_2h)

    print("\n=======================================================")
    print(f"STEP 1a VERIFICATION SUMMARY: {len(all_results)} / {len(all_results)} CANDLES MATCH EXACTLY")
    print("Coins tested: BTCUSDT, ETHUSDT, SOLUSDT")
    print("30m: 60/60 matched Binance fapi (0 mismatches)")
    print("2h:  60/60 matched Binance fapi (0 mismatches)")
    print("=======================================================")


if __name__ == "__main__":
    main()
