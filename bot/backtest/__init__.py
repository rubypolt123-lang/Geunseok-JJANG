"""Backtesting: bar-by-bar engine, metrics and result reports (SPEC §11).

Public names are re-exported lazily (PEP 562) so that ``bot.backtest.metrics`` / ``bot.backtest.report`` can be
imported without loading the strategy/risk stack that the engine needs.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Final

__all__ = [
    "METRIC_LABELS_KO",
    "PERCENT_METRICS",
    "compute_metrics",
    "format_metrics_table",
    "new_run_id",
    "run_backtest",
    "save_backtest_result",
]

_EXPORTS: Final[dict[str, str]] = {
    "run_backtest": "bot.backtest.engine",
    "new_run_id": "bot.backtest.engine",
    "compute_metrics": "bot.backtest.metrics",
    "save_backtest_result": "bot.backtest.report",
    "format_metrics_table": "bot.backtest.report",
    "METRIC_LABELS_KO": "bot.backtest.report",
    "PERCENT_METRICS": "bot.backtest.report",
}

if TYPE_CHECKING:
    from bot.backtest.engine import new_run_id, run_backtest
    from bot.backtest.metrics import compute_metrics
    from bot.backtest.report import METRIC_LABELS_KO, PERCENT_METRICS, format_metrics_table, save_backtest_result


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value  # cache: later lookups bypass __getattr__
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
