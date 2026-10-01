"""Tests for bot/timeutil.py (SPEC §4.3, §14.2 U1)."""

from __future__ import annotations

import numpy as np
import pytest

from bot.config import SUPPORTED_INTERVALS
from bot.errors import ConfigError
from bot.timeutil import (
    DAY_MS,
    INTERVAL_MS,
    bars_per_year,
    expected_last_closed_open,
    floor_time,
    interval_to_ms,
    ms_to_iso,
    next_close_ms,
    now_ms,
    parse_date_ms,
    utc_day,
)

JAN_1_2024_MS = 1_704_067_200_000


def test_interval_ms_table() -> None:
    expected = {
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
    assert INTERVAL_MS == expected
    assert tuple(INTERVAL_MS) == SUPPORTED_INTERVALS
    for interval, ms in expected.items():
        assert interval_to_ms(interval) == ms
    assert DAY_MS == 86_400_000


@pytest.mark.parametrize("interval", ["1M", "1w", "3d", "1s", "", "60m"])
def test_unsupported_intervals_rejected(interval: str) -> None:
    with pytest.raises(ConfigError):
        interval_to_ms(interval)


def test_now_ms_uses_clock() -> None:
    assert now_ms(lambda: 1_790_769_600.1234) == 1_790_769_600_123
    assert now_ms(lambda: 1_790_769_600.9996) == 1_790_769_601_000
    assert isinstance(now_ms(), int)


def test_next_close_and_expected_last_closed() -> None:
    hour = 3_600_000
    now = 1_790_770_782_215  # 2026-09-30T12:19:42.215Z, inside the 12:00 bar
    bar_open = 1_790_769_600_000
    assert floor_time(now, hour) == bar_open
    assert next_close_ms(now, hour) == bar_open + hour
    assert expected_last_closed_open(now, hour) == bar_open - hour
    # exactly on a boundary: the bar that just opened is forming, the previous one is closed
    assert floor_time(bar_open, hour) == bar_open
    assert next_close_ms(bar_open, hour) == bar_open + hour
    assert expected_last_closed_open(bar_open, hour) == bar_open - hour
    # numpy inputs give native ints
    value = floor_time(np.int64(now), np.int64(hour))
    assert value == bar_open and type(value) is int


def test_parse_date_ms_utc() -> None:
    assert parse_date_ms("2024-01-01") == JAN_1_2024_MS
    assert parse_date_ms(" 2024-01-01 ") == JAN_1_2024_MS
    assert parse_date_ms("2024-01-01T00:00:00") == JAN_1_2024_MS  # naive = UTC
    assert parse_date_ms("2024-01-01T00:00:00Z") == JAN_1_2024_MS
    assert parse_date_ms("2024-01-01T09:00:00+09:00") == JAN_1_2024_MS  # KST
    assert parse_date_ms("2024-01-01 01:30") == JAN_1_2024_MS + 90 * 60_000
    assert parse_date_ms("2024-01-01T00:00:00.250Z") == JAN_1_2024_MS + 250
    for bad in ("", "yesterday", "2024-13-01", "2024-02-30", "01/02/2024"):
        with pytest.raises(ConfigError):
            parse_date_ms(bad)
    with pytest.raises(ConfigError):
        parse_date_ms(None)  # type: ignore[arg-type]


def test_bars_per_year() -> None:
    assert bars_per_year("1h") == 8760.0
    assert bars_per_year("4h") == 2190.0
    assert bars_per_year("1d") == 365.0
    assert bars_per_year("1m") == 525_600.0
    assert isinstance(bars_per_year("1h"), float)
    with pytest.raises(ConfigError):
        bars_per_year("1w")


def test_ms_to_iso_utc() -> None:
    assert ms_to_iso(JAN_1_2024_MS) == "2024-01-01T00:00:00Z"
    assert ms_to_iso(1_790_769_600_000) == "2026-09-30T12:00:00Z"
    assert ms_to_iso(np.int64(JAN_1_2024_MS + 3_599_999)) == "2024-01-01T00:59:59Z"
    assert utc_day(JAN_1_2024_MS) == "2024-01-01"
    assert utc_day(JAN_1_2024_MS - 1) == "2023-12-31"
    assert utc_day(JAN_1_2024_MS + DAY_MS - 1) == "2024-01-01"
    assert parse_date_ms(ms_to_iso(1_790_769_600_000)) == 1_790_769_600_000
