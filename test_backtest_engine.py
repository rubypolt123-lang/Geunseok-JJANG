"""U4 — bot/backtest/engine.py (SPEC §11.1, §14.2).

Most cases drive the engine with a scripted (unregistered) strategy so every signal bar is explicit; the
look-ahead / determinism cases use the real ``ma_cross`` strategy with ATR stops on a random walk.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from bot.backtest.engine import new_run_id, run_backtest
from bot.backtest.report import save_backtest_result
from bot.config import ExecutionConfig, FeeConfig, RiskConfig, StopLossConfig
from bot.errors import DataError
from bot.exchange.filters import round_protective_price
from bot.models import BacktestResult, Direction, ExitReason, Signal, SignalAction, SymbolFilters
from bot.risk import approx_liquidation_price, plan_entry
from bot.storage import Storage
from bot.strategy import indicators
from bot.strategy.base import Strategy
from bot.strategy.ma_cross import MACrossStrategy

T0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z
HOUR = 3_600_000
DAY = 86_400_000
RUN_ID = "bt-20240101-000000-abcdef"
TAKER = 0.0005
MAKER = 0.0002
SLIP = 0.0005  # 5 bps
BALANCE = 10_000.0
FLAT = (50_000.0, 50_010.0, 49_990.0, 50_000.0)

LONG = SignalAction.LONG
SHORT = SignalAction.SHORT
CLOSE = SignalAction.CLOSE


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def make_risk(**overrides: Any) -> RiskConfig:
    """Percent stop 2 %, no TP, no kill switch, no cooldown, no notional cap unless overridden."""
    base: dict[str, Any] = {
        "leverage": 3,
        "max_leverage": 10,
        "risk_per_trade_pct": 1.0,
        "stop_loss": StopLossConfig(mode="percent", percent=2.0, atr_period=14, atr_multiple=2.0),
        "take_profit_r": None,
        "max_position_notional": 1_000_000.0,
        "max_margin_fraction": 0.9,
        "max_daily_loss_pct": 0.0,
        "kill_switch_flatten": True,
        "cooldown_bars_after_stop": 0,
        "min_liq_distance_multiple": 2.0,
        "maint_margin_rate": 0.004,
        "liq_mmr_buffer": 0.005,
    }
    base.update(overrides)
    return RiskConfig(**base)


def make_execution(*, taker: float = TAKER, maker: float = MAKER, slippage_bps: float = 5.0) -> ExecutionConfig:
    return ExecutionConfig(
        fees=FeeConfig(maker=maker, taker=taker),
        slippage_bps=slippage_bps,
        working_type="MARK_PRICE",
        price_protect=False,
        protective_mode="close_position",
        candle_close_delay_sec=3.0,
        kline_limit=500,
        recv_window_ms=5000,
        heartbeat_sec=30,
        bot_id="mab1",
    )


class ScriptedStrategy(Strategy):
    """Returns the scripted action for bar index ``i`` (NONE otherwise). Not registered in the registry."""

    name = "scripted_test"

    def __init__(self, script: Mapping[int, SignalAction] | None = None, *, warmup: int = 1) -> None:
        super().__init__({"warmup": warmup})
        self.script: dict[int, SignalAction] = dict(script or {})
        self.calls: list[int] = []

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"warmup": 1}

    @property
    def warmup_bars(self) -> int:
        return int(self.params["warmup"])

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.copy()

    def signal_at(self, prepared: pd.DataFrame, i: int) -> Signal:
        i = self.check_index(prepared, i)
        self.calls.append(i)
        t = int(prepared["open_time"].iat[i])
        price = float(prepared["close"].iat[i])
        if i < self.warmup_bars - 1:
            return Signal(SignalAction.NONE, t, price, "warmup")
        return Signal(self.script.get(i, SignalAction.NONE), t, price, "scripted")


def run(
    df: pd.DataFrame,
    strategy: Strategy,
    filters: SymbolFilters,
    *,
    risk: RiskConfig | None = None,
    execution: ExecutionConfig | None = None,
    funding: pd.DataFrame | None = None,
    trade_start_ms: int | None = None,
    interval: str = "1h",
    initial_balance: float = BALANCE,
    config_snapshot: dict | None = None,
) -> BacktestResult:
    return run_backtest(
        df,
        strategy,
        symbol="BTCUSDT",
        interval=interval,
        filters=filters,
        risk=risk or make_risk(),
        execution=execution or make_execution(),
        initial_balance=initial_balance,
        funding=funding,
        trade_start_ms=trade_start_ms,
        run_id=RUN_ID,
        config_snapshot=config_snapshot,
    )


def funding_frame(rows: Sequence[tuple[int, float, float]]) -> pd.DataFrame:
    """``(funding_time, funding_rate, mark_price)`` rows in the downloader's column layout."""
    return pd.DataFrame(
        {
            "funding_time": np.asarray([r[0] for r in rows], dtype=np.int64),
            "funding_rate": np.asarray([r[1] for r in rows], dtype=np.float64),
            "mark_price": np.asarray([r[2] for r in rows], dtype=np.float64),
        }
    )


def ot(df: pd.DataFrame, i: int) -> int:
    return int(df["open_time"].iat[i])


def ct(df: pd.DataFrame, i: int) -> int:
    return int(df["close_time"].iat[i])


