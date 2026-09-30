"""ICT Time engine: session, killzone, key-level tracking for one symbol.

All session/killzone boundaries are in America/New_York (DST-correct via zoneinfo).
Stored and computed in UTC epoch milliseconds. No look-ahead: only closed candles.
"""

from __future__ import annotations

import zoneinfo
from collections import deque
from datetime import UTC, date, datetime, timedelta
from typing import Any

from deskmind.agents.ict_time.config import DEFAULT_CONFIG, IctTimeConfig
from deskmind.agents.ict_time.models import KeyLevel, SessionItem
from deskmind.agents.trend.models import Bar

_NY = zoneinfo.ZoneInfo("America/New_York")


# ------------------------------------------------------------------ time helpers


def _to_ny(ms: int) -> datetime:
    """Convert epoch ms to America/New_York datetime."""
    return datetime.fromtimestamp(ms / 1000, UTC).astimezone(_NY)


def _to_ms(dt: datetime) -> int:
    """Datetime → epoch ms."""
    return int(dt.timestamp() * 1000)


def _ny_date(ms: int) -> date:
    return _to_ny(ms).date()


def _ny_midnight_ms(d: date) -> int:
    """00:00 America/New_York on date d → epoch ms."""
    return _to_ms(datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=_NY))


def _week_start_ny(d: date) -> date:
    """Monday of the week containing date d (NY calendar)."""
    return d - timedelta(days=d.weekday())


def _week_str(d: date) -> str:
    ws = _week_start_ny(d)
    return ws.strftime("%Y-W%W")


def _month_str(d: date) -> str:
    return d.strftime("%Y-%m")


# ------------------------------------------------------------------ session boundaries

_SESSION_DEF: dict[str, tuple[int, int]] = {
    # (open_hour, close_hour) in NY time; open is on *previous* calendar day for Asia
    "asia": (20, 24),       # 20:00 prev-day NY -> 00:00 same day
    "london": (2, 5),
    "new_york": (8, 17),
}

# Killzone definitions (open_hour_min, close_hour_min) in NY time, (h, m) pairs
_KZ_DEF: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "asian":        ((20, 0), (24, 0)),   # wraps midnight; handled specially
    "london_open":  ((2, 0), (5, 0)),
    "ny_am":        ((7, 0), (10, 0)),
    "ny_lunch":     ((12, 0), (13, 30)),
    "london_close": ((10, 0), (12, 0)),
}

# Silver bullet windows (open, close) NY time
_SB_DEF: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "sb1": ((3, 0), (4, 0)),
    "sb2": ((10, 0), (11, 0)),
    "sb3": ((14, 0), (15, 0)),
}

# Level type labels for chart
LEVEL_LABELS: dict[str, str] = {
    "pdh": "PDH", "pdl": "PDL",
    "pwh": "PWH", "pwl": "PWL",
    "pmh": "PMH", "pml": "PML",
    "daily_open": "DO", "weekly_open": "WO", "monthly_open": "MO",
    "ny_midnight_open": "MO", "asian_range_high": "ARH", "asian_range_low": "ARL",
}


def _ny_hm(dt: datetime) -> tuple[int, int]:
    """(hour, minute) in NY time from a datetime."""
    ny = dt.astimezone(_NY)
    return ny.hour, ny.minute


def _minutes_of_day_ny(ms: int) -> int:
    """Minutes since midnight in NY time."""
    h, m = _ny_hm(_to_ny(ms))
    return h * 60 + m


def _in_window(ms: int, open_hm: tuple[int, int], close_hm: tuple[int, int]) -> bool:
    """True if ms falls in [open_hm, close_hm) in NY time (intra-day, no wrap)."""
    mins = _minutes_of_day_ny(ms)
    o = open_hm[0] * 60 + open_hm[1]
    c = close_hm[0] * 60 + close_hm[1]
    return o <= mins < c


def _in_asian_kz(ms: int) -> bool:
    """Asian KZ: 20:00–24:00 NY previous day or 00:00 on same day (handled as 20:00–24:00)."""
    h, m = _ny_hm(_to_ny(ms))
    return h >= 20  # 20:00 NY onward (wraps: midnight is 00:00 next day, caught separately)


