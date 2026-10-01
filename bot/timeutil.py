"""Time helpers (SPEC §4.3). All timestamps are int milliseconds since the epoch, UTC."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Final

from bot.errors import ConfigError

INTERVAL_MS: Final[dict[str, int]] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}
DAY_MS: Final = 86_400_000

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MS: Final = timedelta(milliseconds=1)
_DATE_ONLY_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def interval_to_ms(interval: str) -> int:
    """Interval string -> milliseconds. ``ConfigError`` for unsupported intervals (e.g. 3d, 1w, 1M, 1s)."""
    try:
        return INTERVAL_MS[interval]
    except (KeyError, TypeError):
        supported = " ".join(INTERVAL_MS)
        raise ConfigError(f"unsupported interval {interval!r}; supported: {supported}") from None


def now_ms(clock: Callable[[], float] = time.time) -> int:
    """Current time of ``clock`` (seconds) as int milliseconds."""
    return int(round(clock() * 1000))


def floor_time(ts_ms: int, interval_ms: int) -> int:
    """Start of the interval containing ``ts_ms``."""
    ts = int(ts_ms)
    step = int(interval_ms)
    return ts - ts % step


def next_close_ms(now_ms: int, interval_ms: int) -> int:
    """Close boundary of the current (forming) bar == open_time of the next bar."""
    return floor_time(now_ms, interval_ms) + int(interval_ms)


def expected_last_closed_open(now_ms: int, interval_ms: int) -> int:
    """open_time of the most recent bar that is fully closed at ``now_ms``."""
    return floor_time(now_ms, interval_ms) - int(interval_ms)


def parse_date_ms(s: str) -> int:
    """Parse "YYYY-MM-DD" (00:00 UTC) or ISO 8601 with/without timezone (naive = UTC) -> ms.

    Raises ``ConfigError`` when the value cannot be parsed.
    """
    if not isinstance(s, str) or not s.strip():
        raise ConfigError(f"invalid date {s!r}: expected YYYY-MM-DD or ISO 8601")
    text = s.strip()
    try:
        if _DATE_ONLY_RE.fullmatch(text):
            d = date.fromisoformat(text)
            dt = datetime(d.year, d.month, d.day, tzinfo=UTC)
        else:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
    except ValueError:
        raise ConfigError(f"invalid date {s!r}: expected YYYY-MM-DD or ISO 8601") from None
    return (dt - _EPOCH) // _ONE_MS


def ms_to_iso(ts_ms: int) -> str:
    """ms -> "2024-01-01T00:00:00Z" (UTC, second precision)."""
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_day(ts_ms: int) -> str:
    """ms -> UTC calendar day "2024-01-01"."""
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).strftime("%Y-%m-%d")


def bars_per_year(interval: str) -> float:
    """365 * DAY_MS / interval_ms (1h -> 8760.0, 4h -> 2190.0, 1d -> 365.0)."""
    return 365 * DAY_MS / interval_to_ms(interval)