def random_walk(candle_factory: Callable[..., pd.DataFrame], n: int = 400, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 50_000.0 * np.cumprod(1.0 + rng.normal(0.0, 0.008, n))
    return candle_factory([float(x) for x in closes], wick=0.004)


def random_funding(df: pd.DataFrame, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    first = int(df["open_time"].iat[0])
    last = int(df["close_time"].iat[-1])
    times = list(range(first, last, 8 * HOUR))
    return funding_frame([(t, float(rng.normal(0.0001, 0.0003)), 50_000.0) for t in times])


def assert_cash_consistent(result: BacktestResult) -> None:
    """With every position closed, final equity == initial balance + sum of net PnL."""
    expected = result.initial_balance + math.fsum(t.net_pnl for t in result.trades)
    assert result.metrics["final_equity"] == pytest.approx(expected, rel=1e-12, abs=1e-9)


# ---------------------------------------------------------------------------------------------
# Timing: signal on close, fill on next open
# ---------------------------------------------------------------------------------------------


def test_signal_on_close_fill_on_next_open(ohlc_factory, btc_filters) -> None:
    rows = [FLAT, FLAT, FLAT, (50_100.0, 50_110.0, 50_090.0, 50_100.0), FLAT, FLAT]
    df = ohlc_factory(rows)
    strat = ScriptedStrategy({2: LONG, 5: LONG})  # bar 5 is the last bar: never asked
    result = run(df, strat, btc_filters)

    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.direction is Direction.LONG
    assert t.entry_time == ot(df, 3)
    assert t.entry_price == pytest.approx(50_100.0 * (1 + SLIP), rel=1e-15)
    # sized and stopped from the NEXT bar's open (ref 50100), not from the signal bar's close (50000)
    assert t.initial_stop == pytest.approx(50_100.0 * 0.98)
    plan = plan_entry(direction=Direction.LONG, ref_price=50_100.0, equity=BALANCE, atr_value=None,
                      filters=btc_filters, risk=make_risk(), fees=make_execution().fees, slippage_bps=5.0)
    assert plan.plan is not None and t.qty == float(plan.plan.qty)
    # no position until the fill bar
    assert result.equity["in_position"].tolist() == [False, False, False, True, True, True]
    # decisions at every bar but the last, in order, each once
    assert strat.calls == [0, 1, 2, 3, 4]
    assert t.trade_id == f"{RUN_ID}-00001"
    assert t.source == "backtest" and t.run_id == RUN_ID


def test_no_trade_before_warmup(ohlc_factory, candle_factory, btc_filters) -> None:
    df = ohlc_factory([FLAT] * 12)
    # signals before the warm-up are never even requested
    strat = ScriptedStrategy({0: LONG, 1: LONG, 2: LONG, 3: LONG}, warmup=5)
    result = run(df, strat, btc_filters)
    assert result.trades == []
    assert min(strat.calls) == 4  # warmup_bars - 1
    assert result.start_time == ot(df, 4)

    # real strategy: the first possible fill is the bar after the first decision bar
    closes = [50_000.0 * (1 + 0.03 * math.sin(2 * math.pi * k / 30)) for k in range(150)]
    rw = candle_factory(closes)
    ma = MACrossStrategy({"fast_period": 3, "slow_period": 8, "ma_type": "SMA"})
    res = run(rw, ma, btc_filters, risk=make_risk(stop_loss=StopLossConfig("percent", 10.0, 14, 2.0)))
    assert res.trades, "the oscillating series must produce crosses"
    assert res.start_time == ot(rw, ma.warmup_bars - 1)
    assert all(t.entry_time >= ot(rw, ma.warmup_bars) for t in res.trades)


def test_fees_charged_both_legs(ohlc_factory, btc_filters) -> None:
    rows = [FLAT, FLAT, FLAT, (50_000.0, 50_510.0, 49_990.0, 50_500.0), (50_500.0, 50_510.0, 50_490.0, 50_500.0),
            (50_500.0, 50_510.0, 50_490.0, 50_500.0)]
    df = ohlc_factory(rows)
    result = run(df, ScriptedStrategy({1: LONG, 3: CLOSE}), btc_filters)

    assert len(result.trades) == 1
    t = result.trades[0]
    entry = 50_000.0 * (1 + SLIP)
    exit_ = 50_500.0 * (1 - SLIP)
    assert t.entry_price == pytest.approx(entry)
    assert t.exit_price == pytest.approx(exit_)
    assert t.exit_reason is ExitReason.SIGNAL
    assert t.exit_time == ot(df, 4)  # market exit at the next open
    entry_fee = t.qty * entry * TAKER
    exit_fee = t.qty * exit_ * TAKER
    assert t.fees == pytest.approx(entry_fee + exit_fee, rel=1e-12)
    assert t.gross_pnl == pytest.approx(t.qty * (exit_ - entry), rel=1e-12)
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.fees, rel=1e-12)
    assert result.metrics["total_fees"] == pytest.approx(t.fees)
    assert_cash_consistent(result)

    # without fees and slippage the trade is booked at the raw opens with zero fees
    free = run(df, ScriptedStrategy({1: LONG, 3: CLOSE}), btc_filters,
               execution=make_execution(taker=0.0, maker=0.0, slippage_bps=0.0))
    tf = free.trades[0]
    assert tf.fees == 0.0
    assert tf.entry_price == 50_000.0 and tf.exit_price == 50_500.0
    assert tf.net_pnl == pytest.approx(tf.qty * 500.0)


# ---------------------------------------------------------------------------------------------
# Protective exits
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("signal", [LONG, SHORT])
def test_clean_stop_out_is_minus_one_r(signal: SignalAction, ohlc_factory, btc_filters) -> None:
    if signal is LONG:
        stop_bar = (50_000.0, 50_010.0, 48_900.0, 49_100.0)  # low through the 49000 stop, no gap
        stop, closing_slip = 49_000.0, 1 - SLIP
    else:
        stop_bar = (50_000.0, 51_100.0, 49_990.0, 50_900.0)  # high through the 51000 stop, no gap
        stop, closing_slip = 51_000.0, 1 + SLIP
    df = ohlc_factory([FLAT, FLAT, stop_bar, FLAT, FLAT])
    result = run(df, ScriptedStrategy({1: signal}), btc_filters)

    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.initial_stop == pytest.approx(stop)
    assert t.exit_price == pytest.approx(stop * closing_slip)
    assert t.exit_time == ct(df, 2)  # intrabar exit in the entry bar itself
    assert t.funding == 0.0
    assert t.r_multiple == pytest.approx(-1.0, abs=1e-9)
    direction = Direction.LONG if signal is LONG else Direction.SHORT
    decision = plan_entry(direction=direction, ref_price=50_000.0, equity=BALANCE, atr_value=None,
                          filters=btc_filters, risk=make_risk(), fees=make_execution().fees, slippage_bps=5.0)
    assert decision.plan is not None
    assert t.net_pnl == pytest.approx(-decision.plan.risk_amount, rel=1e-9)
    assert result.metrics["n_stop_losses"] == 1
    assert_cash_consistent(result)


def test_sl_first_rule_in_engine(ohlc_factory, btc_filters) -> None:
    risk = make_risk(take_profit_r=2.0)  # LONG from 50000: stop 49000, TP 52000

    # both levels touched inside the entry bar, neither gapped at the open -> STOP_LOSS at the stop
    both = ohlc_factory([FLAT, FLAT, (50_000.0, 52_500.0, 48_500.0, 50_000.0), FLAT])
    t = run(both, ScriptedStrategy({1: LONG}), btc_filters, risk=risk).trades[0]
    assert t.take_profit == pytest.approx(52_000.0)
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.exit_price == pytest.approx(49_000.0 * (1 - SLIP))
    assert t.exit_time == ct(both, 2)

    # gap beyond the TP at the open (the low also crosses the stop): TAKE_PROFIT at the open
    gap_tp = ohlc_factory([FLAT, FLAT, FLAT, (52_600.0, 52_700.0, 48_000.0, 50_000.0), FLAT])
    t = run(gap_tp, ScriptedStrategy({1: LONG}), btc_filters, risk=risk).trades[0]
    assert t.exit_reason is ExitReason.TAKE_PROFIT
    assert t.exit_price == pytest.approx(52_600.0 * (1 - SLIP))
    assert t.exit_time == ct(gap_tp, 3)

    # gap through the stop at the open: STOP_LOSS at the (worse) open
    gap_sl = ohlc_factory([FLAT, FLAT, FLAT, (48_800.0, 48_900.0, 48_700.0, 48_850.0), FLAT])
    t = run(gap_sl, ScriptedStrategy({1: LONG}), btc_filters, risk=risk).trades[0]
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.exit_price == pytest.approx(48_800.0 * (1 - SLIP))
    assert t.r_multiple is not None and t.r_multiple < -1.0

    # TP alone inside the bar -> TAKE_PROFIT at the TP level
    tp_only = ohlc_factory([FLAT, FLAT, FLAT, (50_000.0, 52_100.0, 49_900.0, 51_000.0), FLAT])
    t = run(tp_only, ScriptedStrategy({1: LONG}), btc_filters, risk=risk).trades[0]
    assert t.exit_reason is ExitReason.TAKE_PROFIT
    assert t.exit_price == pytest.approx(52_000.0 * (1 - SLIP))


# ---------------------------------------------------------------------------------------------
# Signal -> action mapping
# ---------------------------------------------------------------------------------------------


def test_flip_closes_then_opens(ohlc_factory, btc_filters) -> None:
    rows = [
        FLAT,
        FLAT,  # LONG signal
        (50_000.0, 52_010.0, 49_990.0, 52_000.0),  # long filled at the open
        (52_000.0, 54_010.0, 51_990.0, 54_000.0),  # SHORT signal -> FLIP_SHORT
        (54_000.0, 54_010.0, 53_990.0, 54_000.0),  # flip at this open
        (54_000.0, 54_010.0, 53_990.0, 54_000.0),
        (54_000.0, 54_010.0, 53_990.0, 54_000.0),
    ]
    df = ohlc_factory(rows)
    result = run(df, ScriptedStrategy({1: LONG, 3: SHORT}), btc_filters)

    assert len(result.trades) == 2
    first, second = result.trades
    assert first.direction is Direction.LONG
    assert first.exit_reason is ExitReason.FLIP
    assert first.exit_time == ot(df, 4)
    assert first.exit_price == pytest.approx(54_000.0 * (1 - SLIP))
    assert second.direction is Direction.SHORT
    assert second.entry_time == first.exit_time  # same timestamp: close leg, then open leg
    assert second.entry_price == pytest.approx(54_000.0 * (1 - SLIP))
    assert second.trade_id == f"{RUN_ID}-00002"
    assert second.exit_reason is ExitReason.END_OF_DATA

    # the open leg is sized from the cash AFTER the close leg at this same open
    post_close_cash = BALANCE + first.net_pnl
    pre_close_cash = BALANCE - first.qty * first.entry_price * TAKER

    def qty_for(equity: float) -> float:
        d = plan_entry(direction=Direction.SHORT, ref_price=54_000.0, equity=equity, atr_value=None,
                       filters=btc_filters, risk=make_risk(), fees=make_execution().fees, slippage_bps=5.0)
        assert d.plan is not None
        return float(d.plan.qty)

    assert second.qty == qty_for(post_close_cash)
    assert qty_for(post_close_cash) != qty_for(pre_close_cash)  # the scenario really discriminates
    assert_cash_consistent(result)


def test_allow_short_false_only_closes(candle_factory, btc_filters) -> None:
    closes = [50_000.0 * (1 + 0.03 * math.sin(2 * math.pi * k / 40)) for k in range(240)]
    df = candle_factory(closes)
    risk = make_risk(stop_loss=StopLossConfig("percent", 10.0, 14, 2.0))  # wide stop: only signals exit

    long_only = run(df, MACrossStrategy({"fast_period": 3, "slow_period": 8, "ma_type": "SMA",
                                         "allow_short": False}), btc_filters, risk=risk)
    assert len(long_only.trades) >= 3
    assert all(t.direction is Direction.LONG for t in long_only.trades)
    assert {t.exit_reason for t in long_only.trades} <= {ExitReason.SIGNAL, ExitReason.END_OF_DATA}
    assert any(t.exit_reason is ExitReason.SIGNAL for t in long_only.trades)
    # a dead-cross close never opens anything at the same open
    entries = {t.entry_time for t in long_only.trades}
    assert all(t.exit_time not in entries for t in long_only.trades if t.exit_reason is ExitReason.SIGNAL)

    both_ways = run(df, MACrossStrategy({"fast_period": 3, "slow_period": 8, "ma_type": "SMA",
                                         "allow_short": True}), btc_filters, risk=risk)
    assert any(t.direction is Direction.SHORT for t in both_ways.trades)
    assert any(t.exit_reason is ExitReason.FLIP for t in both_ways.trades)

    # scripted CLOSE never closes a short and does nothing when flat
    flat_rows = candle_factory([50_000.0] * 8)
    res = run(flat_rows, ScriptedStrategy({0: CLOSE, 1: SHORT, 3: CLOSE}), btc_filters)
    assert len(res.trades) == 1
    assert res.trades[0].direction is Direction.SHORT
    assert res.trades[0].exit_reason is ExitReason.END_OF_DATA


# ---------------------------------------------------------------------------------------------
# Kill switch and cooldown
# ---------------------------------------------------------------------------------------------


def _kill_rows() -> list[tuple[float, float, float, float]]:
    drop = (50_000.0, 50_010.0, 49_390.0, 49_400.0)  # -1.2 % close, stop 49000 untouched
    after = (49_400.0, 49_410.0, 49_390.0, 49_400.0)
    return [FLAT, FLAT, drop] + [after] * 27  # 30 hourly bars: bar 24 opens 2024-01-02 00:00 UTC


def test_kill_switch_blocks_entries_and_flattens(ohlc_factory, btc_filters) -> None:
    df = ohlc_factory(_kill_rows())
    signals = {1: LONG, 4: LONG, 10: LONG, 23: LONG, 25: LONG}
    risk = make_risk(max_daily_loss_pct=0.5, kill_switch_flatten=True)
    result = run(df, ScriptedStrategy(signals), btc_filters, risk=risk)

    # equity at the close of bar 2: 10000 - entry fee + 0.09 * (49400 - 50025) ~= 9941.5 -> -0.59 % trips
    assert result.equity["equity"].iat[2] <= BALANCE * (1 - 0.005)
    assert len(result.trades) == 2
    killed, next_day = result.trades
    assert killed.exit_reason is ExitReason.KILL_SWITCH
    assert killed.exit_time == ot(df, 3)  # flattened at the next open
    assert killed.exit_price == pytest.approx(49_400.0 * (1 - SLIP))
    # LONG signals at bars 4, 10, 23 (same UTC day) were blocked; bar 25 (next day) opens at bar 26
    assert next_day.entry_time == ot(df, 26)
    assert ot(df, 24) == T0 + DAY
    assert next_day.exit_reason is ExitReason.END_OF_DATA
    assert_cash_consistent(result)

    # without flattening the position is kept (exits are never forced) and no entry happens while long
    kept = run(df, ScriptedStrategy(signals), btc_filters,
               risk=make_risk(max_daily_loss_pct=0.5, kill_switch_flatten=False))
    assert len(kept.trades) == 1
    assert kept.trades[0].exit_reason is ExitReason.END_OF_DATA

    # max_daily_loss_pct = 0 disables the switch
    off = run(df, ScriptedStrategy(signals), btc_filters, risk=make_risk(max_daily_loss_pct=0.0))
    assert all(t.exit_reason is not ExitReason.KILL_SWITCH for t in off.trades)


def test_kill_switch_trips_on_1d_interval(ohlc_factory, btc_filters) -> None:
    drop = (50_000.0, 50_010.0, 49_390.0, 49_400.0)
    after = (49_400.0, 49_410.0, 49_390.0, 49_400.0)
    df = ohlc_factory([FLAT, FLAT, drop, after, after, after, after], interval="1d")
    risk = make_risk(max_daily_loss_pct=0.5)
    result = run(df, ScriptedStrategy({1: LONG, 3: LONG}), btc_filters, risk=risk, interval="1d")

    assert len(result.trades) == 2
    killed, again = result.trades
    # the single daily bar that lost 0.59 % (baseline = previous day's close equity) trips the switch
    assert killed.entry_time == ot(df, 2)
    assert killed.exit_reason is ExitReason.KILL_SWITCH
    assert killed.exit_time == ot(df, 3)
    # every 1d bar is a new UTC day: the switch re-arms and the LONG at bar 3 fills at bar 4
    assert again.entry_time == ot(df, 4)
    assert again.exit_reason is ExitReason.END_OF_DATA


def test_cooldown_after_stop_blocks_reentry(ohlc_factory, btc_filters) -> None:
    stop_bar = (50_000.0, 50_010.0, 48_900.0, 49_500.0)  # T = bar 3
    after = (49_500.0, 49_510.0, 49_490.0, 49_500.0)
    df = ohlc_factory([FLAT, FLAT, FLAT, stop_bar] + [after] * 8)
    signals = {1: LONG, **{i: LONG for i in range(3, 11)}}

    blocked = run(df, ScriptedStrategy(signals), btc_filters, risk=make_risk(cooldown_bars_after_stop=3))
    assert blocked.trades[0].exit_reason is ExitReason.STOP_LOSS
    assert blocked.trades[0].exit_time == ct(df, 3)
    # decisions at T, T+i, T+2i blocked; T+3i decides -> first new fill at T+4i
    assert blocked.trades[1].entry_time == ot(df, 3 + 4)

    free = run(df, ScriptedStrategy(signals), btc_filters, risk=make_risk(cooldown_bars_after_stop=0))
    assert free.trades[1].entry_time == ot(df, 3 + 1)

    one = run(df, ScriptedStrategy(signals), btc_filters, risk=make_risk(cooldown_bars_after_stop=1))
    assert one.trades[1].entry_time == ot(df, 3 + 2)


# ---------------------------------------------------------------------------------------------
# Funding and liquidation
# ---------------------------------------------------------------------------------------------


def test_funding_charged_by_timestamp_rule(ohlc_factory, btc_filters) -> None:
    df = ohlc_factory([FLAT] * 30)
    funding = funding_frame(
        [
            (T0 + 8 * HOUR, 0.0001, 50_000.0),  # == entry time (bar 8 open): NOT charged
            (T0 + 16 * HOUR, 0.0003, 50_100.0),  # mid-trade: charged
            (T0 + 24 * HOUR, -0.0002, float("nan")),  # == exit time (bar 24 open): charged, mark -> bar open
        ]
    )

    # LONG: entry at bar 8, CLOSE decided at bar 23 -> exit at bar 24 open
    res = run(df, ScriptedStrategy({7: LONG, 23: CLOSE}), btc_filters, funding=funding)
    (t,) = res.trades
    assert t.entry_time == T0 + 8 * HOUR and t.exit_time == T0 + 24 * HOUR
    expected = t.qty * 50_100.0 * 0.0003 + t.qty * 50_000.0 * (-0.0002)  # + paid, - received
    assert t.funding == pytest.approx(expected, rel=1e-12)
    assert t.funding > 0  # a long pays positive funding
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.fees - t.funding, rel=1e-12)
    assert res.metrics["funding_events"] == 2
    assert res.metrics["total_funding"] == pytest.approx(expected)
    assert_cash_consistent(res)

    # SHORT: positive rate is received (negative payment); exit at bar 24 via FLIP, the new long entered at
    # the funding timestamp is not charged for it
    res_s = run(df, ScriptedStrategy({7: SHORT, 23: LONG}), btc_filters, funding=funding)
    short, new_long = res_s.trades
    assert short.direction is Direction.SHORT and short.exit_reason is ExitReason.FLIP
    expected_s = -short.qty * 50_100.0 * 0.0003 + (-short.qty) * 50_000.0 * (-0.0002)
    assert short.funding == pytest.approx(expected_s, rel=1e-12)
    assert short.funding < 0
    assert new_long.entry_time == T0 + 24 * HOUR
    assert new_long.funding == 0.0
    assert res_s.metrics["funding_events"] == 2
    assert_cash_consistent(res_s)