def _current_killzone(ms: int) -> str | None:
    dt = _to_ny(ms)
    h, m = dt.hour, dt.minute
    mins = h * 60 + m
    if h >= 20:                  # Asian KZ
        return "asian"
    if 2 * 60 <= mins < 5 * 60: return "london_open"
    if 7 * 60 <= mins < 10 * 60: return "ny_am"
    if 10 * 60 <= mins < 12 * 60: return "london_close"
    if 12 * 60 <= mins < 13 * 60 + 30: return "ny_lunch"
    return None


def _current_silver_bullet(ms: int) -> str | None:
    for name, (o, c) in _SB_DEF.items():
        if _in_window(ms, o, c):
            return name
    return None


def _minutes_to_next_kz(ms: int) -> int:
    """Minutes from ms until the start of the next killzone (NY time)."""
    current = _current_killzone(ms)
    mins = _minutes_of_day_ny(ms)
    # All next starts in minutes-of-day order
    starts = [
        ("london_open", 2 * 60),
        ("ny_am", 7 * 60),
        ("london_close", 10 * 60),
        ("ny_lunch", 12 * 60),
        ("asian", 20 * 60),
    ]
    for name, start in starts:
        if name == current:
            continue
        if start > mins:
            return start - mins
    # Wrap to next day
    first = starts[0]
    return (24 * 60 - mins) + first[1]


# ------------------------------------------------------------------ engine


