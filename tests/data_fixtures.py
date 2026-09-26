"""Synthetic Binance-shaped data for offline tests (no network)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Iterable

KLINE_HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore"
)


def kline_row(t_ms: int, interval_ms: int, base: float = 20000.0) -> list[str]:
    """Deterministic, OHLC-valid 12-field kline for open_time t_ms (price level ~base)."""
    k = t_ms // interval_ms
    o = base + (k % 97) * 3.5
    c = o + ((k % 7) - 3) * 1.25
    h = max(o, c) + 2.0 + (k % 3)
    lo = min(o, c) - 1.5 - (k % 5)
    vol = 10 + (k % 11) * 0.5
    return [
        str(t_ms),
        f"{o:.2f}",
        f"{h:.2f}",
        f"{lo:.2f}",
        f"{c:.2f}",
        f"{vol:.3f}",
        str(t_ms + interval_ms - 1),
        f"{vol * o:.5f}",
        str(100 + k % 50),
        f"{vol / 2:.3f}",
        f"{vol * o / 2:.5f}",
        "0",
    ]


def resample_rows(rows: list[list[str]], factor: int, interval_ms: int) -> list[list[str]]:
    """Exact resample of consecutive kline rows (as Binance would build the higher timeframe)."""
    out = []
    for i in range(0, len(rows) - factor + 1, factor):
        chunk = rows[i : i + factor]
        t = int(chunk[0][0])
        vol = sum(float(r[5]) for r in chunk)
        out.append(
            [
                str(t),
                chunk[0][1],
                f"{max(float(r[2]) for r in chunk):.2f}",
                f"{min(float(r[3]) for r in chunk):.2f}",
                chunk[-1][4],
                f"{vol:.3f}",
                str(t + interval_ms * factor - 1),
                "0",
                "0",
                "0",
                "0",
                "0",
            ]
        )
    return out


def make_zip(name: str, rows: Iterable[Iterable[str]], header: str | None = KLINE_HEADER) -> bytes:
    lines = ([header] if header else []) + [",".join(r) for r in rows]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, "\n".join(lines) + "\n")
    return buf.getvalue()


def checksum_file(data: bytes, name: str) -> bytes:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()


class FakeClock:
    """Deterministic clock + sleep pair for rate-limit tests."""

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