def test_funding_events_metric(ohlc_factory, btc_filters) -> None:
    df = ohlc_factory([FLAT] * 30)
    rows = [
        (T0 + 24 * HOUR, 0.0001, 50_000.0),  # after the exit (flat): not charged
        (T0 + 16 * HOUR, 0.0002, 50_000.0),  # mid-trade: charged
        (T0, 0.0001, 50_000.0),  # at open_time[s]: skipped before the simulation starts
        (T0 + 4 * HOUR, 0.0001, 50_000.0),  # flat: not charged
        (T0 + 8 * HOUR, 0.0001, 50_000.0),  # entry timestamp: not charged
        (T0 + 20 * HOUR, -0.0001, 50_000.0),  # exit timestamp: charged
        (T0 + 16 * HOUR, 0.0002, 50_000.0),  # duplicate row: counted once
    ]
    res = run(df, ScriptedStrategy({7: LONG, 19: CLOSE}), btc_filters, funding=funding_frame(rows))
    (t,) = res.trades
    assert t.exit_time == T0 + 20 * HOUR
    assert res.metrics["funding_events"] == 2
    assert type(res.metrics["funding_events"]) is int
    assert t.funding == pytest.approx(t.qty * 50_000.0 * (0.0002 - 0.0001))
    assert res.metrics["total_funding"] == pytest.approx(t.funding)

    for nothing in (None, funding_frame([])):
        r0 = run(df, ScriptedStrategy({7: LONG, 19: CLOSE}), btc_filters, funding=nothing)
        assert r0.metrics["funding_events"] == 0
        assert r0.metrics["total_funding"] == 0.0
        assert r0.trades[0].funding == 0.0