class IctTimeEngine:
    """Incremental ICT Time engine for one symbol.

    Feed closed candles strictly in time order via `on_candle`.
    Returns new/updated items (full item dicts) per call.
    """

    def __init__(self, symbol: str, cfg: IctTimeConfig = DEFAULT_CONFIG) -> None:
        self.symbol = symbol
        self.cfg = cfg
        self.bars: deque[Bar] = deque(maxlen=cfg.bar_memory)

        # Active sessions: one per session name (replaces on new date)
        self.sessions: dict[str, SessionItem] = {}

        # Key levels: keyed by id
        self.key_levels: dict[str, KeyLevel] = {}

        # Period tracking (NY midnight calendar)
        self._last_ny_day: date | None = None
        self._last_ny_week: date | None = None  # Monday
        self._last_ny_month: str | None = None  # "YYYY-MM"

        # Period accumulators (build prev-period stats before confirming)
        self._day_high: float | None = None
        self._day_low: float | None = None
        self._day_open: float | None = None
        self._week_high: float | None = None
        self._week_low: float | None = None
        self._week_open: float | None = None
        self._month_high: float | None = None
        self._month_low: float | None = None
        self._month_open: float | None = None

        # Asia session accumulator for Asian range
        self._asia_sess_high: float | None = None
        self._asia_sess_low: float | None = None
        self._asia_sess_open: float | None = None

        # AMD state for today
        self._amd_daily_open: float | None = None
        self._amd_asia_swept_high: bool = False
        self._amd_asia_swept_low: bool = False
        self._amd_manipulation_done: bool = False
        self._amd_phase: str = "unknown"
        self._amd_confidence: str = "low"

        # ATR (simple rolling 14-period)
        self._atr: float | None = None
        self._atr_period = 14
        self._tr_buf: deque[float] = deque(maxlen=self._atr_period)

        self.as_of: int | None = None

    # ---------------------------------------------------------------- helpers

    def _update_atr(self, bar: Bar) -> None:
        if len(self.bars) >= 2:
            prev = list(self.bars)[-2]
            tr = max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close))
        else:
            tr = bar.high - bar.low
        self._tr_buf.append(tr)
        if len(self._tr_buf) >= self._atr_period:
            self._atr = sum(self._tr_buf) / len(self._tr_buf)

    def _is_ny_midnight_crossed(self, bar: Bar) -> bool:
        """True if bar's open and close straddle or the bar closes on a new NY day."""
        open_day = _ny_date(bar.open_time)
        close_day = _ny_date(bar.close_time - 1)  # close_time is exclusive
        return close_day > open_day or (
            self._last_ny_day is not None and close_day > self._last_ny_day
        )

    def _session_name_at(self, ms: int) -> str | None:
        """Which session is active at ms (NY time)?"""
        h, m = _ny_hm(_to_ny(ms))
        mins = h * 60 + m
        if h >= 20:               return "asia"
        if 0 <= mins < 0:         return "asia"   # handled via prev-day
        if 2 * 60 <= mins < 5 * 60:   return "london"
        if 8 * 60 <= mins < 17 * 60:  return "new_york"
        # Asia runs 20:00 prev-day to 00:00; so 00:00 is end of Asia
        if 0 <= mins < 2 * 60:    return "asia"   # midnight to 02:00 still Asia
        return None

    # ---------------------------------------------------------------- main

    def on_candle(self, bar: Bar) -> list[dict[str, Any]]:
        """Process one closed candle. Returns new/updated items."""
        if self.bars and bar.open_time <= self.bars[-1].open_time:
            return []

        self._update_atr(bar)
        self.bars.append(bar)
        if len(self.bars) < self.cfg.min_warmup_bars:
            return []
        changes: list[dict[str, Any]] = []

        close_ms = bar.close_time
        close_ny = _to_ny(close_ms)
        close_day = close_ny.date()

        # ---- Detect NY-midnight day boundary
        new_day = self._last_ny_day is None or close_day > self._last_ny_day

        if new_day:
            # Confirm PDH/PDL/daily_open/ny_midnight_open from previous day
            self._confirm_day_levels(bar, changes)
            # Reset accumulators for the new day
            self._day_high = bar.high
            self._day_low = bar.low
            self._day_open = bar.open
            self._last_ny_day = close_day
            # AMD: new daily open
            self._amd_daily_open = bar.open
            self._amd_asia_swept_high = False
            self._amd_asia_swept_low = False
            self._amd_manipulation_done = False
            self._amd_phase = "unknown"
            self._amd_confidence = "low"

        # ---- Detect week boundary (Monday NY)
        week_start = _week_start_ny(close_day)
        new_week = self._last_ny_week is None or week_start > self._last_ny_week
        if new_week:
            self._confirm_week_levels(bar, changes)
            self._week_high = bar.high
            self._week_low = bar.low
            self._week_open = bar.open
            self._last_ny_week = week_start

        # ---- Detect month boundary
        month_str = _month_str(close_day)
        new_month = self._last_ny_month is None or month_str != self._last_ny_month
        if new_month:
            self._confirm_month_levels(bar, changes)
            self._month_high = bar.high
            self._month_low = bar.low
            self._month_open = bar.open
            self._last_ny_month = month_str

        # ---- Update period accumulators
        if self._day_high is None or bar.high > self._day_high:
            self._day_high = bar.high
        if self._day_low is None or bar.low < self._day_low:
            self._day_low = bar.low
        if self._week_high is None or bar.high > self._week_high:
            self._week_high = bar.high
        if self._week_low is None or bar.low < self._week_low:
            self._week_low = bar.low
        if self._month_high is None or bar.high > self._month_high:
            self._month_high = bar.high
        if self._month_low is None or bar.low < self._month_low:
            self._month_low = bar.low

        # ---- Session tracking
        changes.extend(self._update_sessions(bar))

        # ---- Key level sweep detection
        changes.extend(self._check_level_sweeps(bar))

        # ---- AMD update
        self._update_amd(bar)

        self.as_of = close_ms
        return changes

    # ---------------------------------------------------------------- session logic

    def _session_window_at(self, ms: int) -> str | None:
        """Session that owns this ms."""
        h, _ = _ny_hm(_to_ny(ms))
        if h >= 20 or h < 2:  return "asia"
        if 2 <= h < 5:         return "london"
        if 8 <= h < 17:        return "new_york"
        return None

    def _update_sessions(self, bar: Bar) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        close_ny = _to_ny(bar.close_time)
        close_day = close_ny.date()

        # Determine which session this bar belongs to
        sess = self._session_window_at(bar.open_time)
        if sess is None:
            # Between sessions; close open sessions if needed
            for s in list(self.sessions.values()):
                if s.state == "active":
                    s.state = "closed"
                    s.closed_at = bar.close_time
                    changes.append(s.item())
            return changes

        # Key: session + NY calendar date for the *start* of the session
        # Asia session starts on the previous day (20:00 prev-day)
        if sess == "asia":
            h, _ = _ny_hm(close_ny)
            if h >= 20:
                sess_date = close_day
            else:
                sess_date = close_day - timedelta(days=1)
        else:
            sess_date = close_day

        sess_id = f"session_{sess}_{sess_date.isoformat()}"

        if sess_id not in self.sessions:
            # Create new session
            s = SessionItem(
                name=sess,  # type: ignore[arg-type]
                date_ny=sess_date.isoformat(),
                created_time=bar.open_time,
                confirmed_at=bar.close_time,
                state="active",
                high=bar.high,
                low=bar.low,
                open=bar.close,
            )
            self.sessions[sess_id] = s
            changes.append(s.item())
        else:
            s = self.sessions[sess_id]

        # Close sessions that ended before this bar
        for sid, os in list(self.sessions.items()):
            if sid == sess_id:
                continue
            if os.state == "active":
                os.state = "closed"
                os.closed_at = bar.open_time
                changes.append(os.item())

        # Update active session extremes
        if s.state == "active":
            updated = False
            if s.high is None or bar.high > s.high:
                s.high = bar.high
                updated = True
            if s.low is None or bar.low < s.low:
                s.low = bar.low
                updated = True
            if updated:
                changes.append(s.item())

        return changes

    # ---------------------------------------------------------------- period levels

    def _confirm_day_levels(self, bar: Bar, changes: list[dict[str, Any]]) -> None:
        """Emit PDH/PDL/daily_open/ny_midnight_open for the ending day."""
        if self._last_ny_day is None or self._day_high is None:
            return
        ref = self._last_ny_day.isoformat()
        conf = bar.close_time

        for ltype, price in [
            ("pdh", self._day_high),
            ("pdl", self._day_low),
            ("daily_open", self._day_open),
            ("ny_midnight_open", self._day_open),
        ]:
            if price is None:
                continue
            lv = KeyLevel(
                level_type=ltype,  # type: ignore[arg-type]
                price=price,
                reference_period=ref,
                created_time=bar.open_time,
                confirmed_at=conf,
            )
            self.key_levels[lv.id] = lv
            changes.append(lv.item())

        # Asian range high/low: derived from the Asia session for this day
        # The Asia session date_ny = previous calendar day (session starts 20:00 prev-day)
        asia_date = (self._last_ny_day - timedelta(days=1)).isoformat()
        asia_id = f"session_asia_{asia_date}"
        asia = self.sessions.get(asia_id)
        if asia is not None and asia.high is not None:
            for ltype, price in [
                ("asian_range_high", asia.high),
                ("asian_range_low", asia.low),
            ]:
                if price is None:
                    continue
                lv = KeyLevel(
                    level_type=ltype,  # type: ignore[arg-type]
                    price=price,
                    reference_period=self._last_ny_day.isoformat(),
                    created_time=bar.open_time,
                    confirmed_at=conf,
                )
                self.key_levels[lv.id] = lv
                changes.append(lv.item())

    def _confirm_week_levels(self, bar: Bar, changes: list[dict[str, Any]]) -> None:
        if self._last_ny_week is None or self._week_high is None:
            return
        ref = _week_str(self._last_ny_week)
        conf = bar.close_time
        for ltype, price in [
            ("pwh", self._week_high),
            ("pwl", self._week_low),
            ("weekly_open", self._week_open),
        ]:
            if price is None:
                continue
            lv = KeyLevel(
                level_type=ltype,  # type: ignore[arg-type]
                price=price,
                reference_period=ref,
                created_time=bar.open_time,
                confirmed_at=conf,
            )
            self.key_levels[lv.id] = lv
            changes.append(lv.item())

    def _confirm_month_levels(self, bar: Bar, changes: list[dict[str, Any]]) -> None:
        if self._last_ny_month is None or self._month_high is None:
            return
        ref = self._last_ny_month
        conf = bar.close_time
        for ltype, price in [
            ("pmh", self._month_high),
            ("pml", self._month_low),
            ("monthly_open", self._month_open),
        ]:
            if price is None:
                continue
            lv = KeyLevel(
                level_type=ltype,  # type: ignore[arg-type]
                price=price,
                reference_period=ref,
                created_time=bar.open_time,
                confirmed_at=conf,
            )
            self.key_levels[lv.id] = lv
            changes.append(lv.item())

    # ---------------------------------------------------------------- sweep detection

    def _check_level_sweeps(self, bar: Bar) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for lv in self.key_levels.values():
            if lv.state == "taken":
                continue
            price = lv.price
            # Swept: wick through (high > level for resistance, low < for support)
            # Taken: close beyond level
            if lv.level_type in ("pdh", "pwh", "pmh", "asian_range_high",
                                  "weekly_open", "monthly_open", "daily_open",
                                  "ny_midnight_open"):
                # Treat as resistance (price above = swept/taken)
                if bar.high > price and bar.close <= price:
                    if lv.state == "active":
                        lv.state = "swept"
                        lv.swept_at = bar.close_time
                        changes.append(lv.item())
                elif bar.close > price:
                    lv.state = "taken"
                    lv.taken_at = bar.close_time
                    changes.append(lv.item())
            else:
                # Support levels (pdl, pwl, pml, asian_range_low)
                if bar.low < price and bar.close >= price:
                    if lv.state == "active":
                        lv.state = "swept"
                        lv.swept_at = bar.close_time
                        changes.append(lv.item())
                elif bar.close < price:
                    lv.state = "taken"
                    lv.taken_at = bar.close_time
                    changes.append(lv.item())
        return changes

    # ---------------------------------------------------------------- AMD heuristic

    def _update_amd(self, bar: Bar) -> None:
        """Update AMD phase heuristic for the current day (HEURISTIC ONLY)."""
        if self._amd_daily_open is None:
            return
        kz = _current_killzone(bar.close_time)

        # Check manipulation: Asian range swept in London/NY AM
        arh_id = f"asian_range_high_{self._last_ny_day.isoformat()}" if self._last_ny_day else None
        arl_id = f"asian_range_low_{self._last_ny_day.isoformat()}" if self._last_ny_day else None
        arh = self.key_levels.get(arh_id) if arh_id else None
        arl = self.key_levels.get(arl_id) if arl_id else None

        if kz in ("london_open", "ny_am"):
            if arh is not None and bar.high > arh.price:
                self._amd_asia_swept_high = True
            if arl is not None and bar.low < arl.price:
                self._amd_asia_swept_low = True

        # Phase determination
        criteria = 0
        if self._amd_asia_swept_high or self._amd_asia_swept_low:
            criteria += 1
            self._amd_manipulation_done = True

        if self._amd_manipulation_done:
            # Distribution: price closes opposite side of daily open from sweep
            if self._amd_asia_swept_high and bar.close < self._amd_daily_open:
                criteria += 1
                self._amd_phase = "distribution"
            elif self._amd_asia_swept_low and bar.close > self._amd_daily_open:
                criteria += 1
                self._amd_phase = "distribution"
            else:
                self._amd_phase = "manipulation"
        else:
            self._amd_phase = "accumulation"

        self._amd_confidence = "low" if criteria == 0 else ("medium" if criteria == 1 else "high")

    # ---------------------------------------------------------------- state for report

    def state(self) -> dict[str, Any]:
        """Current engine state for reports."""
        close_ms = self.as_of or 0
        atr = self._atr

        sessions_out: dict[str, Any] = {}
        for s in self.sessions.values():
            sessions_out[s.name] = s.summary()

        levels_out = []
        for lv in sorted(self.key_levels.values(), key=lambda l: l.confirmed_at):
            if lv.state == "taken":
                continue  # don't report taken levels
            d: dict[str, Any] = lv.summary()
            if atr and atr > 0:
                close_price = list(self.bars)[-1].close if self.bars else 0.0
                d["distance_atr"] = round((lv.price - close_price) / atr, 3)
            else:
                d["distance_atr"] = None
            levels_out.append(d)

        weekend = False
        if close_ms:
            dt = datetime.fromtimestamp(close_ms / 1000, UTC)
            weekend = dt.weekday() >= 5  # Sat=5, Sun=6

        kz = _current_killzone(close_ms) if close_ms else None
        sb = _current_silver_bullet(close_ms) if close_ms else None

        return {
            "sessions": sessions_out,
            "current_session": self._current_session_name(close_ms),
            "killzone": {
                "current": kz,
                "minutes_to_next": _minutes_to_next_kz(close_ms) if close_ms else None,
            },
            "silver_bullet": {
                "active": sb,
                "fvg_in_windows": [],  # populated by agent layer via ICT SMC store
            },
            "key_levels": levels_out,
            "weekend_flag": weekend,
            "amd": {
                "phase": self._amd_phase,
                "confidence": self._amd_confidence,
                "heuristic": True,
            },
        }

    def _current_session_name(self, ms: int) -> str | None:
        if not ms:
            return None
        for s in self.sessions.values():
            if s.state == "active":
                return s.name
        return None
