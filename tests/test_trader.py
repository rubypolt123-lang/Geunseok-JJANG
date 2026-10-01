"""Trader loop tests (SPEC §10, §14.2 U6).

FakeBroker implements ``Broker`` with scripted outcomes, FakeMarket serves ``candle_factory`` data relative to a
``FakeClock`` (closed candles + the forming one), Storage is real. No test touches the network.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from bot import trader as trader_mod
from bot.backtest.engine import run_backtest
from bot.broker.base import Broker
from bot.broker.exchange_broker import ExchangeBroker
from bot.broker.paper import PaperBroker
from bot.config import MAINNET_REST_URL, TESTNET_REST_URL, AppConfig
from bot.errors import (
    AuthError,
    BotError,
    ConfigError,
    EmergencyError,
    LiveTradingNotConfirmed,
    ProtectionFailedError,
    TransientError,
)
from bot.exchange.filters import round_protective_price
from bot.exchange.market import empty_funding_df
from bot.exchange.rest import BinanceRestClient
from bot.fillmodel import FillModel
from bot.models import (
    AccountSnapshot,
    Action,
    ActiveTrade,
    BotState,
    Direction,
    ExitReason,
    Mode,
    OpenOutcome,
    OrderPurpose,
    OrderResult,
    OrderStatus,
    OrderType,
    Position,
    PositionClosure,
    ProtectiveOrder,
    Signal,
    SignalAction,
    SymbolFilters,
    SyncResult,
    TradePlan,
    candles_from_df,
    make_client_id,
    symbol_tag,
)
from bot.risk import DailyLossKillSwitch
from bot.storage import Storage
from bot.strategy.base import Strategy
from bot.strategy.ma_cross import MACrossStrategy
from bot.timeutil import floor_time, next_close_ms, now_ms, utc_day
from bot.trader import (
    ABORTED_MESSAGE,
    ONCE_MESSAGE,
    USER_STOP_MESSAGE,
    IterationReport,
    SingleInstanceLock,
    Trader,
    active_trade_key,
    build_trader,
    cooldown_key,
    emergency_key,
    halted_key,
    kill_switch_key,
    last_bar_key,
)
from tests.conftest import FakeClock

H = 3_600_000
START_MS = 1_790_726_400_000  # 2026-09-30T00:00:00Z (a UTC day start)
SYMBOL = "BTCUSDT"
BOT_ID = "mab1"
FORMING = 20  # default layout: bars 0..19 closed, bar 20 forming
BAR = START_MS + 19 * H  # last closed bar (decision bar)
ENTRY_BAR = START_MS + 20 * H  # forming bar == entry bar
DELAY_MS = 3000
PERCENT_STOP = {"mode": "percent", "percent": 2.0}


# ---------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------


class FakeClient:
    """The part of ``BinanceRestClient`` the trader uses: server time (local clock + offset, no HTTP)."""

    def __init__(self, clock: FakeClock, offset_ms: int = 0) -> None:
        self._clock = clock
        self.offset_ms = offset_ms
        self.has_credentials = False
        self.base_url = MAINNET_REST_URL
        self.closed = False

    def server_time_ms(self) -> int:
        return now_ms(self._clock) + self.offset_ms

    def close(self) -> None:
        self.closed = True


class FakeMarket:
    """Serves a candle frame relative to the clock: closed bars (close_time < now) and the forming bar."""

    def __init__(
        self, df: pd.DataFrame, clock: FakeClock, *, offset_ms: int = 0, filters: SymbolFilters | None = None
    ) -> None:
        self.df = df.reset_index(drop=True)
        self.client = FakeClient(clock, offset_ms)
        self.filters = filters
        self.mark: float | None = None
        self.lag_bars = 0  # pretend the newest N closed bars are not published yet
        self.lag_queue: list[int] = []  # per-call lag (consumed first)
        self.fail_on_call: dict[int, BaseException] = {}  # 1-based recent_klines call -> exception
        self.kline_calls: list[int] = []  # server time of every recent_klines call
        self.mark_calls = 0

    def recent_klines(self, symbol: str, interval: str, limit: int) -> tuple[pd.DataFrame, Any, int]:
        server_now = self.client.server_time_ms()
        self.kline_calls.append(server_now)
        exc = self.fail_on_call.get(len(self.kline_calls))
        if exc is not None:
            raise exc
        df = self.df
        closed = df.loc[df["close_time"] < server_now]
        lag = self.lag_queue.pop(0) if self.lag_queue else self.lag_bars
        if lag:
            closed = closed.iloc[: max(0, len(closed) - lag)]
        closed = closed.tail(int(limit)).reset_index(drop=True)
        rows = df.loc[(df["open_time"] <= server_now) & (df["close_time"] >= server_now)]
        forming = candles_from_df(rows)[0] if len(rows) else None
        return closed, forming, server_now

    def mark_price(self, symbol: str) -> float:
        self.mark_calls += 1
        return float(self.mark if self.mark is not None else self.df["close"].iloc[-1])

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        assert self.filters is not None
        return self.filters

    def funding_rates(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        return empty_funding_df()

    def premium_index(self, symbol: str) -> dict[str, float | int]:
        return {"mark_price": self.mark_price(symbol), "last_funding_rate": 0.0, "next_funding_time": 0, "time": 0}


class ScriptedStrategy(Strategy):
    """Returns a scripted action per bar open time (``default`` otherwise); records what ``generate`` saw."""

    name = "scripted"

    def __init__(
        self,
        actions: dict[int, SignalAction] | None = None,
        *,
        default: SignalAction = SignalAction.NONE,
        warmup: int = 3,
    ) -> None:
        self.actions = dict(actions or {})
        self.default = default
        self._warmup = warmup
        self.generated_on: list[int] = []
        self.fail_with: BaseException | None = None
        super().__init__({})

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {}

    @property
    def warmup_bars(self) -> int:
        return self._warmup

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.copy()

    def signal_at(self, prepared: pd.DataFrame, i: int) -> Signal:
        t = int(prepared["open_time"].iloc[i])
        return Signal(self.actions.get(t, self.default), t, float(prepared["close"].iloc[i]), reason="scripted")

    def generate(self, df: pd.DataFrame) -> Signal:
        self.generated_on.append(int(df["open_time"].iloc[-1]))
        if self.fail_with is not None:
            raise self.fail_with
        return super().generate(df)


def _protective(kind: OrderPurpose, client_id: str, price: float, side: Any, status: str = "NEW") -> ProtectiveOrder:
    return ProtectiveOrder(
        kind=kind,
        client_id=client_id,
        exchange_id="900",
        side=side,
        trigger_price=float(price),
        status=status,
        close_position=True,
        quantity=None,
    )


class FakeBroker(Broker):
    """Stateful scripted broker: one position, protective orders, a cash balance; records every call."""

    def __init__(self, mode: Mode, filters: SymbolFilters, *, cash: float = 10_000.0, clock: FakeClock) -> None:
        self.mode = Mode(mode)
        self.filters = filters
        self.cash = float(cash)
        self.clock = clock
        self.position: Position | None = None
        self.mark: float | None = None
        self.protective: list[ProtectiveOrder] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.max_notional_value: float | None = None
        self.extra_issues: list[tuple[str, ...]] = []  # appended to consecutive syncs
        self.closure_hook: Callable[[FakeBroker, ActiveTrade | None, int], PositionClosure | None] | None = None
        self.close_errors: list[BaseException] = []
        self.open_error: BaseException | None = None
        self.ensure_error: BaseException | None = None
        self.on_open: Callable[[FakeBroker], None] | None = None
        self.on_close: Callable[[FakeBroker], None] | None = None
        self._order_id = 5000

    # helpers -------------------------------------------------------------------------------
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def calls_named(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]

    def set_position(
        self,
        direction: Direction,
        qty: float,
        entry: float,
        *,
        mark: float | None = None,
        leverage: int = 3,
        sl_id: str | None = None,
        stop: float | None = None,
        updated_at: int = ENTRY_BAR,
    ) -> None:
        self.position = Position(
            symbol=SYMBOL,
            qty=direction.sign * float(qty),
            entry_price=float(entry),
            mark_price=float(mark or entry),
            unrealized_pnl=0.0,
            liquidation_price=None,
            isolated_margin=None,
            leverage=leverage,
            updated_at=updated_at,
        )
        self.mark = float(mark or entry)
        self.protective = (
            []
            if sl_id is None
            else [_protective(OrderPurpose.STOP_LOSS, sl_id, stop or entry * 0.98, direction.closing_side)]
        )

    def account(self) -> AccountSnapshot:
        pos = self.position
        upnl = 0.0
        if pos is not None and self.mark is not None:
            upnl = pos.qty * (self.mark - pos.entry_price)
            pos = dataclasses.replace(pos, mark_price=self.mark, unrealized_pnl=upnl)
        return AccountSnapshot(
            ts=now_ms(self.clock),
            wallet_balance=self.cash,
            equity=self.cash + upnl,
            available_balance=self.cash,
            unrealized_pnl=upnl,
            position=pos,
            protective_orders=tuple(self.protective),
        )

    # Broker API ----------------------------------------------------------------------------
    def prepare_symbol(self, symbol: str, leverage: int) -> SymbolFilters:
        self.calls.append(("prepare_symbol", {"symbol": symbol, "leverage": leverage}))
        return self.filters

    def sync(self, symbol: str, active: ActiveTrade | None, closed_candles: Any) -> SyncResult:
        n = len(closed_candles)
        self.calls.append(
            ("sync", {"active": None if active is None else active.trade_id, "n_candles": n, "t": now_ms(self.clock)})
        )
        closure = self.closure_hook(self, active, n) if self.closure_hook is not None else None
        if closure is not None:
            self.position = None
            self.protective = []
        issues: list[str] = []
        pos = self.position
        if active is None and pos is not None:
            issues.append("UNTRACKED_POSITION")
        if active is not None and pos is not None and abs(abs(pos.qty) - active.qty) > 1e-12:
            issues.append("QTY_MISMATCH")
        if pos is not None and not any(
            o.kind is OrderPurpose.STOP_LOSS and o.status in ("NEW", "TRIGGERING") for o in self.protective
        ):
            issues.append("SL_MISSING")
        if self.extra_issues:
            issues.extend(self.extra_issues.pop(0))
        return SyncResult(account=self.account(), closure=closure, issues=tuple(issues))

    def open_position(
        self,
        plan: TradePlan,
        *,
        entry_client_id: str,
        sl_client_id: str,
        tp_client_id: str | None,
        ref_price: float,
        bar_time: int,
    ) -> OpenOutcome:
        self.calls.append(
            (
                "open_position",
                {
                    "plan": plan,
                    "entry_client_id": entry_client_id,
                    "sl_client_id": sl_client_id,
                    "tp_client_id": tp_client_id,
                    "ref_price": ref_price,
                    "bar_time": bar_time,
                },
            )
        )
        if self.on_open is not None:
            self.on_open(self)
        if self.open_error is not None:
            raise self.open_error
        qty = float(plan.qty)
        fee = qty * ref_price * 0.0005
        self.cash -= fee
        self.position = Position(
            symbol=SYMBOL,
            qty=plan.direction.sign * qty,
            entry_price=float(ref_price),
            mark_price=float(ref_price),
            unrealized_pnl=0.0,
            liquidation_price=plan.liquidation_price,
            isolated_margin=None,
            leverage=plan.leverage,
            updated_at=bar_time,
        )
        self.mark = float(ref_price)
        self._order_id += 1
        order = OrderResult(
            client_id=entry_client_id,
            exchange_id=str(self._order_id),
            symbol=plan.symbol,
            side=plan.direction.opening_side,
            order_type=OrderType.MARKET,
            purpose=OrderPurpose.ENTRY,
            status=OrderStatus.FILLED,
            requested_qty=qty,
            executed_qty=qty,
            avg_price=float(ref_price),
            trigger_price=None,
            fee=fee,
            ts=bar_time,
        )
        closing = plan.direction.closing_side
        self.protective = [_protective(OrderPurpose.STOP_LOSS, sl_client_id, float(plan.stop_price), closing)]
        self.calls.append(("place_sl", {"client_id": sl_client_id}))
        if tp_client_id is not None and plan.take_profit_price is not None:
            self.protective.append(
                _protective(OrderPurpose.TAKE_PROFIT, tp_client_id, float(plan.take_profit_price), closing)
            )
        return OpenOutcome(
            filled=True,
            qty=qty,
            avg_price=float(ref_price),
            entry_fee=fee,
            entry_time=bar_time,
            entry_order=order,
            protective=tuple(self.protective),
        )

    def close_position(
        self,
        symbol: str,
        active: ActiveTrade | None,
        *,
        reason: ExitReason,
        client_id: str,
        ref_price: float | None,
        bar_time: int,
    ) -> PositionClosure | None:
        self.calls.append(
            (
                "close_position",
                {
                    "reason": reason,
                    "client_id": client_id,
                    "ref_price": ref_price,
                    "bar_time": bar_time,
                    "active": None if active is None else active.trade_id,
                },
            )
        )
        if self.on_close is not None:
            self.on_close(self)
        if self.close_errors:
            raise self.close_errors.pop(0)
        pos = self.position
        self.protective = []
        if pos is None:
            return None
        exit_price = float(ref_price) if ref_price is not None else float(self.mark or pos.entry_price)
        qty = abs(pos.qty)
        gross = pos.qty * (exit_price - pos.entry_price)
        fee = qty * exit_price * 0.0005
        self.cash += gross - fee
        self.position = None
        return PositionClosure(
            exit_time=bar_time,
            exit_price=exit_price,
            qty=qty,
            reason=ExitReason(reason),
            exit_fee=fee,
            funding=0.0,
            gross_pnl=gross,
        )

    def ensure_protection(
        self, active: ActiveTrade, account: AccountSnapshot, *, sl_client_id: str, tp_client_id: str | None
    ) -> tuple[ProtectiveOrder, ...]:
        self.calls.append(
            ("ensure_protection", {"sl_client_id": sl_client_id, "tp_client_id": tp_client_id, "t": now_ms(self.clock)})
        )
        if self.ensure_error is not None:
            raise self.ensure_error
        closing = active.direction.closing_side
        new = [_protective(OrderPurpose.STOP_LOSS, sl_client_id, active.stop_price, closing)]
        if tp_client_id is not None and active.take_profit_price is not None:
            new.append(_protective(OrderPurpose.TAKE_PROFIT, tp_client_id, active.take_profit_price, closing))
        self.protective = new
        return tuple(new)

    def max_notional(self, symbol: str) -> float | None:
        return self.max_notional_value


# ---------------------------------------------------------------------------------------------
# Rig helpers
# ---------------------------------------------------------------------------------------------


def make_cfg(app_config: AppConfig, *, mode: Mode = Mode.PAPER, stop: dict | None = None, **risk: Any) -> AppConfig:
    r = app_config.risk
    if stop:
        r = dataclasses.replace(r, stop_loss=dataclasses.replace(r.stop_loss, **stop))
    if risk:
        r = dataclasses.replace(r, **risk)
    return dataclasses.replace(app_config, mode=Mode(mode), risk=r)


def at_bar(clock: FakeClock, idx: int, *, offset_ms: int = 0, extra_ms: int = 0) -> None:
    """Place the clock so that bar ``idx`` is forming: server time = its open + 3 s (+ extra).

    Bar ``idx`` opens at ``START_MS + idx * H`` (it may lie beyond the frame: then there is no forming candle).
    """
    server = START_MS + idx * H + DELAY_MS + extra_ms
    clock.now_s = (server - offset_ms) / 1000


@dataclass
class Rig:
    cfg: AppConfig
    df: pd.DataFrame
    clock: FakeClock
    market: FakeMarket
    broker: Any
    strategy: Strategy
    trader: Trader
    storage: Storage
    sleep: Callable[[float], None]

    def at(self, idx: int, *, extra_ms: int = 0) -> None:
        at_bar(self.clock, idx, offset_ms=self.market.client.offset_ms, extra_ms=extra_ms)

    def new_trader(self) -> Trader:
        self.trader = Trader(
            self.cfg,
            broker=self.broker,
            market=self.market,
            strategy=self.strategy,
            storage=self.storage,
            clock=self.clock,
            sleep=self.sleep,
        )
        return self.trader

    def open_time(self, idx: int) -> int:
        return int(self.df["open_time"].iloc[idx])


@pytest.fixture
def rig_factory(
    app_config: AppConfig,
    storage: Storage,
    fixed_clock: Callable[..., FakeClock],
    candle_factory: Callable[..., pd.DataFrame],
    btc_filters: SymbolFilters,
) -> Callable[..., Rig]:
    def make(
        *,
        mode: Mode = Mode.PAPER,
        closes: list[float] | None = None,
        opens: list[float] | None = None,
        n_bars: int = 30,
        forming: int = FORMING,
        strategy: Strategy | None = None,
        offset_ms: int = 0,
        stop: dict | None = PERCENT_STOP,
        risk: dict | None = None,
        sleep: Callable[[float], None] | None = None,
        real_paper: bool = False,
        cfg: AppConfig | None = None,
    ) -> Rig:
        clock = fixed_clock()
        the_cfg = cfg if cfg is not None else make_cfg(app_config, mode=mode, stop=stop, **(risk or {}))
        df = candle_factory(closes if closes is not None else [100.0] * n_bars, start_ms=START_MS, opens=opens)
        market = FakeMarket(df, clock, offset_ms=offset_ms, filters=btc_filters)
        at_bar(clock, forming, offset_ms=offset_ms)
        broker: Any
        if real_paper:
            broker = PaperBroker(
                market=market,  # type: ignore[arg-type]
                fill_model=FillModel.from_config(the_cfg.execution),
                storage=storage,
                initial_balance=10_000.0,
                include_funding=False,
                clock=clock,
                sleep=clock.sleep,
            )
        else:
            broker = FakeBroker(Mode(the_cfg.mode), btc_filters, clock=clock)
        strat = strategy if strategy is not None else ScriptedStrategy()
        the_sleep = sleep if sleep is not None else clock.sleep
        trader = Trader(
            the_cfg, broker=broker, market=market, strategy=strat, storage=storage, clock=clock, sleep=the_sleep
        )
        return Rig(the_cfg, df, clock, market, broker, strat, trader, storage, the_sleep)

    return make


def make_active(
    mode: Mode,
    direction: Direction,
    *,
    qty: float = 10.0,
    entry: float = 100.0,
    entry_bar: int = BAR,
    stop: float | None = None,
    tp: float | None = None,
    protect_seq: int = 1,
    order_id: str | None = "77",
) -> ActiveTrade:
    if stop is None:
        stop = 98.0 if direction is Direction.LONG else 102.0
    return ActiveTrade(
        trade_id=f"{mode.value}-{SYMBOL}-{entry_bar}-{'L' if direction is Direction.LONG else 'S'}",
        symbol=SYMBOL,
        direction=direction,
        qty=qty,
        entry_price=entry,
        entry_time=entry_bar,
        entry_bar_open_time=entry_bar,
        stop_price=stop,
        take_profit_price=tp,
        liquidation_price=None,
        leverage=3,
        risk_amount=qty * 2.2,
        entry_fee=0.5,
        entry_client_id=make_client_id(BOT_ID, SYMBOL, "EN", entry_bar - H),
        protect_seq=protect_seq,
        entry_order_id=order_id,
    )


def hold(rig: Rig, active: ActiveTrade, *, mark: float | None = None, with_sl: bool = True) -> None:
    """Persist ``active`` and give the broker the matching position (with its current-generation SL)."""
    rig.storage.set_state(active_trade_key(rig.cfg.mode, SYMBOL), active.to_dict())
    sl_id = make_client_id(BOT_ID, SYMBOL, "SL", active.entry_bar_open_time, active.protect_seq)
    rig.broker.set_position(
        active.direction,
        active.qty,
        active.entry_price,
        mark=mark,
        sl_id=sl_id if with_sl else None,
        stop=active.stop_price,
        updated_at=active.entry_time,
    )


def event_kinds(storage: Storage) -> list[str]:
    return [e["kind"] for e in storage.recent_events(limit=500)]


# ---------------------------------------------------------------------------------------------
# Iteration basics
# ---------------------------------------------------------------------------------------------


def test_once_processes_last_closed_bar(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.trader.startup()
    report = rig.trader.run_once()

    assert isinstance(report, IterationReport)
    assert report.bar_open_time == BAR and isinstance(report.bar_open_time, int)
    assert report.signal is SignalAction.NONE and report.action is Action.NONE
    assert not report.executed and report.skipped_reason is None and report.closure is None
    assert report.server_time == ENTRY_BAR + DELAY_MS
    assert rig.strategy.generated_on == [BAR]  # the strategy never sees the forming candle
    assert rig.storage.get_state(last_bar_key(Mode.PAPER, SYMBOL, "1h")) == BAR
    signals = rig.storage.recent_signals("paper")
    assert signals[0]["bar_open_time"] == BAR and signals[0]["decided_action"] == "NONE"
    candles = rig.storage.get_candles(SYMBOL, "1h", limit=1000)
    assert candles[-1]["open_time"] == BAR and len(candles) == 20  # closed candles only
    syncs = rig.broker.calls_named("sync")
    assert [s["n_candles"] for s in syncs] == [20, 20]  # startup reconcile + step 5; nothing executed -> no re-read
    assert rig.storage.equity_curve("paper")[-1]["time"] == BAR


def test_same_bar_not_processed_twice(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(strategy=ScriptedStrategy({BAR: SignalAction.LONG}))
    rig.trader.startup()
    first = rig.trader.run_once()
    second = rig.trader.run_once()
    assert first.action is Action.OPEN_LONG and first.executed
    assert second.skipped_reason == "already_processed" and second.action is Action.NONE
    assert rig.strategy.generated_on == [BAR]
    assert len(rig.broker.calls_named("open_position")) == 1

    # a restart within the same candle does not act again either (last_bar is persisted)
    restarted = rig.new_trader()
    restarted.startup()
    assert restarted.run_once().skipped_reason == "already_processed"
    assert len(rig.broker.calls_named("open_position")) == 1

    # the next candle is processed
    rig.at(FORMING + 1)
    nxt = restarted.run_once()
    assert nxt.skipped_reason is None and nxt.bar_open_time == rig.open_time(FORMING)


def test_stale_data_skips_orders(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(strategy=ScriptedStrategy(default=SignalAction.LONG))
    rig.trader.startup()
    rig.market.lag_bars = 2
    rig.clock.sleeps.clear()
    calls_before = len(rig.market.kline_calls)

    report = rig.trader.run_once()
    assert report.skipped_reason == "stale_data" and report.bar_open_time is None
    assert rig.clock.sleeps == [2.0, 2.0, 2.0]  # 3 retries, 2 s apart
    assert len(rig.market.kline_calls) - calls_before == 4
    assert "open_position" not in rig.broker.names()
    assert rig.strategy.generated_on == []
    assert "STALE_DATA" in event_kinds(rig.storage)
    assert rig.broker.calls_named("sync")[-1]["n_candles"] > 0  # sync + protection still run
    assert rig.storage.get_status()["last_bar_open_time"] is None  # nothing marked as processed

    # data catching up during the retries is processed normally
    rig.market.lag_bars = 0
    rig.market.lag_queue = [1, 1]
    rig.clock.sleeps.clear()
    report = rig.trader.run_once()
    assert rig.clock.sleeps == [2.0, 2.0]
    assert report.skipped_reason is None and report.action is Action.OPEN_LONG

    # too few closed candles for the strategy warmup is stale too
    rig2 = rig_factory(strategy=ScriptedStrategy(default=SignalAction.LONG, warmup=40))
    rig2.trader.startup()
    report2 = rig2.trader.run_once()
    assert report2.skipped_reason == "stale_data"
    assert "open_position" not in rig2.broker.names()


def test_forming_candle_open_used_as_ref_price(rig_factory: Callable[..., Rig]) -> None:
    closes = [100.0] * 30
    opens = [100.0] * 30
    opens[FORMING] = 101.3  # the forming candle gaps up
    rig = rig_factory(closes=closes, opens=opens, strategy=ScriptedStrategy({BAR: SignalAction.LONG}))
    rig.trader.startup()
    rig.trader.run_once()
    call = rig.broker.calls_named("open_position")[0]
    assert call["ref_price"] == pytest.approx(101.3)
    assert call["plan"].ref_price == pytest.approx(101.3)
    assert call["bar_time"] == ENTRY_BAR
    assert rig.market.mark_calls == 0

    # no forming candle for the entry bar -> the mark price is used, fetched once per iteration
    # (both rigs share the storage fixture: drop rig's open trade so rig2 starts flat)
    rig.storage.delete_state(active_trade_key(rig.cfg.mode, SYMBOL))
    rig2 = rig_factory(n_bars=21, strategy=ScriptedStrategy({START_MS + 20 * H: SignalAction.LONG}), forming=21)
    rig2.market.mark = 99.0
    rig2.trader.startup()
    rig2.trader.run_once()
    call2 = rig2.broker.calls_named("open_position")[0]
    assert call2["ref_price"] == pytest.approx(99.0) and call2["plan"].ref_price == pytest.approx(99.0)
    assert rig2.market.mark_calls == 1


def test_open_long_on_golden_cross_saves_active_trade(rig_factory: Callable[..., Rig]) -> None:
    closes = [100.0] * 15 + [99.0, 98.0, 97.0, 96.0, 110.0] + [110.0] * 10  # golden cross at bar 19
    strategy = MACrossStrategy({"fast_period": 2, "slow_period": 4, "ma_type": "SMA"})
    rig = rig_factory(mode=Mode.TESTNET, closes=closes, strategy=strategy)
    rig.trader.startup()
    report = rig.trader.run_once()

    assert report.signal is SignalAction.LONG and report.action is Action.OPEN_LONG and report.executed
    call = rig.broker.calls_named("open_position")[0]
    en_id = make_client_id(BOT_ID, SYMBOL, "EN", BAR)
    sl_id = make_client_id(BOT_ID, SYMBOL, "SL", ENTRY_BAR, 1)
    tp_id = make_client_id(BOT_ID, SYMBOL, "TP", ENTRY_BAR, 1)
    assert (call["entry_client_id"], call["sl_client_id"], call["tp_client_id"]) == (en_id, sl_id, tp_id)
    for cid in (en_id, sl_id, tp_id):
        assert f"-{symbol_tag(SYMBOL)}-" in cid  # ids carry the symbol tag

    active = rig.trader.active
    assert active is not None
    assert active.trade_id == f"testnet-{SYMBOL}-{ENTRY_BAR}-L"
    assert active.direction is Direction.LONG
    assert active.entry_order_id == "5001"
    assert active.protect_seq == 1
    assert active.entry_client_id == en_id
    assert active.entry_bar_open_time == ENTRY_BAR
    assert active.stop_price == pytest.approx(float(call["plan"].stop_price))
    assert active.qty == pytest.approx(float(call["plan"].qty))
    saved = rig.storage.get_state(active_trade_key(Mode.TESTNET, SYMBOL))
    assert saved == active.to_dict()
    assert ActiveTrade.from_dict(saved).entry_order_id == "5001"
    orders = rig.storage.recent_orders("testnet")
    assert orders[0]["client_id"] == en_id and orders[0]["exchange_id"] == "5001"
    assert rig.broker.names()[-3:] == ["open_position", "place_sl", "sync"]  # post-execution re-read
    assert "ENTRY" in event_kinds(rig.storage)


# ---------------------------------------------------------------------------------------------
# Flips, closes, closures
# ---------------------------------------------------------------------------------------------


def test_flip_closes_verifies_flat_then_opens(rig_factory: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch) -> None:
    rig = rig_factory(mode=Mode.TESTNET, strategy=ScriptedStrategy({BAR: SignalAction.LONG}))
    short = make_active(Mode.TESTNET, Direction.SHORT, qty=10.0, entry=100.0, entry_bar=START_MS + 10 * H)
    hold(rig, short, mark=95.0)  # +50 unrealized for the short; the close at 100 realizes 0 minus the fee
    equities: list[float] = []
    real_plan_entry = trader_mod.plan_entry

    def spy(**kwargs: Any) -> Any:
        equities.append(kwargs["equity"])
        return real_plan_entry(**kwargs)

    monkeypatch.setattr(trader_mod, "plan_entry", spy)
    rig.trader.startup()
    pre_close_equity = rig.broker.account().equity
    start = len(rig.broker.calls)
    report = rig.trader.run_once()

    assert report.action is Action.FLIP_LONG and report.executed
    names = rig.broker.names()[start:]
    i_close = names.index("close_position")
    i_verify = names.index("sync", i_close)
    i_open = names.index("open_position")
    assert names[0] == "sync" and i_close < i_verify < i_open
    calls = rig.broker.calls[start:]
    assert calls[i_close][1]["reason"] is ExitReason.FLIP
    assert calls[i_close][1]["client_id"] == make_client_id(BOT_ID, SYMBOL, "EX", BAR)
    assert calls[i_verify][1]["n_candles"] == 0 and calls[i_verify][1]["active"] is None
    # the open leg is sized from the post-close account (cash after the exit fee), not the pre-close equity
    post_close_equity = 10_000.0 - 10.0 * 100.0 * 0.0005
    assert pre_close_equity == pytest.approx(10_050.0)
    assert equities == [pytest.approx(post_close_equity)]
    trades = rig.storage.list_trades(source="testnet")
    assert len(trades) == 1 and trades[0]["exit_reason"] == "FLIP" and trades[0]["direction"] == "SHORT"
    assert rig.trader.active is not None and rig.trader.active.direction is Direction.LONG


def test_flip_blocked_becomes_close_only(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(mode=Mode.TESTNET, strategy=ScriptedStrategy({BAR: SignalAction.LONG}))
    hold(rig, make_active(Mode.TESTNET, Direction.SHORT, entry_bar=START_MS + 10 * H))
    rig.cfg.halt_path.parent.mkdir(parents=True, exist_ok=True)
    rig.cfg.halt_path.write_text("", encoding="utf-8")
    rig.trader.startup()
    report = rig.trader.run_once()

    assert report.action is Action.CLOSE
    assert report.skipped_reason == "entries_blocked:halt_file"
    closes = rig.broker.calls_named("close_position")
    assert len(closes) == 1 and closes[0]["reason"] is ExitReason.SIGNAL
    assert "open_position" not in rig.broker.names()
    assert rig.storage.list_trades(source="testnet")[0]["exit_reason"] == "SIGNAL"
    assert rig.trader.active is None


def test_closure_recorded_and_cooldown_after_stop(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(mode=Mode.TESTNET, strategy=ScriptedStrategy(default=SignalAction.LONG))
    active = make_active(Mode.TESTNET, Direction.LONG, qty=10.0, entry=100.0, entry_bar=START_MS + 15 * H)
    hold(rig, active)
    rig.trader.startup()
    exit_time = BAR + 1_800_000  # the stop filled inside bar 19
    fired: list[bool] = []

    def hook(b: FakeBroker, act: ActiveTrade | None, n: int) -> PositionClosure | None:
        if act is not None and n > 0 and not fired:
            fired.append(True)
            return PositionClosure(
                exit_time=exit_time,
                exit_price=98.0,
                qty=act.qty,
                reason=ExitReason.STOP_LOSS,
                exit_fee=0.49,
                funding=0.2,
                gross_pnl=None,
            )
        return None

    rig.broker.closure_hook = hook
    report = rig.trader.run_once()

    trades = rig.storage.list_trades(source="testnet")
    assert len(trades) == 1
    t = trades[0]
    assert t["trade_id"] == active.trade_id and t["exit_reason"] == "STOP_LOSS"
    assert t["gross_pnl"] == pytest.approx(10.0 * (98.0 - 100.0))
    assert t["net_pnl"] == pytest.approx(-20.0 - (0.5 + 0.49) - 0.2)
    assert report.closure is not None and report.closure.reason is ExitReason.STOP_LOSS
    assert rig.trader.active is None
    assert rig.storage.get_state(active_trade_key(Mode.TESTNET, SYMBOL)) is None
    cooldown = rig.storage.get_state(cooldown_key(Mode.TESTNET, SYMBOL))
    assert cooldown["until_ms"] == floor_time(exit_time, H) + 3 * H
    # the LONG signal at the stop bar is blocked by the cooldown (bars=3: decisions at T, T+1, T+2)
    assert report.skipped_reason == "entries_blocked:cooldown"
    assert "open_position" not in rig.broker.names()
    for idx in (FORMING + 1, FORMING + 2):
        rig.at(idx)
        assert rig.trader.run_once().skipped_reason == "entries_blocked:cooldown"
    rig.at(FORMING + 3)  # decision at T + 3 bars may open
    report = rig.trader.run_once()
    assert report.action is Action.OPEN_LONG and report.executed


def test_closure_from_post_execution_sync_is_recorded(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(mode=Mode.TESTNET, strategy=ScriptedStrategy({BAR: SignalAction.LONG}))
    rig.trader.startup()

    def hook(b: FakeBroker, act: ActiveTrade | None, n: int) -> PositionClosure | None:
        if act is not None and n == 0 and b.position is not None:  # the re-read right after the entry
            return PositionClosure(
                exit_time=ENTRY_BAR + 2_000,
                exit_price=97.0,
                qty=act.qty,
                reason=ExitReason.STOP_LOSS,
                exit_fee=0.1,
                funding=0.0,
                gross_pnl=None,
            )
        return None

    rig.broker.closure_hook = hook
    report = rig.trader.run_once()
    assert report.action is Action.OPEN_LONG and report.executed
    trades = rig.storage.list_trades(source="testnet")
    assert len(trades) == 1 and trades[0]["exit_reason"] == "STOP_LOSS"
    assert trades[0]["trade_id"] == f"testnet-{SYMBOL}-{ENTRY_BAR}-L"
    assert report.closure is not None and report.closure.exit_price == 97.0
    assert rig.trader.active is None
    assert rig.storage.get_state(active_trade_key(Mode.TESTNET, SYMBOL)) is None
    assert rig.storage.get_state(cooldown_key(Mode.TESTNET, SYMBOL))["until_ms"] == ENTRY_BAR + 3 * H


# ---------------------------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------------------------


def _kill_rig(rig_factory: Callable[..., Rig], **kwargs: Any) -> Rig:
    rig = rig_factory(
        mode=Mode.TESTNET, strategy=ScriptedStrategy(default=SignalAction.LONG), risk={"max_daily_loss_pct": 5.0}, **kwargs
    )
    hold(rig, make_active(Mode.TESTNET, Direction.LONG, qty=100.0, entry=100.0, entry_bar=START_MS + 10 * H), mark=100.0)
    return rig


def test_kill_switch_flattens_and_blocks(rig_factory: Callable[..., Rig]) -> None:
    rig = _kill_rig(rig_factory)
    rig.trader.startup()  # seeds the kill switch with equity 10000
    rig.broker.mark = 94.0  # -600 USDT = -6 %
    report = rig.trader.run_once()

    closes = rig.broker.calls_named("close_position")
    assert len(closes) == 1
    assert closes[0]["reason"] is ExitReason.KILL_SWITCH
    assert closes[0]["client_id"] == make_client_id(BOT_ID, SYMBOL, "KS", BAR)
    assert closes[0]["ref_price"] == pytest.approx(100.0)  # forming open
    assert closes[0]["bar_time"] == ENTRY_BAR
    assert rig.storage.list_trades(source="testnet")[0]["exit_reason"] == "KILL_SWITCH"
    events = rig.storage.recent_events(limit=100)
    ks = [e for e in events if e["kind"] == "KILL_SWITCH"]
    assert ks and ks[0]["level"] == "CRITICAL"
    # the LONG signal is blocked; the switch state is persisted
    assert report.skipped_reason == "entries_blocked:kill_switch"
    assert "open_position" not in rig.broker.names()
    assert rig.storage.get_state(kill_switch_key(Mode.TESTNET, SYMBOL))["tripped"] is True
    status = rig.storage.get_status()
    assert status["state"] == "KILL_SWITCH" and status["entries_blocked_reason"] == "kill_switch"


def test_kill_switch_flatten_retried_after_transient_error(rig_factory: Callable[..., Rig]) -> None:
    rig = _kill_rig(rig_factory)
    rig.trader.startup()
    rig.broker.mark = 94.0
    rig.broker.close_errors = [TransientError("gateway timeout", http_status=504, path="/fapi/v1/order")]
    with pytest.raises(TransientError):
        rig.trader.run_once()
    assert rig.broker.position is not None and rig.trader.active is not None

    rig.at(FORMING + 1)  # next iteration: still tripped -> flatten again with this bar's id
    rig.trader.run_once()
    ids = [c["client_id"] for c in rig.broker.calls_named("close_position")]
    assert ids == [make_client_id(BOT_ID, SYMBOL, "KS", BAR), make_client_id(BOT_ID, SYMBOL, "KS", BAR + H)]
    assert rig.broker.position is None
    assert [t["exit_reason"] for t in rig.storage.list_trades(source="testnet")] == ["KILL_SWITCH"]


def test_kill_switch_flattens_after_restart_on_tripped_day(rig_factory: Callable[..., Rig]) -> None:
    rig = _kill_rig(rig_factory)
    tripped = DailyLossKillSwitch(5.0)
    tripped.seed(10_000.0)
    tripped.update(BAR - 1, 9_400.0)  # tripped earlier the same UTC day
    assert tripped.tripped
    rig.storage.set_state(kill_switch_key(Mode.TESTNET, SYMBOL), tripped.to_dict())
    rig.trader.startup()  # restart: equity back at 10000, but the day is still tripped
    report = rig.trader.run_once()

    closes = rig.broker.calls_named("close_position")
    assert len(closes) == 1 and closes[0]["reason"] is ExitReason.KILL_SWITCH
    assert rig.storage.list_trades(source="testnet")[0]["exit_reason"] == "KILL_SWITCH"
    assert "KILL_SWITCH" not in [e["kind"] for e in rig.storage.recent_events(limit=50)]  # not newly tripped
    assert report.skipped_reason == "entries_blocked:kill_switch"


def test_kill_switch_paper_mode_flatten_uses_ref_price(rig_factory: Callable[..., Rig]) -> None:
    closes = [100.0] * 20 + [99.0] * 10  # bar 20 closes 1 % lower (its low stays above the 2 % stop)
    rig = rig_factory(
        closes=closes,
        strategy=ScriptedStrategy({BAR: SignalAction.LONG}),
        risk={"max_daily_loss_pct": 0.4},
        real_paper=True,
    )
    rig.trader.startup()
    first = rig.trader.run_once()
    assert first.action is Action.OPEN_LONG and rig.trader.active is not None

    rig.at(FORMING + 1)
    report = rig.trader.run_once()  # paper close_position needs a ref price: it gets the forming open
    assert rig.trader.kill.tripped
    trades = rig.storage.list_trades(source="paper")
    assert len(trades) == 1 and trades[0]["exit_reason"] == "KILL_SWITCH"
    assert trades[0]["exit_price"] == pytest.approx(99.0 * (1 - 0.0005))
    assert trades[0]["exit_time"] == START_MS + 21 * H
    assert report.closure is not None and report.closure.reason is ExitReason.KILL_SWITCH
    assert rig.trader.active is None


def test_kill_switch_uses_bar_close_time(
    rig_factory: Callable[..., Rig], btc_filters: SymbolFilters, candle_factory: Callable[..., pd.DataFrame]
) -> None:
    # Day D: flat at 100 until 19:00, LONG signal at the 19:00 close, entry at 20:00, then a slow decline. The
    # 23:00 bar (last bar of day D) closes -0.27 % vs. the day baseline -> trips at 0.25 %. With the next bar's
    # open time (00:00 of D+1) the baseline would be the 22:00 equity and nothing would trip.
    closes = [100.0] * 20 + [99.9, 99.7, 99.6, 99.5] + [99.4] * 8
    actions = {BAR: SignalAction.LONG}
    rig = rig_factory(
        closes=closes, strategy=ScriptedStrategy(actions), risk={"max_daily_loss_pct": 0.25}, real_paper=True
    )
    df = rig.df

    engine = run_backtest(
        df,
        ScriptedStrategy(actions),
        symbol=SYMBOL,
        interval="1h",
        filters=btc_filters,
        risk=rig.cfg.risk,
        execution=rig.cfg.execution,
        initial_balance=10_000.0,
        funding=None,
    )
    ks_engine = [t for t in engine.trades if t.exit_reason is ExitReason.KILL_SWITCH]
    assert len(ks_engine) == 1
    assert ks_engine[0].exit_time == rig.open_time(24)

    rig.trader.startup()
    for idx in range(FORMING, 25):
        rig.at(idx)
        rig.trader.run_once()

    trades = rig.storage.list_trades(source="paper")
    assert len(trades) == 1 and trades[0]["exit_reason"] == "KILL_SWITCH"
    assert trades[0]["exit_time"] == ks_engine[0].exit_time
    assert trades[0]["qty"] == pytest.approx(ks_engine[0].qty)
    assert trades[0]["entry_price"] == pytest.approx(ks_engine[0].entry_price)
    assert trades[0]["exit_price"] == pytest.approx(ks_engine[0].exit_price)
    close_23 = int(df["close_time"].iloc[23])
    assert rig.trader.kill.tripped_at == close_23
    assert utc_day(close_23) == utc_day(START_MS)

    # the scenario discriminates: feeding the next bar's open time instead would not trip on that bar
    eq = engine.equity.set_index("time")["equity"]
    alt = DailyLossKillSwitch(0.25)
    alt.seed(10_000.0)
    tripped_alt = [alt.update(int(t) + H, float(eq.loc[t])) for t in eq.index if int(t) <= rig.open_time(23)]
    assert not any(tripped_alt)


# ---------------------------------------------------------------------------------------------
# Halt file, protection failures, emergency
# ---------------------------------------------------------------------------------------------


def test_halt_file_blocks_entries_not_exits(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(
        mode=Mode.TESTNET,
        strategy=ScriptedStrategy({BAR: SignalAction.LONG, BAR + H: SignalAction.CLOSE}),
    )
    rig.cfg.halt_path.parent.mkdir(parents=True, exist_ok=True)
    rig.cfg.halt_path.write_text("", encoding="utf-8")
    rig.trader.startup()
    report = rig.trader.run_once()
    assert report.signal is SignalAction.LONG and report.action is Action.NONE
    assert report.skipped_reason == "entries_blocked:halt_file"
    assert "open_position" not in rig.broker.names()
    assert rig.storage.get_status()["entries_blocked_reason"] == "halt_file"

    # an existing (here: untracked, unprotected) position is still protected and closed by the exit signal
    rig.broker.set_position(Direction.LONG, 5.0, 100.0)
    rig.at(FORMING + 1)
    report = rig.trader.run_once()
    assert "ensure_protection" in rig.broker.names()
    assert report.action is Action.CLOSE
    closes = rig.broker.calls_named("close_position")
    assert len(closes) == 1 and closes[0]["reason"] is ExitReason.SIGNAL
    assert rig.broker.position is None


def test_protection_failure_halts_entries(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(mode=Mode.TESTNET, strategy=ScriptedStrategy(default=SignalAction.LONG))
    entry = OpenOutcome(filled=True, qty=1.0, avg_price=100.05, entry_fee=0.05, entry_time=ENTRY_BAR + 150)
    closure = PositionClosure(
        exit_time=ENTRY_BAR + 900,
        exit_price=99.9,
        qty=1.0,
        reason=ExitReason.PROTECTION_FAILED,
        exit_fee=0.05,
        funding=0.0,
        gross_pnl=-0.15,
    )
    rig.broker.open_error = ProtectionFailedError("stop rejected", flattened=True, closure=closure, entry=entry)
    rig.trader.startup()
    report = rig.trader.run_once()

    assert report.skipped_reason == "protection_failed" and report.executed
    plan = rig.broker.calls_named("open_position")[0]["plan"]
    trades = rig.storage.list_trades(source="testnet")
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "PROTECTION_FAILED"
    assert t["trade_id"] == f"testnet-{SYMBOL}-{ENTRY_BAR}-L"
    assert t["qty"] == pytest.approx(1.0) and t["entry_price"] == pytest.approx(100.05)
    assert t["initial_stop"] == pytest.approx(float(plan.stop_price))
    assert t["net_pnl"] == pytest.approx(-0.15 - 0.1)
    assert rig.trader.active is None
    assert rig.storage.get_state(halted_key(Mode.TESTNET, SYMBOL))["reason"] == "protection_failed"
    events = [e for e in rig.storage.recent_events(limit=50) if e["kind"] == "PROTECTION_FAILED"]
    assert events and events[0]["level"] == "CRITICAL"
    assert rig.storage.get_status()["state"] == "HALTED"

    rig.broker.open_error = None
    rig.at(FORMING + 1)
    report = rig.trader.run_once()
    assert report.skipped_reason == "entries_blocked:halted:protection_failed"
    assert len(rig.broker.calls_named("open_position")) == 1

    # a restart clears the halt
    restarted = rig.new_trader()
    restarted.startup()
    assert restarted.halted is None
    assert rig.storage.get_state(halted_key(Mode.TESTNET, SYMBOL)) is None


def test_emergency_flatten_uses_new_id_each_attempt(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(mode=Mode.TESTNET, strategy=ScriptedStrategy({BAR: SignalAction.LONG}))
    entry = OpenOutcome(filled=True, qty=1.0, avg_price=100.05, entry_fee=0.05, entry_time=ENTRY_BAR + 150)

    def filled_unprotected(b: FakeBroker) -> None:
        b.set_position(Direction.LONG, 1.0, 100.05)  # filled, but no stop

    rig.broker.on_open = filled_unprotected
    rig.broker.open_error = EmergencyError("entry filled but unprotected and flatten failed", entry=entry)
    rig.trader.startup()
    with pytest.raises(EmergencyError):
        rig.trader.run_once()
    em = rig.storage.get_state(emergency_key(Mode.TESTNET, SYMBOL))
    assert em["attempt"] == 0 and em["bar"] == ENTRY_BAR
    assert rig.trader.active is not None and rig.trader.active.qty == pytest.approx(1.0)
    assert rig.storage.get_state(last_bar_key(Mode.TESTNET, SYMBOL, "1h")) == BAR  # persisted even on failure

    # restart: run_forever starts with the emergency flatten; each attempt uses a never-used FL id
    rig.broker.open_error = None
    rig.broker.on_open = None
    rig.broker.ensure_error = EmergencyError("cannot protect")  # startup reconcile cannot protect either
    rig.broker.close_errors = [EmergencyError("still not flat"), TransientError("timeout")]
    seen: list[tuple[int, int]] = []

    def on_close(b: FakeBroker) -> None:
        seen.append((rig.storage.get_state(emergency_key(Mode.TESTNET, SYMBOL))["attempt"], len(rig.clock.sleeps)))

    rig.broker.on_close = on_close
    restarted = rig.new_trader()
    rig.clock.sleeps.clear()
    restarted.run_forever(max_iterations=1)

    ids = [c["client_id"] for c in rig.broker.calls_named("close_position")]
    assert ids == [make_client_id(BOT_ID, SYMBOL, "FL", ENTRY_BAR, n) for n in (1, 2, 3)]
    assert [a for a, _ in seen] == [1, 2, 3]  # attempt counter persisted before each attempt
    assert [s for _, s in seen] == [0, 10, 20]  # 10 s (1 s chunks) between attempts
    assert all(c["reason"] is ExitReason.PROTECTION_FAILED for c in rig.broker.calls_named("close_position"))
    assert rig.storage.get_state(emergency_key(Mode.TESTNET, SYMBOL)) is None
    assert rig.storage.get_state(halted_key(Mode.TESTNET, SYMBOL))["reason"] == "emergency_flatten"
    trades = rig.storage.list_trades(source="testnet")
    assert len(trades) == 1 and trades[0]["exit_reason"] == "PROTECTION_FAILED"
    assert rig.broker.position is None
    status = rig.storage.get_status()
    assert status["state"] == "STOPPED" and status["message"] == ONCE_MESSAGE


# ---------------------------------------------------------------------------------------------
# Adoption and protection
# ---------------------------------------------------------------------------------------------


def test_untracked_position_adopted_and_protected(rig_factory: Callable[..., Rig], btc_filters: SymbolFilters) -> None:
    rig = rig_factory(mode=Mode.TESTNET, stop=None)  # default ATR stop: unavailable between bars -> percent
    rig.trader.startup()
    updated_at = ENTRY_BAR + 600_000
    rig.broker.set_position(Direction.SHORT, 0.5, 100.0, leverage=7, updated_at=updated_at)
    server_now = rig.market.client.server_time_ms()
    rig.trader.protection_check()

    active = rig.trader.active
    assert active is not None
    assert active.trade_id == f"testnet-{SYMBOL}-adopted-{server_now}"
    assert active.direction is Direction.SHORT and active.qty == pytest.approx(0.5)
    assert active.leverage == 7  # from the position
    assert active.entry_client_id == "adopted" and active.entry_order_id is None
    assert active.entry_time == updated_at and active.entry_bar_open_time == floor_time(updated_at, H)
    expected_stop = float(round_protective_price(100.0 * 1.02, btc_filters.tick_size, entry=100.0))
    assert active.stop_price == pytest.approx(expected_stop)
    assert active.take_profit_price == pytest.approx(100.0 - 2.0 * abs(100.0 - expected_stop))
    assert active.risk_amount == pytest.approx(0.5 * abs(100.0 - expected_stop))
    ensure = rig.broker.calls_named("ensure_protection")
    assert len(ensure) == 1
    assert ensure[0]["sl_client_id"] == make_client_id(BOT_ID, SYMBOL, "SL", floor_time(updated_at, H), 1)
    assert ensure[0]["tp_client_id"] == make_client_id(BOT_ID, SYMBOL, "TP", floor_time(updated_at, H), 1)
    assert active.protect_seq == 1
    assert rig.storage.get_state(active_trade_key(Mode.TESTNET, SYMBOL))["protect_seq"] == 1
    adopted = [e for e in rig.storage.recent_events(limit=50) if e["kind"] == "ADOPTED_POSITION"]
    assert adopted and adopted[0]["level"] == "WARNING"


def test_sl_missing_triggers_ensure_protection(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(mode=Mode.TESTNET)
    active = make_active(Mode.TESTNET, Direction.LONG, entry_bar=START_MS + 12 * H, tp=104.0)
    hold(rig, active)
    rig.trader.startup()
    assert "ensure_protection" not in rig.broker.names()
    rig.broker.protective = []  # the SL was cancelled / expired on the exchange
    rig.trader.run_once()

    ensure = rig.broker.calls_named("ensure_protection")
    assert len(ensure) == 1
    assert ensure[0]["sl_client_id"] == make_client_id(BOT_ID, SYMBOL, "SL", START_MS + 12 * H, 2)
    assert ensure[0]["tp_client_id"] == make_client_id(BOT_ID, SYMBOL, "TP", START_MS + 12 * H, 2)
    assert rig.trader.active is not None and rig.trader.active.protect_seq == 2
    assert rig.storage.get_state(active_trade_key(Mode.TESTNET, SYMBOL))["protect_seq"] == 2


def test_protection_qty_mismatch_triggers_ensure_protection(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(mode=Mode.TESTNET)
    hold(rig, make_active(Mode.TESTNET, Direction.SHORT, entry_bar=START_MS + 12 * H))
    rig.trader.startup()
    rig.broker.extra_issues = [("PROTECTION_QTY_MISMATCH",)]
    rig.trader.run_once()
    ensure = rig.broker.calls_named("ensure_protection")
    assert len(ensure) == 1
    assert ensure[0]["sl_client_id"] == make_client_id(BOT_ID, SYMBOL, "SL", START_MS + 12 * H, 2)
    assert ensure[0]["tp_client_id"] is None  # no take-profit planned for this trade
    assert rig.trader.active is not None and rig.trader.active.protect_seq == 2


def test_protection_check_between_bars(rig_factory: Callable[..., Rig]) -> None:
    removed: dict[str, int] = {}
    holder: dict[str, Rig] = {}
    t_remove = ENTRY_BAR + DELAY_MS + 100_000

    def sleeper(s: float) -> None:
        r = holder["rig"]
        r.clock.sleep(s)
        if not removed and now_ms(r.clock) >= t_remove:
            r.broker.protective = []  # the stop disappears while waiting for the next candle
            removed["t"] = now_ms(r.clock)

    rig = rig_factory(mode=Mode.TESTNET, sleep=sleeper)
    holder["rig"] = rig
    hold(rig, make_active(Mode.TESTNET, Direction.LONG, entry_bar=START_MS + 12 * H))
    rig.trader.run_forever(max_iterations=2)

    ensure = rig.broker.calls_named("ensure_protection")
    assert len(ensure) == 1
    assert removed["t"] <= ensure[0]["t"] <= removed["t"] + 31_000  # within min(heartbeat_sec, 60) s
    assert ensure[0]["t"] < ENTRY_BAR + H  # before the next candle
    names = rig.broker.names()
    last_candle_sync = max(i for i, (n, kw) in enumerate(rig.broker.calls) if n == "sync" and kw["n_candles"] > 0)
    assert names.index("ensure_protection") < last_candle_sync
    between = [kw for n, kw in rig.broker.calls if n == "sync" and kw["n_candles"] == 0]
    assert len(between) >= 100  # ~every 30 s for an hour

    # paper: stops are simulated per candle -> no between-bar checks
    paper = rig_factory(mode=Mode.PAPER)
    paper.trader.run_forever(max_iterations=2)
    assert [kw["n_candles"] for n, kw in paper.broker.calls if n == "sync"] == [20, 20, 21]
    before = len(paper.broker.calls)
    paper.trader.protection_check()
    assert len(paper.broker.calls) == before


def test_max_notional_caps_sizing(rig_factory: Callable[..., Rig], caplog: pytest.LogCaptureFixture) -> None:
    rig = rig_factory(mode=Mode.TESTNET, strategy=ScriptedStrategy({BAR: SignalAction.LONG}))
    rig.broker.max_notional_value = 1_000.0
    caplog.set_level(logging.WARNING, logger="bot.trader")
    rig.trader.startup()
    rig.trader.run_once()
    plan = rig.broker.calls_named("open_position")[0]["plan"]
    assert plan.sizing_cap == "notional"
    assert plan.notional <= 1_000.0 + 1e-9
    assert plan.qty == Decimal("10.000")
    rig.trader._effective_risk()
    warnings = [r for r in caplog.records if "leverage bracket" in r.getMessage()]
    assert len(warnings) == 1  # logged once per distinct value
    rig.broker.max_notional_value = 50_000.0  # above the config cap -> the config applies unchanged
    assert rig.trader._effective_risk() is rig.cfg.risk


# ---------------------------------------------------------------------------------------------
# Status, timing, stopping
# ---------------------------------------------------------------------------------------------


def test_status_and_heartbeat_written(rig_factory: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch) -> None:
    offset = 3_800  # Binance runs 3.8 s ahead of the local clock
    rig = rig_factory(offset_ms=offset)
    rig.trader.startup()
    rig.trader.run_once()
    status = rig.storage.get_status()
    local = now_ms(rig.clock)
    assert status["updated_at"] == local and status["updated_at"] != rig.market.client.server_time_ms()
    assert status["started_at"] == local
    assert status["mode"] == "paper" and status["symbol"] == SYMBOL and status["interval"] == "1h"
    assert status["state"] == "RUNNING"
    assert status["pid"] == os.getpid()
    assert status["last_bar_open_time"] == BAR
    assert status["account"]["equity"] == pytest.approx(10_000.0)
    assert status["last_signal"]["bar_open_time"] == BAR
    assert status["strategy"].startswith("scripted")

    beats: list[tuple[int, int]] = []
    real = rig.storage.touch_heartbeat

    def spy(ts: int) -> None:
        beats.append((ts, now_ms(rig.clock)))
        real(ts)

    monkeypatch.setattr(rig.storage, "touch_heartbeat", spy)
    rig.at(FORMING)
    rig.trader.run_forever(max_iterations=2)
    assert len(beats) >= 100
    assert all(ts == local_now for ts, local_now in beats)  # LOCAL timestamps
    gaps = {b[0] - a[0] for a, b in zip(beats, beats[1:])}
    assert gaps == {30_000}  # every heartbeat_sec
    final = rig.storage.get_status()
    assert final["state"] == "STOPPED" and final["message"] == ONCE_MESSAGE


def test_next_wake_time(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.at(FORMING, extra_ms=250)
    rig.trader.run_forever(max_iterations=2)
    calls = rig.market.kline_calls
    assert len(calls) == 3  # startup, iteration 1, iteration 2
    t0, t1 = calls[1], calls[2]
    wake = next_close_ms(t0, H) + DELAY_MS
    assert wake == ENTRY_BAR + H + DELAY_MS
    assert wake <= t1 <= wake + 1
    sleeps = rig.clock.sleeps
    assert sleeps and all(0 < s <= 1.0 for s in sleeps)  # <= 1 s chunks: a stop is honoured within 1 s
    assert sum(sleeps) * 1000 == pytest.approx(wake - t0, abs=2)


def test_stop_request_mid_iteration_finishes_order_sequence(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(mode=Mode.TESTNET, strategy=ScriptedStrategy(default=SignalAction.LONG))

    def ctrl_c_inside_order_sequence(b: FakeBroker) -> None:
        rig.trader.request_stop()
        rig.clock.sleep(0.5)  # the fake sleep inside open_position

    rig.broker.on_open = ctrl_c_inside_order_sequence
    rig.trader.run_forever()

    names = rig.broker.names()
    i_open = names.index("open_position")
    assert names[i_open + 1] == "place_sl"  # the SL was still placed
    assert names[-1] == "sync"  # the iteration finished (post-execution re-read)
    assert rig.trader.active is not None and rig.trader.active.protect_seq == 1
    assert rig.storage.get_state(active_trade_key(Mode.TESTNET, SYMBOL)) is not None
    assert len(rig.market.kline_calls) == 2  # startup + one iteration, then the loop exits
    assert not rig.trader.stopped_before_start
    status = rig.storage.get_status()
    assert status["state"] == "STOPPED" and status["message"] == USER_STOP_MESSAGE


def test_live_countdown_abort_sets_stopped_before_start(
    rig_factory: Callable[..., Rig], app_config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    holder: dict[str, Rig] = {}
    sleeps: list[float] = []

    def sleeper(s: float) -> None:
        sleeps.append(s)
        holder["rig"].clock.sleep(s)
        if len(sleeps) == 3:
            holder["rig"].trader.request_stop()  # Ctrl+C during the countdown

    rig = rig_factory(cfg=dataclasses.replace(app_config, mode=Mode.LIVE), sleep=sleeper)
    holder["rig"] = rig
    caplog.set_level(logging.WARNING, logger="bot.trader")
    rig.trader.run_forever()

    assert rig.trader.stopped_before_start
    assert sleeps == [1, 1, 1]
    assert rig.broker.calls == []  # nothing was sent to the exchange
    assert rig.market.kline_calls == []
    assert any("LIVE TRADING ON BINANCE MAINNET" in r.getMessage() for r in caplog.records)
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    status = rig.storage.get_status()
    assert status["state"] == "STOPPED" and status["message"] == ABORTED_MESSAGE


def test_auth_error_stops_loop(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(mode=Mode.TESTNET)
    rig.market.fail_on_call = {2: AuthError("invalid API key", code=-2015, http_status=401, path="/fapi/v3/account")}
    with pytest.raises(AuthError):
        rig.trader.run_forever()
    assert len(rig.market.kline_calls) == 2  # no further iterations
    status = rig.storage.get_status()
    assert status["state"] == "ERROR" and "AuthError" in status["message"]
    fatal = [e for e in rig.storage.recent_events(limit=50) if e["kind"] == "FATAL_ERROR"]
    assert fatal and fatal[0]["level"] == "CRITICAL"


def test_transient_errors_skip_and_repeated_errors_halt_entries(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(strategy=ScriptedStrategy(default=SignalAction.LONG))
    rig.market.fail_on_call = {2: TransientError("bad gateway", http_status=502, path="/fapi/v1/klines")}
    rig.strategy.fail_with = RuntimeError("strategy bug")  # type: ignore[attr-defined]
    rig.trader.run_forever(max_iterations=6)
    kinds = event_kinds(rig.storage)
    assert "ITERATION_SKIPPED" in kinds  # transient: skipped, the loop continued
    assert kinds.count("LOOP_ERROR") == 5
    assert rig.storage.get_state(halted_key(Mode.PAPER, SYMBOL))["reason"] == "repeated_errors"
    assert len(rig.market.kline_calls) == 7  # startup + 6 iterations: the loop kept going
    assert "open_position" not in rig.broker.names()


def test_effective_limit_raised_for_warmup_and_capped(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(strategy=ScriptedStrategy(warmup=400))
    rig.trader.startup()
    assert rig.trader.effective_limit == 800  # 2 x warmup > kline_limit 500
    too_long = rig_factory(strategy=ScriptedStrategy(warmup=800))
    with pytest.raises(ConfigError, match="strategy warmup too long for 1500 klines"):
        too_long.trader.startup()


def test_trader_rejects_mode_mismatch_and_paper_credentials(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(mode=Mode.PAPER)
    with pytest.raises(ConfigError, match="does not match"):
        Trader(
            dataclasses.replace(rig.cfg, mode=Mode.TESTNET),
            broker=rig.broker,
            market=rig.market,  # type: ignore[arg-type]
            strategy=rig.strategy,
            storage=rig.storage,
        )
    rig.market.client.has_credentials = True
    with pytest.raises(ConfigError, match="WITHOUT API credentials"):
        Trader(rig.cfg, broker=rig.broker, market=rig.market, strategy=rig.strategy, storage=rig.storage)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# Wiring and the single-instance lock
# ---------------------------------------------------------------------------------------------

FAKE_KEYS = {
    "BINANCE_API_KEY": "k" * 64,
    "BINANCE_API_SECRET": "s" * 64,
    "BINANCE_TESTNET_API_KEY": "t" * 64,
    "BINANCE_TESTNET_API_SECRET": "u" * 64,
}


def test_build_trader_paper_has_no_credentials(app_config: AppConfig, storage: Storage) -> None:
    trader = build_trader(app_config, storage, environ=dict(FAKE_KEYS))
    try:
        client = trader.market.client
        assert isinstance(client, BinanceRestClient)
        assert client.has_credentials is False
        assert client.base_url == MAINNET_REST_URL
        assert isinstance(trader.broker, PaperBroker)
        assert trader.mode is Mode.PAPER
        assert trader.strategy.name == "ma_cross"
        with pytest.raises(AuthError):  # raised locally, before any HTTP
            client.signed_request("GET", "/fapi/v3/account")
    finally:
        trader.close()


def test_build_trader_live_requires_confirmation(app_config: AppConfig, storage: Storage) -> None:
    live = dataclasses.replace(app_config, mode=Mode.LIVE)
    with pytest.raises(LiveTradingNotConfirmed):
        build_trader(live, storage, environ=dict(FAKE_KEYS))
    with pytest.raises(LiveTradingNotConfirmed):
        build_trader(live, storage, environ=dict(FAKE_KEYS) | {"CONFIRM_LIVE_TRADING": "yes"})
    trader = build_trader(live, storage, environ=dict(FAKE_KEYS) | {"CONFIRM_LIVE_TRADING": "YES"})
    try:
        assert isinstance(trader.broker, ExchangeBroker)
        assert trader.broker.mode is Mode.LIVE
        assert trader.market.client.base_url == MAINNET_REST_URL
        assert trader.market.client.has_credentials
    finally:
        trader.close()

    testnet = dataclasses.replace(app_config, mode=Mode.TESTNET)
    with pytest.raises(ConfigError):
        build_trader(testnet, storage, environ={})  # keys missing
    trader = build_trader(testnet, storage, environ=dict(FAKE_KEYS))
    try:
        assert isinstance(trader.broker, ExchangeBroker)
        assert trader.market.client.base_url == TESTNET_REST_URL
    finally:
        trader.close()


def test_single_instance_lock(tmp_path: Path) -> None:
    path = tmp_path / "data" / "trader.lock"
    with SingleInstanceLock(path):
        assert path.exists()
        with pytest.raises(BotError, match="another trader instance is running"):
            with SingleInstanceLock(path):  # second acquire in the same process on a separate fd
                pass
        assert path.stat().st_size == 0
    with SingleInstanceLock(path):  # released -> can be acquired again
        pass
    assert path.read_bytes() == b""  # never written to
    assert path.exists()  # and not deleted
