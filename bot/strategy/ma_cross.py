"""Moving-average crossover strategy (SPEC §8.4).

Golden cross (fast MA crosses above slow MA at the close of bar i) -> LONG; dead cross -> SHORT, or CLOSE (exit a
long only) when ``allow_short`` is false. The signal fires on the crossing bar only.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import pandas as pd

from bot.errors import ConfigError
from bot.models import Signal, SignalAction
from bot.strategy.base import Strategy
from bot.strategy.indicators import MA_KINDS, moving_average
from bot.strategy.registry import register

logger = logging.getLogger(__name__)


def _as_int_param(value: Any, key: str) -> int:
    """int (or numpy integer / integral float) -> int; bool, strings and non-integral floats -> ConfigError."""
    if isinstance(value, (bool, np.bool_)):
        raise ConfigError(f"strategy.params.{key} must be an integer, got {value!r}")
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)) and math.isfinite(value) and float(value).is_integer():
        return int(value)
    raise ConfigError(f"strategy.params.{key} must be an integer, got {value!r}")


@register
class MACrossStrategy(Strategy):
    """Fast/slow moving-average crossover on closes (SMA or EMA)."""

    name = "ma_cross"

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"fast_period": 20, "slow_period": 50, "ma_type": "EMA", "allow_short": True}

    def validate_params(self) -> None:
        p = self.params
        fast = _as_int_param(p["fast_period"], "fast_period")
        if fast < 1:
            raise ConfigError(f"strategy.params.fast_period must be >= 1, got {fast}")
        slow = _as_int_param(p["slow_period"], "slow_period")
        if slow <= fast:
            raise ConfigError(
                f"strategy.params.slow_period ({slow}) must be greater than fast_period ({fast})"
            )
        ma_type = p["ma_type"]
        if not isinstance(ma_type, str) or ma_type.strip().upper() not in MA_KINDS:
            raise ConfigError(
                f"strategy.params.ma_type must be one of {', '.join(MA_KINDS)}, got {ma_type!r}"
            )
        allow_short = p["allow_short"]
        if not isinstance(allow_short, (bool, np.bool_)):
            raise ConfigError(f"strategy.params.allow_short must be true or false, got {allow_short!r}")
        p["fast_period"] = fast
        p["slow_period"] = slow
        p["ma_type"] = ma_type.strip().upper()
        p["allow_short"] = bool(allow_short)

    @property
    def warmup_bars(self) -> int:
        slow = int(self.params["slow_period"])
        # EMA needs ~3 x period to forget its seed; SMA is exact after `slow` bars (+1 for the previous-bar diff).
        return 3 * slow + 1 if self.params["ma_type"] == "EMA" else slow + 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        self.check_columns(df)
        out = df.copy()
        close = out["close"].astype("float64")
        kind = self.params["ma_type"]
        out["ma_fast"] = moving_average(close, self.params["fast_period"], kind)
        out["ma_slow"] = moving_average(close, self.params["slow_period"], kind)
        return out

    def signal_at(self, prepared: pd.DataFrame, i: int) -> Signal:
        i = self.check_index(prepared, i)
        col = prepared.columns.get_loc
        c_fast, c_slow = col("ma_fast"), col("ma_slow")
        bar_open_time = int(prepared.iat[i, col("open_time")])
        price = float(prepared.iat[i, col("close")])
        f1 = float(prepared.iat[i, c_fast])
        s1 = float(prepared.iat[i, c_slow])
        meta = {"ma_fast": f1, "ma_slow": s1}

        def make(action: SignalAction, reason: str) -> Signal:
            return Signal(action=action, bar_open_time=bar_open_time, price=price, reason=reason, meta=meta)

        if i < self.warmup_bars - 1 or i < 1:
            return make(SignalAction.NONE, "warmup")
        f0 = float(prepared.iat[i - 1, c_fast])
        s0 = float(prepared.iat[i - 1, c_slow])
        if any(math.isnan(v) for v in (f0, s0, f1, s1)):
            return make(SignalAction.NONE, "warmup")
        d0 = f0 - s0
        d1 = f1 - s1
        if d0 <= 0 and d1 > 0:
            return make(SignalAction.LONG, "golden_cross")
        if d0 >= 0 and d1 < 0:
            if self.params["allow_short"]:
                return make(SignalAction.SHORT, "dead_cross")
            return make(SignalAction.CLOSE, "dead_cross_close_long")
        return make(SignalAction.NONE, "no_cross")
