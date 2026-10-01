"""U4 — bot/backtest/metrics.py (SPEC §11.2, §14.2)."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd
import pytest

from bot.backtest.metrics import compute_metrics
from bot.models import Direction, ExitReason, Trade
from bot.timeutil import DAY_MS

T0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z
HOUR = 3_600_000


def equity_frame(values: Sequence[float], *, interval_ms: int = HOUR, in_position: Sequence[bool] | None = None,
                 start: int = T0) -> pd.DataFrame:
    n = len(values)
    return pd.DataFrame(
        {
            "time": np.asarray([start + i * interval_ms for i in range(n)], dtype=np.int64),
            "equity": np.asarray(values, dtype=np.float64),
            "in_position": np.asarray(in_position if in_position is not None else [False] * n, dtype=bool),
            "position_qty": np.zeros(n, dtype=np.float64),
        }
    )


def make_trade(net: float, *, idx: int = 0, reason: ExitReason = ExitReason.SIGNAL, r: float | None = None,
               fees: float = 1.0, funding: float = 0.0, hours: float = 2.0) -> Trade:
    entry = T0 + idx * 10 * HOUR
    return Trade(
        trade_id=f"bt-{idx:05d}", source="backtest", run_id="bt-test", symbol="BTCUSDT", direction=Direction.LONG,
        qty=0.1, entry_time=entry, entry_price=50_000.0, exit_time=entry + int(hours * HOUR), exit_price=50_100.0,
        exit_reason=reason, gross_pnl=net + fees + funding, fees=fees, funding=funding, net_pnl=net,
        r_multiple=r, initial_stop=49_000.0, take_profit=None, leverage=3,
    )


def test_total_return_and_final_equity() -> None:
    m = compute_metrics(equity_frame([100.0, 110.0, 120.0]), [], initial_balance=100.0, interval="1h")
    assert m["initial_balance"] == 100.0
    assert m["final_equity"] == 120.0
    assert m["total_return"] == pytest.approx(0.2)
    assert m["bars"] == 3
    assert m["n_trades"] == 0
    m2 = compute_metrics(equity_frame([95.0, 90.0]), [], initial_balance=100.0, interval="1h")
    assert m2["total_return"] == pytest.approx(-0.1)


def test_max_drawdown_known_series() -> None:
    m = compute_metrics(equity_frame([100, 120, 90, 130, 65]), [], initial_balance=100.0, interval="1h")
    assert m["max_drawdown"] == pytest.approx(0.5)
    assert m["max_drawdown_duration_bars"] == 1
    # the running max includes E0: an immediate loss is a drawdown
    m2 = compute_metrics(equity_frame([90.0, 80.0, 95.0, 101.0]), [], initial_balance=100.0, interval="1h")
    assert m2["max_drawdown"] == pytest.approx(0.2)
    assert m2["max_drawdown_duration_bars"] == 3
    # monotonic growth -> no drawdown
    m3 = compute_metrics(equity_frame([101.0, 102.0, 103.0]), [], initial_balance=100.0, interval="1h")
    assert m3["max_drawdown"] == 0.0
    assert m3["max_drawdown_duration_bars"] == 0


def test_sharpe_annualization_1h() -> None:
    values = [101.0, 99.5, 102.0, 103.5, 101.0, 104.0]
    m = compute_metrics(equity_frame(values), [], initial_balance=100.0, interval="1h")
    e = np.asarray([100.0, *values])
    r = e[1:] / e[:-1] - 1
    expected = r.mean() / r.std(ddof=1) * math.sqrt(8760)
    assert m["sharpe"] == pytest.approx(expected, rel=1e-12)
    downside = math.sqrt(np.mean(np.minimum(r, 0.0) ** 2))
    assert m["sortino"] == pytest.approx(r.mean() / downside * math.sqrt(8760), rel=1e-12)
    # 4h interval annualizes with sqrt(2190)
    m4 = compute_metrics(equity_frame(values, interval_ms=4 * HOUR), [], initial_balance=100.0, interval="4h")
    assert m4["sharpe"] == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(2190), rel=1e-12)


def test_sharpe_daily() -> None:
    # 1h bars over 3 UTC days; last equity per day: 102, 101, 105 (E0 100 prepended)
    values = [101.0] * 23 + [102.0] + [100.0] * 23 + [101.0] + [104.0] * 23 + [105.0]
    m = compute_metrics(equity_frame(values), [], initial_balance=100.0, interval="1h")
    daily = np.asarray([100.0, 102.0, 101.0, 105.0])
    dr = daily[1:] / daily[:-1] - 1
    assert m["sharpe_daily"] == pytest.approx(dr.mean() / dr.std(ddof=1) * math.sqrt(365), rel=1e-12)
    # a single day -> None
    one_day = compute_metrics(equity_frame([101.0, 102.0, 103.0]), [], initial_balance=100.0, interval="1h")
    assert one_day["sharpe_daily"] is None


def test_sharpe_none_when_flat() -> None:
    m = compute_metrics(equity_frame([100.0] * 50), [], initial_balance=100.0, interval="1h")
    assert m["sharpe"] is None
    assert m["sharpe_daily"] is None
    assert m["sortino"] is None
    assert m["max_drawdown"] == 0.0
    assert m["total_return"] == 0.0
    # a single bar: not enough returns for a standard deviation
    m1 = compute_metrics(equity_frame([101.0]), [], initial_balance=100.0, interval="1h")
    assert m1["sharpe"] is None


def test_profit_factor_none_without_losses() -> None:
    wins = [make_trade(10.0, idx=0), make_trade(5.0, idx=1)]
    m = compute_metrics(equity_frame([110.0, 115.0]), wins, initial_balance=100.0, interval="1h")
    assert m["profit_factor"] is None
    assert m["avg_loss"] is None
    assert m["n_losses"] == 0
    mixed = [make_trade(10.0, idx=0), make_trade(-4.0, idx=1), make_trade(6.0, idx=2), make_trade(-1.0, idx=3)]
    m2 = compute_metrics(equity_frame([111.0]), mixed, initial_balance=100.0, interval="1h")
    assert m2["profit_factor"] == pytest.approx(16.0 / 5.0)
    # break-even trades are losses for counting but do not create a profit factor denominator
    even = [make_trade(10.0, idx=0), make_trade(0.0, idx=1)]
    m3 = compute_metrics(equity_frame([110.0]), even, initial_balance=100.0, interval="1h")
    assert m3["profit_factor"] is None
    assert m3["n_losses"] == 1


def test_win_rate_expectancy() -> None:
    trades = [
        make_trade(10.0, idx=0, r=1.0, hours=2, reason=ExitReason.TAKE_PROFIT, fees=1.0, funding=0.5),
        make_trade(-5.0, idx=1, r=-0.5, hours=4, reason=ExitReason.STOP_LOSS, fees=1.5, funding=-0.2),
        make_trade(-3.0, idx=2, r=None, hours=6, reason=ExitReason.LIQUIDATION, fees=0.5, funding=0.0),
        make_trade(6.0, idx=3, r=0.6, hours=8, reason=ExitReason.SIGNAL, fees=1.0, funding=0.1),
        make_trade(-1.0, idx=4, r=-0.1, hours=2, reason=ExitReason.STOP_LOSS, fees=1.0, funding=0.0),
    ]
    m = compute_metrics(equity_frame([107.0]), trades, initial_balance=100.0, interval="1h")
    assert m["n_trades"] == 5
    assert m["n_wins"] == 2
    assert m["n_losses"] == 3
    assert m["win_rate"] == pytest.approx(0.4)
    assert m["expectancy"] == pytest.approx(7.0 / 5)
    assert m["expectancy_r"] == pytest.approx((1.0 - 0.5 + 0.6 - 0.1) / 4)
    assert m["avg_win"] == pytest.approx(8.0)
    assert m["avg_loss"] == pytest.approx(-3.0)
    assert m["best_trade"] == 10.0
    assert m["worst_trade"] == -5.0
    assert m["avg_holding_hours"] == pytest.approx(4.4)
    assert m["total_fees"] == pytest.approx(5.0)
    assert m["total_funding"] == pytest.approx(0.4)
    assert m["n_stop_losses"] == 2
    assert m["n_take_profits"] == 1
    assert m["n_liquidations"] == 1
    assert m["max_consecutive_losses"] == 2  # trades 1, 2 (then a win, then one loss)


def test_max_consecutive_losses_uses_exit_time_order() -> None:
    # listed out of order: by exit time the three losses are consecutive
    trades = [make_trade(-1.0, idx=2), make_trade(5.0, idx=0), make_trade(-1.0, idx=3), make_trade(-2.0, idx=1)]
    m = compute_metrics(equity_frame([101.0]), trades, initial_balance=100.0, interval="1h")
    assert m["max_consecutive_losses"] == 3


def test_exposure() -> None:
    m = compute_metrics(
        equity_frame([100.0, 101.0, 102.0, 100.0], in_position=[True, False, True, True]),
        [], initial_balance=100.0, interval="1h",
    )
    assert m["exposure"] == pytest.approx(0.75)
    m0 = compute_metrics(equity_frame([100.0, 100.0]), [], initial_balance=100.0, interval="1h")
    assert m0["exposure"] == 0.0


def test_cagr() -> None:
    # 365 daily bars: days = (time[-1] + 1d - time[0]) / 1d = 365
    values = list(np.linspace(100.0, 110.0, 365))
    m = compute_metrics(equity_frame(values, interval_ms=DAY_MS), [], initial_balance=100.0, interval="1d")
    assert m["cagr"] == pytest.approx(1.1 ** (365.25 / 365) - 1, rel=1e-12)
    # 1h bars over 10 days
    hourly = [100.0] * 239 + [101.0]
    mh = compute_metrics(equity_frame(hourly), [], initial_balance=100.0, interval="1h")
    assert mh["cagr"] == pytest.approx(1.01 ** (365.25 / 10) - 1, rel=1e-12)
    # wiped out -> -1.0
    mw = compute_metrics(equity_frame([50.0, 0.0], interval_ms=DAY_MS), [], initial_balance=100.0, interval="1d")
    assert mw["cagr"] == -1.0
    assert mw["total_return"] == -1.0
    # unrepresentable growth -> None, never inf
    mx = compute_metrics(equity_frame([1e300]), [], initial_balance=1.0, interval="1m")
    assert mx["cagr"] is None


def test_empty_equity_curve() -> None:
    m = compute_metrics(equity_frame([]), [], initial_balance=100.0, interval="1h")
    assert m["bars"] == 0
    assert m["final_equity"] == 100.0
    assert m["cagr"] is None
    assert m["sharpe"] is None
    assert m["exposure"] is None


def test_no_numpy_scalars_or_nan() -> None:
    rng = np.random.default_rng(7)
    values = list(10_000.0 * np.cumprod(1 + rng.normal(0, 0.01, 500)))
    in_pos = list(rng.random(500) > 0.5)
    trades = [make_trade(float(x), idx=i, r=float(x) / 50) for i, x in enumerate(rng.normal(0, 20, 30))]
    for eq, tr in ((equity_frame(values, in_position=in_pos), trades), (equity_frame([10_000.0] * 5), [])):
        m = compute_metrics(eq, tr, initial_balance=10_000.0, interval="1h")
        for key, value in m.items():
            assert value is None or type(value) in (int, float), (key, type(value))
            if isinstance(value, float):
                assert math.isfinite(value), key
    expected_keys = {
        "initial_balance", "final_equity", "total_return", "cagr", "max_drawdown", "max_drawdown_duration_bars",
        "sharpe", "sharpe_daily", "sortino", "n_trades", "n_wins", "n_losses", "win_rate", "profit_factor",
        "expectancy", "expectancy_r", "avg_win", "avg_loss", "best_trade", "worst_trade", "avg_holding_hours",
        "exposure", "total_fees", "total_funding", "n_liquidations", "n_stop_losses", "n_take_profits",
        "max_consecutive_losses", "bars",
    }
    assert set(m) == expected_keys
    for key in ("n_trades", "bars", "max_drawdown_duration_bars", "max_consecutive_losses", "n_stop_losses"):
        assert type(m[key]) is int


def test_invalid_initial_balance() -> None:
    with pytest.raises(ValueError):
        compute_metrics(equity_frame([1.0]), [], initial_balance=0.0, interval="1h")
