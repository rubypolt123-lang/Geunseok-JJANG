"""Strategies: base class, registry, indicators and the built-in MA crossover (SPEC §8.1-§8.4).

Importing this package registers the built-in strategies (``ma_cross``).
"""

from __future__ import annotations

from bot.strategy.base import Strategy
from bot.strategy.registry import (
    available_strategies,
    create_strategy,
    get_strategy_class,
    load_strategy_modules,
    register,
)

from . import indicators  # noqa: F401  (``from bot.strategy import indicators`` is used by the backtest engine)
from . import ma_cross  # noqa: F401  (registers "ma_cross")
from .ma_cross import MACrossStrategy

__all__ = [
    "MACrossStrategy",
    "Strategy",
    "indicators",
    "available_strategies",
    "create_strategy",
    "get_strategy_class",
    "load_strategy_modules",
    "register",
]
