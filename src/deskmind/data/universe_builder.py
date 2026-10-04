"""Step 2: Universe builder and filter resolver for Binance USDT-M perpetuals (2022-12-01 to 2025-12-31)."""

import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

DATA_DIR = Path("data")
CACHE_DIR = DATA_DIR / "cache"
META_DIR = DATA_DIR / "meta"
REPORTS_DIR = Path("reports/research")

TARGET_START_MS = int(datetime(2022, 12, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
TARGET_END_MS = int(datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)

EXCLUDED_NON_CRYPTO = {
    "TSLA", "AMZN", "GOOGL", "SPX", "XAU", "XAG", "BTCDOM", "DEFI", "FOOTBALL", "BLUEBIRD",
    "AAPL", "NVDA", "MSFT", "META", "COIN", "BABA", "OIL", "NDX", "DJI"
}

def fetch_fapi_exchange_info() -> dict[str, dict]:
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    
    symbols_map = {}
    for s in data["symbols"]:
        sym = s["symbol"]
        symbols_map[sym] = s
    return symbols_map

def get_archived_delisted_filters() -> dict[str, dict]:
    """Known verified filters from Binance launch/delisting announcements for delisted perpetuals."""
    return {
        "AERGOUSDT": {"tick_size": Decimal("0.0001"), "step_size": Decimal("1"), "min_qty": Decimal("1")},
        "ANTUSDT": {"tick_size": Decimal("0.001"), "step_size": Decimal("0.1"), "min_qty": Decimal("0.1")},
        "AUDIOUSDT": {"tick_size": Decimal("0.0001"), "step_size": Decimal("1"), "min_qty": Decimal("1")},
        "BDXNUSDT": {"tick_size": Decimal("0.0001"), "step_size": Decimal("1"), "min_qty": Decimal("1")},
        "BTCSTUSDT": {"tick_size": Decimal("0.001"), "step_size": Decimal("0.1"), "min_qty": Decimal("0.1")},
        "BTSUSDT": {"tick_size": Decimal("0.00001"), "step_size": Decimal("1"), "min_qty": Decimal("1")},
        "COCOSUSDT": {"tick_size": Decimal("0.0001"), "step_size": Decimal("1"), "min_qty": Decimal("1")},
        "EOSUSDT": {"tick_size": Decimal("0.001"), "step_size": Decimal("0.1"), "min_qty": Decimal("0.1")},
        "FRONTUSDT": {"tick_size": Decimal("0.0001"), "step_size": Decimal("1"), "min_qty": Decimal("1")},
        "GALUSDT": {"tick_size": Decimal("0.001"), "step_size": Decimal("0.1"), "min_qty": Decimal("0.1")},
        "HNTUSDT": {"tick_size": Decimal("0.001"), "step_size": Decimal("0.1"), "min_qty": Decimal("0.1")},
        "MATICUSDT": {"tick_size": Decimal("0.0001"), "step_size": Decimal("0.1"), "min_qty": Decimal("0.1")},
        "MBLUSDT": {"tick_size": Decimal("0.00001"), "step_size": Decimal("100"), "min_qty": Decimal("100")},
        "RNDRUSDT": {"tick_size": Decimal("0.001"), "step_size": Decimal("0.1"), "min_qty": Decimal("0.1")},
        "SRMUSDT": {"tick_size": Decimal("0.0001"), "step_size": Decimal("1"), "min_qty": Decimal("1")},
        "SXPUSDT": {"tick_size": Decimal("0.0001"), "step_size": Decimal("0.1"), "min_qty": Decimal("0.1")},
        "TOMOUSDT": {"tick_size": Decimal("0.0001"), "step_size": Decimal("1"), "min_qty": Decimal("1")},
    }

def build_universe():
    print("Building full Binance USDT-M perpetual symbol list for 2022-12-01 -> 2025-12-31...")
    META_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    
    fapi_symbols = fetch_fapi_exchange_info()
    archived_filters = get_archived_delisted_filters()
    
    # 1. Identify TRADIFI assets to exclude
    tradifi_bases = set()
    for s, info in fapi_symbols.items():
        if info.get("contractType") == "TRADIFI_PERPETUAL":
            tradifi_bases.add(info.get("baseAsset"))
    all_excluded_bases = EXCLUDED_NON_CRYPTO | tradifi_bases
    
    # 2. Cache symbols
    cache_files = list(CACHE_DIR.glob("*.csv"))
    cache_coins = set()
    for f in cache_files:
        m = re.match(r"ohlcv_([A-Za-z0-9\-_]+)_USDT_USDT.*\.csv", f.name)
        if m:
            c = m.group(1)
            if c not in all_excluded_bases and not re.search(r"_[0-9]{6}$", c) and "-" not in c:
                cache_coins.add(c)
                
    # 3. Vision monthly symbols
    with open(REPORTS_DIR / "binance_vision_all_usdt_symbols.json") as f:
        vision_usdt = json.load(f)
        
    candidate_symbols = set()
    for c in cache_coins:
        candidate_symbols.add(f"{c}USDT")
        
    for s in vision_usdt:
        if not s.endswith("USDT"): continue
        coin = s[:-4]
        if coin in all_excluded_bases or re.search(r"_[0-9]{6}$", s) or "-" in s:
            continue
        candidate_symbols.add(s)
        
    for s, info in fapi_symbols.items():
        if not s.endswith("USDT"): continue
        if info.get("contractType") != "PERPETUAL": continue
        coin = info.get("baseAsset", s[:-4])
        if coin in all_excluded_bases or re.search(r"_[0-9]{6}$", s) or "-" in s:
            continue
        candidate_symbols.add(s)
        
    print(f"Total candidate crypto perpetual symbols: {len(candidate_symbols)}")
    
    # Exclude symbols listed after 2025-12-31
    # Exclude symbols delisted before 2022-12-01
    pre_2022_delisted = {
        "1000BTTCUSDT", "AKROUSDT", "ANCUSDT", "BTTUSDT", "BZRXUSDT", "DODOUSDT",
        "DOTECOUSDT", "KEEPUSDT", "LENDUSDT", "LUNAUSDT", "NUUSDT", "YFIIUSDT"
    }
    
    universe = []
    symbol_filters = {}
    
    for sym in sorted(candidate_symbols):
        if sym in pre_2022_delisted:
            continue
            
        fapi_meta = fapi_symbols.get(sym)
        onboard_ms = fapi_meta.get("onboardDate") if fapi_meta else None
        
        # Listed after 2025-12-31 -> exclude
        if onboard_ms and onboard_ms > TARGET_END_MS:
            continue
            
        coin = sym[:-4]
        in_cache = (coin in cache_coins)
        
        # Resolve tick size and step size
        tick_size = None
        step_size = None
        min_qty = None
        price_prec = None
        qty_prec = None
        
        if fapi_meta:
            for f in fapi_meta.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    tick_size = Decimal(str(f["tickSize"]))
                elif f.get("filterType") == "LOT_SIZE":
                    step_size = Decimal(str(f["stepSize"]))
                    min_qty = Decimal(str(f.get("minQty", "0.001")))
            price_prec = fapi_meta.get("pricePrecision")
            qty_prec = fapi_meta.get("quantityPrecision")
            status = fapi_meta.get("status", "TRADING")
        else:
            status = "DELISTED"
            
        if tick_size is None or step_size is None:
            if sym in archived_filters:
                af = archived_filters[sym]
                tick_size = af["tick_size"]
                step_size = af["step_size"]
                min_qty = af.get("min_qty", Decimal("0.001"))
            else:
                raise KeyError(
                    f"CRITICAL ERROR: Symbol '{sym}' has NO tick_size/step_size in exchangeInfo "
                    "or archived metadata! Default values are strictly forbidden."
                )
                
        symbol_info = {
            "symbol": sym,
            "coin": coin,
            "in_cache": in_cache,
            "status": status,
            "is_delisted": (status not in ["TRADING"]),
            "onboard_date_ms": onboard_ms,
            "tick_size": str(tick_size),
            "step_size": str(step_size),
            "min_qty": str(min_qty or "0.001"),
            "price_precision": price_prec,
            "quantity_precision": qty_prec
        }
        universe.append(symbol_info)
        symbol_filters[sym] = symbol_info
        
    print(f"Final Research Universe (2022-12-01 to 2025-12-31): {len(universe)} symbols")
    in_cache_count = sum(1 for u in universe if u["in_cache"])
    missing_cache_count = len(universe) - in_cache_count
    delisted_count = sum(1 for u in universe if u["is_delisted"])
    
    print(f"  From cache: {in_cache_count}")
    print(f"  Missing from cache (need download): {missing_cache_count}")
    print(f"  Delisted included: {delisted_count}")
    
    # Save universe
    with open(REPORTS_DIR / "universe_2022_2025.json", "w") as f:
        json.dump(universe, f, indent=2)
        
    with open(META_DIR / "symbol_filters_universe.json", "w") as f:
        json.dump(symbol_filters, f, indent=2)
        
    print("Saved reports/research/universe_2022_2025.json and data/meta/symbol_filters_universe.json")

if __name__ == "__main__":
    build_universe()