def test_liquidation_loses_isolated_margin(ohlc_factory, btc_filters) -> None:
    gap = (33_000.0, 33_100.0, 32_900.0, 33_000.0)  # opens below the ~33636 liquidation price
    df = ohlc_factory([FLAT] * 17 + [gap] + [gap] * 6)
    funding = funding_frame([(T0 + 16 * HOUR, 0.001, 50_000.0)])  # paid before the liquidation
    res = run(df, ScriptedStrategy({7: LONG}), btc_filters, funding=funding)

    (t,) = res.trades
    liq = approx_liquidation_price(50_000.0, Direction.LONG, 3, 0.004 + 0.005)
    assert t.exit_reason is ExitReason.LIQUIDATION
    assert t.exit_price == pytest.approx(liq)
    assert t.exit_time == ct(df, 17)
    funding_paid = t.qty * 50_000.0 * 0.001
    assert t.funding == pytest.approx(funding_paid)
    im = t.qty * t.entry_price / 3
    entry_fee = t.qty * t.entry_price * TAKER
    assert t.fees == pytest.approx(entry_fee)  # no exit fee on a liquidation
    assert t.gross_pnl == pytest.approx(-(im - funding_paid))
    assert t.net_pnl == pytest.approx(-im - t.fees, rel=1e-12)
    assert res.metrics["n_liquidations"] == 1
    assert res.metrics["final_equity"] == pytest.approx(BALANCE - im - entry_fee, rel=1e-12)
    assert_cash_consistent(res)


