"""Technical indicators (SPEC §8.1).

Every function is causal (the value at row i depends only on rows <= i), returns a float64 ``pd.Series``
aligned to the input index, and raises ``ValueError`` for ``period < 1``. Indicators work in ``float``;
exchange precision (Decimal) is applied later by ``bot.exchange.filters``.
"""

from __future__ import annotations

import logging
from typing import Final

import numpy as np
import pandas as pd

from bot.errors import ConfigError, DataError

logger = logging.getLogger(__name__)

MA_KINDS: Final[tuple[str, ...]] = ("SMA", "EMA")
_OHLC_COLUMNS: Final[tuple[str, ...]] = ("high", "low", "close")


def _check_period(period: int) -> int:
    """Return ``period`` as a native int; ``ValueError`` unless it is an integer >= 1."""
    if isinstance(period, (bool, np.bool_)) or not isinstance(period, (int, np.integer)):
        raise ValueError(f"period must be an integer >= 1, got {period!r}")
    p = int(period)
    if p < 1:
        raise ValueError(f"period must be >= 1, got {p}")
    return p


def _as_float_series(s: pd.Series) -> pd.Series:
    if not isinstance(s, pd.Series):
        raise TypeError(f"expected a pandas Series, got {type(s).__name__}")
    return s.astype("float64")


def sma(s: pd.Series, period: int) -> pd.Series:
    """Simple moving average: ``s.rolling(period, min_periods=period).mean()``."""
    p = _check_period(period)
    return _as_float_series(s).rolling(p, min_periods=p).mean()


def ema(s: pd.Series, period: int) -> pd.Series:
    """Exponential moving average seeded with the first value: ``ewm(span=period, adjust=False, min_periods=period)``."""
    p = _check_period(period)
    return _as_float_series(s).ewm(span=p, adjust=False, min_periods=p).mean()


def moving_average(s: pd.Series, period: int, kind: str) -> pd.Series:
    """Dispatch to ``sma`` / ``ema``; ``kind`` must be exactly "SMA" or "EMA" (upper-case), else ``ConfigError``."""
    if kind == "SMA":
        return sma(s, period)
    if kind == "EMA":
        return ema(s, period)
    raise ConfigError(f"unknown moving average type {kind!r}; expected one of {', '.join(MA_KINDS)}")


def _require_ohlc(df: pd.DataFrame) -> None:
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"expected a pandas DataFrame, got {type(df).__name__}")
    missing = [c for c in _OHLC_COLUMNS if c not in df.columns]
    if missing:
        raise DataError(f"candle frame is missing columns for true range: {', '.join(missing)}")


def true_range(df: pd.DataFrame) -> pd.Series:
    """``max(h - l, |h - prev_close|, |l - prev_close|)``; row 0 (no previous close) is ``h - l``."""
    _require_ohlc(df)
    high = df["high"].astype("float64")
    low = df["low"].astype("float64")
    prev_close = df["close"].astype("float64").shift(1)
    hl = high - low
    # np.fmax would silently skip NaN; np.maximum propagates it, and the row-0 rule is applied explicitly below.
    tr = np.maximum(hl, np.maximum((high - prev_close).abs(), (low - prev_close).abs()))
    tr = tr.where(prev_close.notna(), hl)
    return tr.astype("float64").rename("true_range")


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average true range (Wilder smoothing): ``true_range(df).ewm(alpha=1/period, adjust=False, min_periods=period)``."""
    p = _check_period(period)
    out = true_range(df).ewm(alpha=1.0 / p, adjust=False, min_periods=p).mean()
    return out.rename("atr")
