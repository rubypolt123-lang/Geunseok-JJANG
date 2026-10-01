"""Strategy base class (SPEC §8.2).

Contract: the SAME instance code runs in the backtest (``prepare`` once, then ``signal_at(prepared, i)`` for each
bar) and live (``generate(closed_df)``). Strategies only ever see CLOSED candles, never the forming one, and never
know the position: the trader/engine maps signals to actions with ``bot.risk.decide_action``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

import pandas as pd

from bot.errors import ConfigError, DataError
from bot.models import Signal

logger = logging.getLogger(__name__)


class Strategy(ABC):
    """Abstract closed-candle strategy. Subclasses set ``name`` and register with ``@register``."""

    name: ClassVar[str]  # registry key, lower_snake_case
    required_columns: ClassVar[tuple[str, ...]] = ("open_time", "open", "high", "low", "close")

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        defaults = self.default_params()
        given = dict(params or {})
        unknown = sorted(str(k) for k in given if k not in defaults)
        if unknown:
            allowed = ", ".join(defaults) or "(none)"
            raise ConfigError(
                f"unknown parameter(s) for strategy '{self._display_name()}': {', '.join(unknown)}; "
                f"allowed: {allowed}"
            )
        self.params: dict[str, Any] = {**defaults, **given}
        self.validate_params()

    # ------------------------------------------------------------------ subclass API

    @classmethod
    @abstractmethod
    def default_params(cls) -> dict[str, Any]:
        """Default value of every accepted parameter (a new dict on every call)."""

    def validate_params(self) -> None:
        """Override to validate/normalize ``self.params``; raise ``ConfigError`` on bad values."""
        return None

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Minimum number of closed bars before ``signal_at`` may return anything but NONE."""

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a NEW frame (``df.copy()``) with causal indicator columns appended; never mutate ``df``."""

    @abstractmethod
    def signal_at(self, prepared: pd.DataFrame, i: int) -> Signal:
        """Signal at the CLOSE of row ``i`` using only rows ``0..i`` of ``prepared``.

        Must return NONE with reason "warmup" when ``i < warmup_bars - 1`` or any needed value is NaN.
        """

    # ------------------------------------------------------------------ shared behaviour

    def generate(self, df: pd.DataFrame) -> Signal:
        """Live path: signal for the last CLOSED candle of ``df``."""
        prepared = self.prepare(df)
        if len(prepared) == 0:
            raise DataError(f"strategy '{self._display_name()}' needs at least one closed candle")
        return self.signal_at(prepared, len(prepared) - 1)

    def describe(self) -> str:
        """``"ma_cross(fast_period=20, slow_period=50, ma_type=EMA, allow_short=True)"``."""
        args = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self._display_name()}({args})"

    def __repr__(self) -> str:
        return self.describe()

    # ------------------------------------------------------------------ helpers for subclasses

    @classmethod
    def _display_name(cls) -> str:
        return getattr(cls, "name", cls.__name__)

    def check_columns(self, df: pd.DataFrame) -> None:
        """``DataError`` unless ``df`` is a DataFrame holding every ``required_columns`` entry."""
        if not isinstance(df, pd.DataFrame):
            raise TypeError(f"expected a pandas DataFrame, got {type(df).__name__}")
        missing = [c for c in self.required_columns if c not in df.columns]
        if missing:
            raise DataError(
                f"strategy '{self._display_name()}' input is missing columns: {', '.join(missing)}"
            )

    @staticmethod
    def check_index(prepared: pd.DataFrame, i: int) -> int:
        """Return ``i`` as a native int; ``ValueError`` unless ``0 <= i < len(prepared)`` (no negative indexing)."""
        if isinstance(i, bool):
            raise ValueError("bar index must be an integer, not bool")
        idx = int(i)
        if idx != i or not 0 <= idx < len(prepared):
            raise ValueError(f"bar index {i!r} out of range for {len(prepared)} prepared rows")
        return idx