# ---------------------------------------------------------------------------------------------
# Sizing counters
# ---------------------------------------------------------------------------------------------


def test_entries_capped_by_notional_counted(ohlc_factory, btc_filters) -> None:
    df = ohlc_factory([FLAT] * 10)
    signals = {1: LONG, 4: CLOSE, 6: LONG}

    capped = run(df, ScriptedStrategy(signals), btc_filters, risk=make_risk(max_position_notional=2_000.0))
    assert len(capped.trades) == 2
    assert all(t.qty == pytest.approx(0.04) for t in capped.trades)  # 2000 / 50000
    assert capped.metrics["entries_capped_by_notional"] == 2
    assert capped.metrics["rejected_entries"] == 0

    uncapped = run(df, ScriptedStrategy(signals), btc_filters)
    assert uncapped.metrics["entries_capped_by_notional"] == 0
    assert all(t.qty == pytest.approx(0.09) for t in uncapped.trades)

    # a cap below the exchange minimum makes every entry a risk rejection (never sized up)
    rejected = run(df, ScriptedStrategy(signals), btc_filters, risk=make_risk(max_position_notional=40.0))
    assert rejected.trades == []
    assert rejected.metrics["rejected_entries"] == 2
    assert rejected.metrics["entries_capped_by_notional"] == 0
    for key in ("rejected_entries", "entries_capped_by_notional", "funding_events"):
        assert type(rejected.metrics[key]) is int


