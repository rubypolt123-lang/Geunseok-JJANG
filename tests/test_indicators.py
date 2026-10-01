"""Indicators (SPEC §8.1, §14.2 U3)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from bot.errors import ConfigError, DataError
from bot.strategy.indicators import atr, ema, moving_average, sma, true_range


def _assert_values(series: pd.Series, expected: list[float]) -> None:
    assert series.dtype == np.float64
    assert len(series) == len(expected)
    for got, want in zip(series.tolist(), expected):
        if math.isnan(want):
            assert math.isnan(got)
        else:
            assert got == pytest.approx(want, rel=1e-12, abs=1e-12)


def _random_candles(candle_factory, n: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 30_000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    opens = closes * (1 + rng.normal(0.0, 0.002, n))  # gaps vs the previous close exercise |h - prev_c|
    return candle_factory(closes.tolist(), opens=opens.tolist(), wick=0.003)


def test_sma_reference() -> None:
    s = pd.Series([1, 2, 3, 4, 5])
    _assert_values(sma(s, 3), [math.nan, math.nan, 2.0, 3.0, 4.0])
    _assert_values(sma(s, 1), [1.0, 2.0, 3.0, 4.0, 5.0])


def test_ema_reference() -> None:
    s = pd.Series([1, 2, 3, 4, 5])
    _assert_values(ema(s, 3), [math.nan, math.nan, 2.25, 3.125, 4.0625])


def test_atr_reference(ohlc_factory) -> None:
    # (open, high, low, close); bar 1 gaps up, bar 2 gaps down -> the previous-close terms dominate.
    df = ohlc_factory(
        [
            (9.0, 10.0, 8.0, 9.0),  # TR = h-l = 2 (row 0)
            (12.0, 13.0, 11.5, 12.5),  # max(1.5, |13-9|=4, |11.5-9|=2.5) = 4
            (10.0, 10.5, 9.0, 9.5),  # max(1.5, |10.5-12.5|=2, |9-12.5|=3.5) = 3.5
            (9.5, 10.5, 9.5, 10.0),  # max(1, 1, 0) = 1
            (10.0, 11.0, 9.0, 10.5),  # max(2, 1, 1) = 2
        ]
    )
    _assert_values(true_range(df), [2.0, 4.0, 3.5, 1.0, 2.0])
    # Wilder smoothing alpha = 1/3, seeded with TR[0]: 2 -> 8/3 -> 53/18 -> 62/27 -> 178/81; min_periods = 3.
    _assert_values(atr(df, 3), [math.nan, math.nan, 53 / 18, 62 / 27, 178 / 81])
    _assert_values(atr(df, 1), [2.0, 4.0, 3.5, 1.0, 2.0])


def test_indicators_are_causal(candle_factory) -> None:
    df = _random_candles(candle_factory, n=300)
    close = df["close"]
    full = {
        "sma": sma(close, 20),
        "ema": ema(close, 20),
        "tr": true_range(df),
        "atr": atr(df, 14),
    }
    for k in (1, 2, 13, 14, 15, 19, 20, 21, 57, 150, 299):
        prefix_df = df.iloc[:k]
        prefix = {
            "sma": sma(prefix_df["close"], 20),
            "ema": ema(prefix_df["close"], 20),
            "tr": true_range(prefix_df),
            "atr": atr(prefix_df, 14),
        }
        for name, series in prefix.items():
            pd.testing.assert_series_equal(series, full[name].iloc[:k], check_names=False, rtol=1e-12, atol=1e-9)

    # Changing future rows never changes past values.
    mutated = df.copy()
    mutated.loc[200:, ["open", "high", "low", "close"]] = mutated.loc[200:, ["open", "high", "low", "close"]] * 3
    for fn in (lambda d: sma(d["close"], 20), lambda d: ema(d["close"], 20), true_range, lambda d: atr(d, 14)):
        pd.testing.assert_series_equal(fn(mutated).iloc[:200], fn(df).iloc[:200], rtol=0, atol=0)


def test_period_zero_raises(candle_factory) -> None:
    s = pd.Series([1.0, 2.0, 3.0])
    df = candle_factory([1.0, 2.0, 3.0])
    for bad in (0, -1):
        with pytest.raises(ValueError):
            sma(s, bad)
        with pytest.raises(ValueError):
            ema(s, bad)
        with pytest.raises(ValueError):
            atr(df, bad)
        with pytest.raises(ValueError):
            moving_average(s, bad, "SMA")
        with pytest.raises(ValueError):
            moving_average(s, bad, "EMA")
    for not_int in (True, 2.5, "3"):
        with pytest.raises(ValueError):
            sma(s, not_int)  # type: ignore[arg-type]


def test_moving_average_dispatch_and_unknown_kind() -> None:
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    pd.testing.assert_series_equal(moving_average(s, 3, "SMA"), sma(s, 3))
    pd.testing.assert_series_equal(moving_average(s, 3, "EMA"), ema(s, 3))
    for bad in ("WMA", "sma", "", None):
        with pytest.raises(ConfigError):
            moving_average(s, 3, bad)  # type: ignore[arg-type]


def test_outputs_are_float64_aligned_to_input_index(candle_factory) -> None:
    s = pd.Series([3, 1, 4, 1, 5, 9, 2, 6], index=range(100, 108))  # int input, non-default index
    for out in (sma(s, 3), ema(s, 3), moving_average(s, 2, "EMA")):
        assert out.dtype == np.float64
        assert list(out.index) == list(s.index)
    df = candle_factory([10.0, 11.0, 12.0, 11.0, 13.0]).iloc[1:]  # index starts at 1
    for out in (true_range(df), atr(df, 2)):
        assert out.dtype == np.float64
        assert list(out.index) == list(df.index)
    assert true_range(df).iloc[0] == pytest.approx(df["high"].iloc[0] - df["low"].iloc[0])


def test_true_range_requires_ohlc_columns() -> None:
    with pytest.raises(DataError):
        true_range(pd.DataFrame({"high": [1.0], "low": [0.5]}))


def test_empty_input_returns_empty_series(candle_factory) -> None:
    empty = candle_factory([1.0]).iloc[:0]
    assert len(true_range(empty)) == 0
    assert len(atr(empty, 14)) == 0
    assert len(sma(pd.Series([], dtype="float64"), 3)) == 0
