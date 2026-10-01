"""Backtest performance metrics (SPEC §11.2).

Every returned value is a plain Python ``int``/``float``/``None`` (no numpy scalars, no NaN/inf), so the
dict can go straight into JSON, SQLite and the dashboard.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np
import pandas as pd

from bot.errors import DataError
from bot.models import ExitReason, Trade
from bot.timeutil import DAY_MS, bars_per_year, interval_to_ms

logger = logging.getLogger(__name__)

__all__ = ["compute_metrics"]

_MS_PER_HOUR = 3_600_000.0
# A standard deviation this small relative to the mean is floating-point noise around "constant returns".
_REL_STD_EPS = 1e-12


def _f(x: Any) -> float | None:
    """Native finite float or None."""
    if x is None:
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def _mean(values: Sequence[float]) -> float | None:
    return _f(math.fsum(values) / len(values)) if values else None


def _ratio(mean: float, std: float, periods: float) -> float | None:
    """``mean / std * sqrt(periods)``; None when std is zero (or indistinguishable from zero)."""
    if not (math.isfinite(mean) and math.isfinite(std)) or not std > 0 or std <= abs(mean) * _REL_STD_EPS:
        return None
    return _f(mean / std * math.sqrt(periods))


def _longest_run(flags: Iterable[bool]) -> int:
    best = cur = 0
    for flag in flags:
        if flag:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


def _sharpe_daily(times: np.ndarray, eq: np.ndarray, e0: float) -> float | None:
    """Resample to UTC days (last equity of each day, E0 prepended) and annualize with sqrt(365)."""
    if len(eq) == 0:
        return None
    days = times // DAY_MS
    last_of_day = np.flatnonzero(np.r_[days[1:] != days[:-1], True])
    daily = np.concatenate(([e0], eq[last_of_day]))
    if len(daily) < 3:  # fewer than 2 daily returns
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        dr = daily[1:] / daily[:-1] - 1.0
    if not bool(np.all(np.isfinite(dr))):
        return None
    return _ratio(float(dr.mean()), float(dr.std(ddof=1)), 365.0)


def compute_metrics(
    equity: pd.DataFrame,
    trades: Sequence[Trade],
    *,
    initial_balance: float,
    interval: str,
) -> dict[str, float | int | None]:
    """Compute the §11.2 metrics from the per-bar equity curve and the closed trades.

    ``equity`` columns: ``time`` (bar open_time ms), ``equity``, ``in_position`` (bool); ``position_qty`` unused.
    Returns r_t = E_t / E_{t-1} - 1 based ratios with E_{-1} = initial_balance (flat bars included).
    """
    e0 = float(initial_balance)
    if not math.isfinite(e0) or e0 <= 0:
        raise ValueError(f"initial_balance must be a positive number (got {initial_balance!r})")
    interval_ms = interval_to_ms(interval)
    periods = bars_per_year(interval)

    if "equity" not in equity.columns:
        raise DataError("equity frame needs an 'equity' column")
    eq = equity["equity"].to_numpy(dtype=np.float64)
    n = int(len(eq))
    if n and not bool(np.all(np.isfinite(eq))):
        raise DataError("equity curve contains NaN/inf values")
    times: np.ndarray | None = None
    if "time" in equity.columns:
        times = equity["time"].to_numpy(dtype=np.int64)

    final = float(eq[-1]) if n else e0
    total_return = final / e0 - 1.0

    # --- CAGR -------------------------------------------------------------------------------
    cagr: float | None = None
    if times is not None and n:
        start_time = int(times[0])
        end_time = int(times[-1]) + interval_ms
        days = (end_time - start_time) / DAY_MS
        if days <= 0:
            cagr = None
        elif final <= 0:
            cagr = -1.0
        else:
            try:
                cagr = _f((final / e0) ** (365.25 / days) - 1.0)
            except OverflowError:  # absurd growth over a very short period: not representable
                cagr = None

    # --- drawdown ---------------------------------------------------------------------------
    path = np.concatenate(([e0], eq))
    running_max = np.maximum.accumulate(path)
    drawdowns = 1.0 - path / running_max
    max_drawdown = max(0.0, float(drawdowns.max()))
    below_peak = (eq < running_max[1:]).tolist()
    max_dd_duration = _longest_run(below_peak)

    # --- per-bar returns: sharpe / sortino --------------------------------------------------
    sharpe: float | None = None
    sortino: float | None = None
    if n:
        prev = np.concatenate(([e0], eq[:-1]))
        with np.errstate(divide="ignore", invalid="ignore"):
            r = eq / prev - 1.0
        if bool(np.all(np.isfinite(r))):
            mean_r = float(r.mean())
            if n >= 2:
                sharpe = _ratio(mean_r, float(r.std(ddof=1)), periods)
            downside = float(np.sqrt(np.mean(np.minimum(r, 0.0) ** 2)))
            if downside > 0:
                sortino = _f(mean_r / downside * math.sqrt(periods))

    sharpe_daily = _sharpe_daily(times, eq, e0) if times is not None else None

    # --- trades -----------------------------------------------------------------------------
    nets = [float(t.net_pnl) for t in trades]
    n_trades = len(nets)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross_profit = math.fsum(wins)
    gross_loss = abs(math.fsum(x for x in nets if x < 0))
    r_values = [float(t.r_multiple) for t in trades if t.r_multiple is not None]
    holding = [(int(t.exit_time) - int(t.entry_time)) / _MS_PER_HOUR for t in trades]
    reasons = [ExitReason(t.exit_reason) for t in trades]
    ordered = sorted(trades, key=lambda t: (int(t.exit_time), int(t.entry_time)))

    exposure: float | None = None
    if n and "in_position" in equity.columns:
        exposure = _f(int(equity["in_position"].astype(bool).sum()) / n)

    metrics: dict[str, float | int | None] = {
        "initial_balance": e0,
        "final_equity": final,
        "total_return": _f(total_return),
        "cagr": cagr,
        "max_drawdown": _f(max_drawdown),
        "max_drawdown_duration_bars": int(max_dd_duration),
        "sharpe": sharpe,
        "sharpe_daily": sharpe_daily,
        "sortino": sortino,
        "n_trades": n_trades,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate": (len(wins) / n_trades) if n_trades else None,
        "profit_factor": _f(gross_profit / gross_loss) if gross_loss > 0 else None,
        "expectancy": _mean(nets),
        "expectancy_r": _mean(r_values),
        "avg_win": _mean(wins),
        "avg_loss": _mean(losses),
        "best_trade": max(nets) if nets else None,
        "worst_trade": min(nets) if nets else None,
        "avg_holding_hours": _mean(holding),
        "exposure": exposure,
        "total_fees": _f(math.fsum(float(t.fees) for t in trades)),
        "total_funding": _f(math.fsum(float(t.funding) for t in trades)),
        "n_liquidations": sum(1 for x in reasons if x is ExitReason.LIQUIDATION),
        "n_stop_losses": sum(1 for x in reasons if x is ExitReason.STOP_LOSS),
        "n_take_profits": sum(1 for x in reasons if x is ExitReason.TAKE_PROFIT),
        "max_consecutive_losses": _longest_run(float(t.net_pnl) <= 0 for t in ordered),
        "bars": n,
    }
    # Final guard for the numpy boundary (§0.2): only native int/float/None leave this function.
    for key, value in metrics.items():
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, int):
            metrics[key] = int(value)
        else:
            metrics[key] = _f(value)
    return metrics