# ---------------------------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------------------------


def test_end_of_data_closes_position(ohlc_factory, btc_filters) -> None:
    df = ohlc_factory([FLAT, FLAT, FLAT, FLAT, (50_000.0, 50_310.0, 49_990.0, 50_300.0)])
    res = run(df, ScriptedStrategy({1: LONG}), btc_filters)
    (t,) = res.trades
    assert t.exit_reason is ExitReason.END_OF_DATA
    assert t.exit_time == ct(df, 4)
    assert t.exit_price == pytest.approx(50_300.0 * (1 - SLIP))
    assert t.fees == pytest.approx(t.qty * t.entry_price * TAKER + t.qty * t.exit_price * TAKER)
    assert res.equity["position_qty"].iat[-1] == 0.0
    assert bool(res.equity["in_position"].iat[-1]) is True
    assert res.equity["equity"].iat[-1] == pytest.approx(res.metrics["final_equity"])
    assert_cash_consistent(res)

    short = run(df, ScriptedStrategy({1: SHORT}), btc_filters)
    assert short.trades[0].exit_price == pytest.approx(50_300.0 * (1 + SLIP))
    assert short.trades[0].exit_reason is ExitReason.END_OF_DATA


def test_equity_curve_length_and_start(ohlc_factory, btc_filters) -> None:
    df = ohlc_factory([FLAT] * 10)
    res = run(df, ScriptedStrategy({5: LONG}, warmup=4), btc_filters)
    eq = res.equity
    assert list(eq.columns) == ["time", "equity", "in_position", "position_qty"]
    assert [str(eq[c].dtype) for c in eq.columns] == ["int64", "float64", "bool", "float64"]
    assert len(eq) == 10 - 3  # from s = warmup_bars - 1 to the last bar
    assert int(eq["time"].iat[0]) == ot(df, 3) == res.start_time
    assert eq["time"].tolist() == df["open_time"].tolist()[3:]
    assert res.end_time == ct(df, 9)
    assert eq["equity"].iat[0] == BALANCE
    assert eq["in_position"].tolist() == [False, False, False, True, True, True, True]
    assert eq["position_qty"].iat[3] == pytest.approx(res.trades[0].qty)
    assert res.metrics["bars"] == len(eq)
    assert res.metrics["initial_balance"] == BALANCE

    # ATR stops: the first decision bar also waits for the ATR period
    atr_risk = make_risk(stop_loss=StopLossConfig(mode="atr", percent=2.0, atr_period=5, atr_multiple=2.0))
    res_atr = run(df, ScriptedStrategy({}, warmup=2), btc_filters, risk=atr_risk)
    assert len(res_atr.equity) == 10 - 5
    assert res_atr.start_time == ot(df, 5)


def test_trade_start_ms_respected(ohlc_factory, btc_filters) -> None:
    df = ohlc_factory([FLAT] * 20)
    funding = funding_frame([(ot(df, 4), 0.01, 50_000.0), (ot(df, 16), 0.0001, 50_000.0)])
    strat = ScriptedStrategy({3: LONG, 12: LONG})
    res = run(df, strat, btc_filters, trade_start_ms=ot(df, 10), funding=funding)
    assert min(strat.calls) == 10  # rows before trade_start only warm up indicators
    assert res.start_time == ot(df, 10)
    assert int(res.equity["time"].iat[0]) == ot(df, 10)
    (t,) = res.trades
    assert t.entry_time == ot(df, 13)
    assert res.metrics["funding_events"] == 1  # only the event during the trade

    # an unaligned start uses the first bar whose open_time >= trade_start_ms
    assert run(df, ScriptedStrategy({}), btc_filters, trade_start_ms=ot(df, 10) - 1).start_time == ot(df, 10)
    assert run(df, ScriptedStrategy({}), btc_filters, trade_start_ms=ot(df, 10) + 1).start_time == ot(df, 11)
    # a start before the data -> the usual warm-up start
    assert run(df, ScriptedStrategy({}, warmup=3), btc_filters, trade_start_ms=T0 - DAY).start_time == ot(df, 2)
    # no bar left to trade -> DataError
    with pytest.raises(DataError, match="not enough candles"):
        run(df, ScriptedStrategy({}), btc_filters, trade_start_ms=ot(df, 19))


def test_deterministic(candle_factory, btc_filters) -> None:
    df = random_walk(candle_factory)
    original = df.copy()
    funding = random_funding(df)
    risk = make_risk(
        stop_loss=StopLossConfig(mode="atr", percent=2.0, atr_period=14, atr_multiple=2.0),
        take_profit_r=2.0, cooldown_bars_after_stop=2, max_daily_loss_pct=3.0,
    )

    def once() -> BacktestResult:
        strat = MACrossStrategy({"fast_period": 5, "slow_period": 20, "ma_type": "SMA"})
        return run(df, strat, btc_filters, risk=risk, funding=funding)

    a, b = once(), once()
    assert len(a.trades) >= 5
    assert a.trades == b.trades
    pd.testing.assert_frame_equal(a.equity, b.equity)
    assert a.metrics == b.metrics
    assert (a.start_time, a.end_time, a.params) == (b.start_time, b.end_time, b.params)
    pd.testing.assert_frame_equal(df, original)  # the input frame is never mutated


def test_future_bars_do_not_change_past_trades(candle_factory, btc_filters) -> None:
    df = random_walk(candle_factory)
    k = 260
    future = df.copy()
    for col in ("open", "high", "low", "close"):
        future.loc[k:, col] = future.loc[k:, col] * 1.07
    future.loc[k + 5 :, "low"] = future.loc[k + 5 :, "low"] * 0.9  # wild future wicks
    funding = random_funding(df)
    risk = make_risk(
        stop_loss=StopLossConfig(mode="atr", percent=2.0, atr_period=14, atr_multiple=2.0),
        take_profit_r=2.0, cooldown_bars_after_stop=2, max_daily_loss_pct=3.0,
    )

    def bt(frame: pd.DataFrame) -> BacktestResult:
        strat = MACrossStrategy({"fast_period": 5, "slow_period": 20, "ma_type": "SMA"})
        return run(frame, strat, btc_filters, risk=risk, funding=funding)

    base, changed = bt(df), bt(future)
    cutoff = ot(df, k)
    past_base = [t for t in base.trades if t.exit_time < cutoff]
    past_changed = [t for t in changed.trades if t.exit_time < cutoff]
    assert len(past_base) >= 3
    assert past_base == past_changed
    mask_a = base.equity["time"] < cutoff
    mask_b = changed.equity["time"] < cutoff
    pd.testing.assert_frame_equal(
        base.equity.loc[mask_a].reset_index(drop=True), changed.equity.loc[mask_b].reset_index(drop=True)
    )
    assert base.trades != changed.trades or base.equity["equity"].iat[-1] != changed.equity["equity"].iat[-1]


# ---------------------------------------------------------------------------------------------
# Extra cases
# ---------------------------------------------------------------------------------------------


def test_atr_stop_uses_decision_bar_atr(ohlc_factory, btc_filters) -> None:
    rows = [
        (50_000.0, 50_200.0, 49_800.0, 50_050.0),
        (50_050.0, 50_300.0, 49_900.0, 50_100.0),
        (50_100.0, 50_250.0, 49_950.0, 50_000.0),
        (50_000.0, 50_400.0, 49_700.0, 50_200.0),
        (50_200.0, 50_350.0, 50_000.0, 50_150.0),
        (50_150.0, 50_300.0, 49_950.0, 50_100.0),  # bar 5: decision bar
        (50_100.0, 53_000.0, 49_900.0, 52_000.0),  # bar 6: fill bar with a huge range (its ATR must not be used)
        (52_000.0, 52_100.0, 51_900.0, 52_000.0),
    ]
    df = ohlc_factory(rows)
    m = 2.0
    risk = make_risk(stop_loss=StopLossConfig(mode="atr", percent=2.0, atr_period=3, atr_multiple=m))
    res = run(df, ScriptedStrategy({5: LONG}), btc_filters, risk=risk)
    (t,) = res.trades
    atr = indicators.atr(df, 3)
    expected = round_protective_price(50_100.0 - m * float(atr.iat[5]), btc_filters.tick_size, entry=50_100.0)
    assert t.initial_stop == pytest.approx(float(expected))
    assert float(atr.iat[6]) != pytest.approx(float(atr.iat[5]))


def test_new_run_id_format() -> None:
    rid = new_run_id(lambda: 1_704_067_200.0)
    assert re.fullmatch(r"bt-20240101-000000-[0-9a-f]{6}", rid)
    assert re.fullmatch(r"bt-\d{8}-\d{6}-[0-9a-f]{6}", new_run_id())
    # same second, random suffix: 16 ids are (practically) all distinct
    assert len({new_run_id(lambda: 0.0) for _ in range(16)}) > 1


def test_result_metadata_and_snapshot(ohlc_factory, btc_filters) -> None:
    df = ohlc_factory([FLAT] * 6)
    snapshot = {"mode": "paper", "funding_coverage": {"included": False, "rows": 0, "first": None, "last": None}}
    res = run(df, MACrossStrategy({"fast_period": 2, "slow_period": 3, "ma_type": "SMA"}), btc_filters,
              config_snapshot=snapshot)
    assert res.run_id == RUN_ID
    assert res.symbol == "BTCUSDT" and res.interval == "1h" and res.strategy == "ma_cross"
    assert res.params == {"fast_period": 2, "slow_period": 3, "ma_type": "SMA", "allow_short": True}
    assert res.config == snapshot
    assert res.initial_balance == BALANCE
    assert type(res.created_at) is int and type(res.start_time) is int and type(res.end_time) is int
    for key in ("rejected_entries", "entries_capped_by_notional", "funding_events"):
        assert key in res.metrics
    # default snapshot (no CLI): JSON-able
    res_default = run(df, ScriptedStrategy({}), btc_filters)
    json.dumps(res_default.config, allow_nan=False)
    assert res_default.config["symbol"] == "BTCUSDT"


def test_engine_result_saves_through_report(ohlc_factory, btc_filters, tmp_path: Path, storage: Storage) -> None:
    df = ohlc_factory([FLAT, FLAT, (50_000.0, 50_010.0, 48_900.0, 49_100.0), FLAT, FLAT, FLAT])
    res = run(df, ScriptedStrategy({1: LONG, 3: SHORT}), btc_filters)
    out = save_backtest_result(res, tmp_path / "backtests", storage)
    payload = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert payload["metrics"]["n_trades"] == len(res.trades) == 2
    assert payload["metrics"]["funding_events"] == 0
    assert len(storage.list_trades(source="backtest", run_id=RUN_ID)) == 2
    assert len(storage.backtest_equity(RUN_ID)) == len(res.equity)


def test_invalid_inputs_rejected(ohlc_factory, btc_filters) -> None:
    df = ohlc_factory([FLAT] * 5)
    with pytest.raises(DataError):
        run(df.iloc[::-1].reset_index(drop=True), ScriptedStrategy({}), btc_filters)  # unsorted
    with pytest.raises(DataError, match="not enough candles"):
        run(df.iloc[:1].reset_index(drop=True), ScriptedStrategy({}), btc_filters)
    with pytest.raises(DataError):
        run(df, ScriptedStrategy({}), btc_filters, interval="4h")  # close_time does not match the interval
    with pytest.raises(ValueError):
        run(df, ScriptedStrategy({}), btc_filters, initial_balance=0.0)
