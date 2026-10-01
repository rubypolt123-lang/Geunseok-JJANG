"""PaperBroker (SPEC §9.3): fake MarketData, real Storage, FakeClock. No network."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from bot.broker.base import Broker
from bot.broker.paper import PaperBroker, paper_state_key
from bot.errors import BotError, ConfigError
from bot.fillmodel import FillModel
from bot.models import (
    ActiveTrade,
    Candle,
    Direction,
    ExitReason,
    Mode,
    OpenOutcome,
    OrderPurpose,
    OrderStatus,
    SymbolFilters,
    Trade,
    TradePlan,
    candles_from_df,
)
from bot.storage import Storage

SYMBOL = "BTCUSDT"
H = 3_600_000
T0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z (a funding time: 00/08/16 UTC)
INITIAL = 10_000.0
SLIP = 0.0005  # 5 bps
TAKER = 0.0005


# ---------------------------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------------------------


class FakeMarket:
    """Stand-in for MarketData: filters, funding rows (optionally published late) and premiumIndex."""

    def __init__(self, filters: SymbolFilters, *, has_credentials: bool = False) -> None:
        self.client = SimpleNamespace(has_credentials=has_credentials)
        self._filters = filters
        self.events: list[tuple[int, float, float | None]] = []
        self.visible_from_call: dict[int, int] = {}  # funding_time -> 1-based fetch number it first appears in
        self.funding_calls: list[tuple[int, int]] = []
        self.premium: dict[str, float | int] = {
            "mark_price": 50_000.0,
            "last_funding_rate": 0.0001,
            "next_funding_time": 0,
            "time": 0,
        }
        self.premium_calls = 0

    def add_funding(self, ft: int, rate: float, mark: float | None = None, *, visible_from_call: int = 1) -> None:
        self.events.append((int(ft), float(rate), mark))
        self.visible_from_call[int(ft)] = visible_from_call

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        return self._filters

    def funding_rates(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        self.funding_calls.append((int(start_ms), int(end_ms)))
        n = len(self.funding_calls)
        rows = [
            (ft, rate, mark)
            for ft, rate, mark in sorted(self.events)
            if start_ms <= ft <= end_ms and self.visible_from_call.get(ft, 1) <= n
        ]
        return pd.DataFrame(
            {
                "funding_time": pd.Series([r[0] for r in rows], dtype="int64"),
                "funding_rate": pd.Series([r[1] for r in rows], dtype="float64"),
                "mark_price": pd.Series([float("nan") if r[2] is None else r[2] for r in rows], dtype="float64"),
            }
        )

    def premium_index(self, symbol: str) -> dict[str, float | int]:
        self.premium_calls += 1
        return dict(self.premium)


def fill_model() -> FillModel:
    return FillModel(maker_fee=0.0002, taker_fee=TAKER, slippage_bps=5)


def make_plan(
    direction: Direction = Direction.LONG,
    *,
    ref: float = 50_000.0,
    qty: str = "0.100",
    stop: str = "49000.0",
    tp: str | None = "52000.0",
    leverage: int = 3,
    liq: float | None = None,
) -> TradePlan:
    q = Decimal(qty)
    if liq is None:  # risk.approx_liquidation_price(50000, dir, 3, 0.009)
        liq = 33_636.06 if direction is Direction.LONG else 66_072.68
    return TradePlan(
        symbol=SYMBOL,
        direction=direction,
        ref_price=ref,
        qty=q,
        stop_price=Decimal(stop),
        take_profit_price=None if tp is None else Decimal(tp),
        notional=float(q) * ref,
        risk_amount=float(q) * abs(ref - float(stop)),
        leverage=leverage,
        liquidation_price=liq,
    )


def active_from(plan: TradePlan, outcome: OpenOutcome, *, entry_bar_time: int) -> ActiveTrade:
    """Exactly what the trader builds (§10.1 ``_active_from_entry``)."""
    return ActiveTrade(
        trade_id=f"paper-{SYMBOL}-{entry_bar_time}-{'L' if plan.direction is Direction.LONG else 'S'}",
        symbol=SYMBOL,
        direction=plan.direction,
        qty=outcome.qty,
        entry_price=outcome.avg_price,
        entry_time=outcome.entry_time,
        entry_bar_open_time=entry_bar_time,
        stop_price=float(plan.stop_price),
        take_profit_price=None if plan.take_profit_price is None else float(plan.take_profit_price),
        liquidation_price=plan.liquidation_price,
        leverage=plan.leverage,
        risk_amount=plan.risk_amount * outcome.qty / float(plan.qty),
        entry_fee=outcome.entry_fee,
        entry_client_id="mab1-4314-EN-1704067200-0",
        protect_seq=1,
        entry_order_id=outcome.entry_order.exchange_id if outcome.entry_order else None,
    )


def ohlc(
    ohlc_factory: Callable[..., pd.DataFrame], rows: Sequence[tuple[float, float, float, float]], start_ms: int
) -> list[Candle]:
    return candles_from_df(ohlc_factory(rows, start_ms=start_ms))


def flat_bars(
    candle_factory: Callable[..., pd.DataFrame], n: int, start_ms: int, price: float = 50_000.0
) -> list[Candle]:
    """``n`` quiet 1h bars at ``price`` (high/low within 0.1 %: never hits the test stops/TPs)."""
    return candles_from_df(candle_factory([price] * n, start_ms=start_ms))


@pytest.fixture
def market(btc_filters: SymbolFilters) -> FakeMarket:
    return FakeMarket(btc_filters)


@pytest.fixture
def make_broker(market: FakeMarket, storage: Storage, fixed_clock: Callable[..., Any]) -> Callable[..., PaperBroker]:
    clock = fixed_clock(T0 / 1000 + 10 * 3600)

    def make(*, include_funding: bool = True, mkt: FakeMarket | None = None) -> PaperBroker:
        return PaperBroker(
            market=mkt or market,  # type: ignore[arg-type]
            fill_model=fill_model(),
            storage=storage,
            initial_balance=INITIAL,
            include_funding=include_funding,
            clock=clock,
            sleep=clock.sleep,
        )

    make.clock = clock  # type: ignore[attr-defined]
    return make


def state_of(storage: Storage) -> dict[str, Any]:
    return storage.get_state(paper_state_key(SYMBOL))


def seed_cursor(broker: PaperBroker, candle_factory: Callable[..., pd.DataFrame], bar_open: int) -> None:
    """First sync: sets last_bar_open_time without simulating anything."""
    broker.sync(SYMBOL, None, flat_bars(candle_factory, 1, bar_open))


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------


def test_paper_requires_no_credentials(btc_filters: SymbolFilters, storage: Storage) -> None:
    keyed = FakeMarket(btc_filters, has_credentials=True)
    with pytest.raises(ConfigError):
        PaperBroker(market=keyed, fill_model=fill_model(), storage=storage, initial_balance=INITIAL)  # type: ignore[arg-type]
    keyless = FakeMarket(btc_filters)
    broker = PaperBroker(market=keyless, fill_model=fill_model(), storage=storage, initial_balance=INITIAL)  # type: ignore[arg-type]
    assert isinstance(broker, Broker)
    assert broker.mode is Mode.PAPER
    assert broker.max_notional(SYMBOL) is None
    assert broker.prepare_symbol(SYMBOL, 3) is btc_filters


def test_open_fills_at_ref_with_slippage_and_fee(make_broker: Callable[..., PaperBroker], storage: Storage) -> None:
    broker = make_broker()
    plan = make_plan(Direction.LONG)
    out = broker.open_position(
        plan, entry_client_id="EN-1", sl_client_id="SL-1", tp_client_id="TP-1", ref_price=50_000.0, bar_time=T0
    )
    fill = 50_000.0 * (1 + SLIP)
    fee = 0.1 * fill * TAKER
    assert out.filled is True
    assert out.qty == pytest.approx(0.1)
    assert out.avg_price == pytest.approx(fill)
    assert out.entry_fee == pytest.approx(fee)
    assert out.entry_time == T0
    assert out.entry_order is not None
    assert out.entry_order.status is OrderStatus.FILLED
    assert out.entry_order.client_id == "EN-1"
    assert out.entry_order.purpose is OrderPurpose.ENTRY
    kinds = [(p.kind, p.client_id, p.trigger_price, p.close_position, p.status) for p in out.protective]
    assert kinds == [
        (OrderPurpose.STOP_LOSS, "SL-1", 49_000.0, True, "NEW"),
        (OrderPurpose.TAKE_PROFIT, "TP-1", 52_000.0, True, "NEW"),
    ]
    st = state_of(storage)
    assert st["cash"] == pytest.approx(INITIAL - fee)
    assert st["position"]["entry_price"] == pytest.approx(fill)
    assert st["position"]["entry_bar_open_time"] == T0
    assert st["funding_cursor"] == T0

    # a second entry while not flat is a bug in the caller
    with pytest.raises(BotError):
        broker.open_position(plan, entry_client_id="EN-2", sl_client_id="SL-2", tp_client_id=None, ref_price=1.0, bar_time=T0)

    # short mirror: SELL fills below the reference
    broker.close_position(SYMBOL, None, reason=ExitReason.SIGNAL, client_id="EX-1", ref_price=50_000.0, bar_time=T0)
    short = broker.open_position(
        make_plan(Direction.SHORT, stop="51000.0", tp="48000.0"),
        entry_client_id="EN-3",
        sl_client_id="SL-3",
        tp_client_id=None,
        ref_price=50_000.0,
        bar_time=T0 + H,
    )
    assert short.avg_price == pytest.approx(50_000.0 * (1 - SLIP))
    assert short.entry_order is not None and short.entry_order.side.value == "SELL"


def test_sl_hit_closes_with_stop_loss(
    make_broker: Callable[..., PaperBroker],
    storage: Storage,
    candle_factory: Callable[..., pd.DataFrame],
    ohlc_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker(include_funding=False)
    seed_cursor(broker, candle_factory, T0 - H)
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0)
    active = active_from(plan, out, entry_bar_time=T0)
    cash_after_entry = state_of(storage)["cash"]

    candles = ohlc(ohlc_factory, [(50_000, 50_100, 49_500, 49_800), (49_800, 49_900, 48_900, 49_000)], T0)
    res = broker.sync(SYMBOL, active, candles)
    assert res.closure is not None
    c = res.closure
    exit_price = 49_000.0 * (1 - SLIP)
    exit_fee = 0.1 * exit_price * TAKER
    gross = 0.1 * (exit_price - out.avg_price)
    assert c.reason is ExitReason.STOP_LOSS
    assert c.exit_time == T0 + 2 * H - 1  # close_time of the bar in which the stop filled
    assert c.exit_price == pytest.approx(exit_price)
    assert c.exit_fee == pytest.approx(exit_fee)
    assert c.gross_pnl == pytest.approx(gross)
    assert c.funding == 0.0
    assert c.order is not None and c.order.client_id == "SL" and c.order.purpose is OrderPurpose.STOP_LOSS
    assert res.account.position is None
    assert res.issues == ()
    st = state_of(storage)
    assert st["position"] is None
    assert st["cash"] == pytest.approx(cash_after_entry + gross - exit_fee)
    assert st["last_bar_open_time"] == T0 + H
    assert st["last_close"] == 49_000.0
    # the trader books it: R multiple of a clean stop-out at the planned stop
    trade = Trade.from_closure(active, c, source="paper")
    assert trade.net_pnl == pytest.approx(gross - out.entry_fee - exit_fee)


def test_sl_first_when_both_touched(
    make_broker: Callable[..., PaperBroker],
    candle_factory: Callable[..., pd.DataFrame],
    ohlc_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker(include_funding=False)
    seed_cursor(broker, candle_factory, T0 - H)
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0)
    res = broker.sync(SYMBOL, active_from(plan, out, entry_bar_time=T0), ohlc(ohlc_factory, [(50_000, 52_500, 48_500, 50_000)], T0))
    assert res.closure is not None
    assert res.closure.reason is ExitReason.STOP_LOSS
    assert res.closure.exit_price == pytest.approx(49_000.0 * (1 - SLIP))


def test_tp_hit(
    make_broker: Callable[..., PaperBroker],
    candle_factory: Callable[..., pd.DataFrame],
    ohlc_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker(include_funding=False)
    seed_cursor(broker, candle_factory, T0 - H)
    # short: TP below, triggered by the low
    plan = make_plan(Direction.SHORT, stop="51000.0", tp="48000.0")
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0)
    res = broker.sync(SYMBOL, active_from(plan, out, entry_bar_time=T0), ohlc(ohlc_factory, [(50_000, 50_500, 47_900, 48_200)], T0))
    assert res.closure is not None
    c = res.closure
    exit_price = 48_000.0 * (1 + SLIP)  # BUY to close: adverse slippage up
    assert c.reason is ExitReason.TAKE_PROFIT
    assert c.exit_price == pytest.approx(exit_price)
    assert c.gross_pnl == pytest.approx(-0.1 * (exit_price - out.avg_price))
    assert c.order is not None and c.order.client_id == "TP" and c.order.purpose is OrderPurpose.TAKE_PROFIT


def test_gap_through_stop_fills_at_open(
    make_broker: Callable[..., PaperBroker],
    candle_factory: Callable[..., pd.DataFrame],
    ohlc_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker(include_funding=False)
    seed_cursor(broker, candle_factory, T0 - H)
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0)
    bars = [(50_000, 50_100, 49_900, 50_000), (48_000, 48_500, 47_500, 48_200)]
    res = broker.sync(SYMBOL, active_from(plan, out, entry_bar_time=T0), ohlc(ohlc_factory, bars, T0))
    assert res.closure is not None
    assert res.closure.reason is ExitReason.STOP_LOSS
    assert res.closure.exit_price == pytest.approx(48_000.0 * (1 - SLIP))  # the open, not the stop
    assert res.closure.exit_time == T0 + 2 * H - 1


def test_gap_beyond_tp_fills_tp_at_open(
    make_broker: Callable[..., PaperBroker],
    candle_factory: Callable[..., pd.DataFrame],
    ohlc_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker(include_funding=False)
    seed_cursor(broker, candle_factory, T0 - H)
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0)
    # opens above the TP and later trades below the stop: the open decides (TAKE_PROFIT at the open)
    res = broker.sync(
        SYMBOL, active_from(plan, out, entry_bar_time=T0), ohlc(ohlc_factory, [(53_000, 53_100, 48_000, 48_500)], T0)
    )
    assert res.closure is not None
    assert res.closure.reason is ExitReason.TAKE_PROFIT
    assert res.closure.exit_price == pytest.approx(53_000.0 * (1 - SLIP))


def test_candles_before_entry_ignored(
    make_broker: Callable[..., PaperBroker],
    storage: Storage,
    candle_factory: Callable[..., pd.DataFrame],
    ohlc_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker(include_funding=False)
    seed_cursor(broker, candle_factory, T0 - 3 * H)
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0)
    # bars T0-2h and T0-1h crash through the stop but precede the entry bar
    bars = [(50_000, 50_100, 40_000, 45_000), (45_000, 50_000, 40_000, 50_000), (50_000, 50_100, 49_900, 50_050)]
    res = broker.sync(SYMBOL, active_from(plan, out, entry_bar_time=T0), ohlc(ohlc_factory, bars, T0 - 2 * H))
    assert res.closure is None
    assert res.account.position is not None
    assert res.issues == ()
    st = state_of(storage)
    assert st["position"] is not None
    assert st["last_bar_open_time"] == T0
    assert st["last_close"] == 50_050.0


def test_processed_candles_not_reprocessed(
    make_broker: Callable[..., PaperBroker],
    storage: Storage,
    candle_factory: Callable[..., pd.DataFrame],
    ohlc_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker(include_funding=False)
    seed_cursor(broker, candle_factory, T0 - H)
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0)
    active = active_from(plan, out, entry_bar_time=T0)
    assert broker.sync(SYMBOL, active, ohlc(ohlc_factory, [(50_000, 50_100, 49_900, 50_000)], T0)).closure is None
    before = state_of(storage)
    # the same bar again (even with different data that would hit the stop) is not simulated twice
    again = broker.sync(SYMBOL, active, ohlc(ohlc_factory, [(50_000, 50_100, 40_000, 41_000)], T0))
    assert again.closure is None
    after = state_of(storage)
    assert after["position"] == before["position"]
    assert after["cash"] == before["cash"]
    assert after["last_bar_open_time"] == T0
    # a NEW bar is simulated
    later = broker.sync(SYMBOL, active, ohlc(ohlc_factory, [(50_000, 50_100, 49_900, 50_000), (50_000, 50_100, 48_000, 48_100)], T0))
    assert later.closure is not None and later.closure.reason is ExitReason.STOP_LOSS
    assert later.closure.exit_time == T0 + 2 * H - 1


def test_sync_with_empty_candles_is_readonly(
    make_broker: Callable[..., PaperBroker],
    market: FakeMarket,
    storage: Storage,
    candle_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker()
    seed_cursor(broker, candle_factory, T0 - H)
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0)
    market.add_funding(T0 + 8 * H, 0.001, 50_000.0)
    before = state_of(storage)
    calls_before = list(market.funding_calls)
    res = broker.sync(SYMBOL, active_from(plan, out, entry_bar_time=T0), [])
    assert state_of(storage) == before  # no cursor / cash / funding change
    assert market.funding_calls == calls_before  # no funding fetch
    assert res.closure is None
    assert res.issues == ()
    assert res.account.position is not None
    assert res.account.position.qty == pytest.approx(0.1)
    assert res.account.wallet_balance == pytest.approx(before["cash"])
    assert len(res.account.protective_orders) == 2


def test_state_persists_across_instances(
    make_broker: Callable[..., PaperBroker],
    storage: Storage,
    candle_factory: Callable[..., pd.DataFrame],
) -> None:
    first = make_broker()
    seed_cursor(first, candle_factory, T0 - H)
    plan = make_plan(Direction.SHORT, stop="51000.0", tp=None)
    out = first.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id=None, ref_price=50_000.0, bar_time=T0)
    second = make_broker()
    res = second.sync(SYMBOL, active_from(plan, out, entry_bar_time=T0), [])
    assert res.account.position is not None
    assert res.account.position.qty == pytest.approx(-0.1)
    assert res.account.position.entry_price == pytest.approx(out.avg_price)
    assert res.account.wallet_balance == pytest.approx(INITIAL - out.entry_fee)
    assert [p.kind for p in res.account.protective_orders] == [OrderPurpose.STOP_LOSS]
    closure = second.close_position(SYMBOL, None, reason=ExitReason.SIGNAL, client_id="EX", ref_price=50_000.0, bar_time=T0 + H)
    assert closure is not None and closure.qty == pytest.approx(0.1)
    # and a third instance sees it flat with the realised cash
    third = make_broker()
    flat = third.sync(SYMBOL, None, [])
    assert flat.account.position is None
    assert flat.account.wallet_balance == pytest.approx(state_of(storage)["cash"])


def test_funding_applied_with_timestamp_rule(
    make_broker: Callable[..., PaperBroker],
    market: FakeMarket,
    storage: Storage,
    candle_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker()
    seed_cursor(broker, candle_factory, T0 - H)
    market.add_funding(T0, 0.0005, 50_000.0)  # at the entry time: NOT charged (entry_time < ft is required)
    market.add_funding(T0 + 8 * H, 0.0002, 50_500.0)  # mid-trade: charged
    market.add_funding(T0 + 16 * H, 0.0003, 51_000.0)  # after the processed bars: not yet
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0)
    active = active_from(plan, out, entry_bar_time=T0)
    cash0 = state_of(storage)["cash"]

    broker.sync(SYMBOL, active, flat_bars(candle_factory, 9, T0))  # bars T0 .. T0+8h
    # fetched ONCE for the whole range, from the cursor + 1 to the last bar's close
    assert market.funding_calls == [(T0 + 1, T0 + 9 * H - 1)]
    paid = 0.1 * 50_500.0 * 0.0002  # long + positive rate pays
    st = state_of(storage)
    assert st["cash"] == pytest.approx(cash0 - paid)
    assert st["position"]["funding"] == pytest.approx(paid)
    assert st["funding_cursor"] == T0 + 8 * H

    closure = broker.close_position(SYMBOL, active, reason=ExitReason.SIGNAL, client_id="EX", ref_price=50_000.0, bar_time=T0 + 9 * H)
    assert closure is not None
    assert closure.funding == pytest.approx(paid)
    assert broker.clock is make_broker.clock  # type: ignore[attr-defined]
    assert make_broker.clock.sleeps == []  # type: ignore[attr-defined]  # 09:00 is no funding time -> no waiting

    # a short RECEIVES a positive rate
    market.add_funding(T0 + 24 * H, 0.0001, 50_000.0)
    short_plan = make_plan(Direction.SHORT, stop="51000.0", tp=None)
    broker.sync(SYMBOL, None, flat_bars(candle_factory, 6, T0 + 9 * H))  # cursor to T0+14h
    s_out = broker.open_position(short_plan, entry_client_id="EN2", sl_client_id="SL2", tp_client_id=None, ref_price=50_000.0, bar_time=T0 + 15 * H)
    s_active = active_from(short_plan, s_out, entry_bar_time=T0 + 15 * H)
    cash1 = state_of(storage)["cash"]
    broker.sync(SYMBOL, s_active, flat_bars(candle_factory, 10, T0 + 15 * H))  # .. T0+24h
    received = 0.1 * 51_000.0 * 0.0003 + 0.1 * 50_000.0 * 0.0001
    st = state_of(storage)
    assert st["position"]["funding"] == pytest.approx(-received)
    assert st["cash"] == pytest.approx(cash1 + received)


def test_funding_cursor_is_last_applied_event(
    make_broker: Callable[..., PaperBroker],
    market: FakeMarket,
    storage: Storage,
    candle_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker()
    seed_cursor(broker, candle_factory, T0)
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0 + H)
    active = active_from(plan, out, entry_bar_time=T0 + H)
    # the 08:00 record is published late: the first fetch does not return it
    market.add_funding(T0 + 8 * H, 0.0004, 50_000.0, visible_from_call=2)

    broker.sync(SYMBOL, active, flat_bars(candle_factory, 8, T0 + H))  # bars 01:00 .. 08:00
    st = state_of(storage)
    assert st["position"]["funding"] == 0.0
    assert st["funding_cursor"] == T0 + H  # NOT advanced to the end of the queried range
    assert st["last_bar_open_time"] == T0 + 8 * H

    broker.sync(SYMBOL, active, flat_bars(candle_factory, 1, T0 + 9 * H))
    assert market.funding_calls[-1][0] == T0 + H + 1  # the next query starts at the cursor + 1
    paid = 0.1 * 50_000.0 * 0.0004
    st = state_of(storage)
    assert st["position"]["funding"] == pytest.approx(paid)
    assert st["funding_cursor"] == T0 + 8 * H


def test_close_at_funding_time_waits_for_late_record(
    make_broker: Callable[..., PaperBroker],
    market: FakeMarket,
    storage: Storage,
    candle_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker()
    clock = make_broker.clock  # type: ignore[attr-defined]
    seed_cursor(broker, candle_factory, T0)
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0 + H)
    active = active_from(plan, out, entry_bar_time=T0 + H)
    market.add_funding(T0 + 8 * H, 0.0001, 50_000.0)
    broker.sync(SYMBOL, active, flat_bars(candle_factory, 15, T0 + H))  # 01:00 .. 15:00 (fetch #1)
    assert state_of(storage)["funding_cursor"] == T0 + 8 * H
    # the 16:00 settlement appears only on the 2nd re-fetch (fetch #4)
    market.add_funding(T0 + 16 * H, 0.0003, 50_200.0, visible_from_call=4)

    closure = broker.close_position(SYMBOL, active, reason=ExitReason.SIGNAL, client_id="EX", ref_price=50_000.0, bar_time=T0 + 16 * H)
    assert closure is not None
    assert clock.sleeps == [1.0, 1.0]
    assert len(market.funding_calls) == 4
    assert market.premium_calls == 0
    expected = 0.1 * 50_000.0 * 0.0001 + 0.1 * 50_200.0 * 0.0003
    assert closure.funding == pytest.approx(expected)
    assert storage.recent_events(mode="paper") == []


def test_close_at_funding_time_estimates_when_missing(
    make_broker: Callable[..., PaperBroker],
    market: FakeMarket,
    storage: Storage,
    candle_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker()
    clock = make_broker.clock  # type: ignore[attr-defined]
    seed_cursor(broker, candle_factory, T0)
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0 + H)
    active = active_from(plan, out, entry_bar_time=T0 + H)
    market.premium = {"mark_price": 50_400.0, "last_funding_rate": 0.0002, "next_funding_time": T0 + 24 * H, "time": T0 + 16 * H}
    # no settlement record has ever been seen: the schedule defaults to 8 h aligned to 00:00 UTC

    closure = broker.close_position(SYMBOL, active, reason=ExitReason.SIGNAL, client_id="EX", ref_price=50_000.0, bar_time=T0 + 16 * H)
    assert closure is not None
    assert clock.sleeps == [1.0, 1.0, 1.0]
    assert len(market.funding_calls) == 4  # initial fetch + 3 re-fetches
    assert market.premium_calls == 1
    estimated = 0.1 * 50_400.0 * 0.0002
    assert closure.funding == pytest.approx(estimated)
    events = storage.recent_events(mode="paper")
    assert [e["kind"] for e in events] == ["FUNDING_ESTIMATED"]
    assert events[0]["level"] == "WARNING"

    # a close that is not on the funding schedule never waits
    clock.sleeps.clear()
    out2 = broker.open_position(plan, entry_client_id="EN2", sl_client_id="SL2", tp_client_id=None, ref_price=50_000.0, bar_time=T0 + 17 * H)
    broker.close_position(SYMBOL, active_from(plan, out2, entry_bar_time=T0 + 17 * H), reason=ExitReason.SIGNAL,
                          client_id="EX2", ref_price=50_000.0, bar_time=T0 + 19 * H)
    assert clock.sleeps == []
    assert market.premium_calls == 1


def test_close_position_at_ref_price(make_broker: Callable[..., PaperBroker], storage: Storage) -> None:
    broker = make_broker(include_funding=False)
    assert broker.close_position(SYMBOL, None, reason=ExitReason.SIGNAL, client_id="EX", ref_price=50_000.0, bar_time=T0) is None
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0)
    with pytest.raises(ValueError):
        broker.close_position(SYMBOL, None, reason=ExitReason.SIGNAL, client_id="EX", ref_price=None, bar_time=T0 + H)
    cash = state_of(storage)["cash"]
    closure = broker.close_position(SYMBOL, None, reason=ExitReason.FLIP, client_id="EX-1", ref_price=51_000.0, bar_time=T0 + H)
    assert closure is not None
    exit_price = 51_000.0 * (1 - SLIP)
    exit_fee = 0.1 * exit_price * TAKER
    gross = 0.1 * (exit_price - out.avg_price)
    assert closure.exit_time == T0 + H
    assert closure.exit_price == pytest.approx(exit_price)
    assert closure.exit_fee == pytest.approx(exit_fee)
    assert closure.gross_pnl == pytest.approx(gross)
    assert closure.reason is ExitReason.FLIP
    assert closure.order is not None
    assert closure.order.client_id == "EX-1" and closure.order.purpose is OrderPurpose.EXIT
    assert state_of(storage)["cash"] == pytest.approx(cash + gross - exit_fee)
    assert state_of(storage)["position"] is None

    broker.open_position(plan, entry_client_id="EN2", sl_client_id="SL2", tp_client_id=None, ref_price=50_000.0, bar_time=T0 + 2 * H)
    ks = broker.close_position(SYMBOL, None, reason=ExitReason.KILL_SWITCH, client_id="KS", ref_price=49_000.0, bar_time=T0 + 3 * H)
    assert ks is not None and ks.order is not None and ks.order.purpose is OrderPurpose.FLATTEN


def test_account_equity_mark_to_market(
    make_broker: Callable[..., PaperBroker],
    candle_factory: Callable[..., pd.DataFrame],
    ohlc_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker(include_funding=False)
    seed_cursor(broker, candle_factory, T0 - H)
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0)
    res = broker.sync(SYMBOL, active_from(plan, out, entry_bar_time=T0), ohlc(ohlc_factory, [(50_000, 51_100, 49_900, 51_000)], T0))
    acct = res.account
    cash = INITIAL - out.entry_fee
    upnl = 0.1 * (51_000.0 - out.avg_price)
    margin = 0.1 * out.avg_price / 3
    assert acct.wallet_balance == pytest.approx(cash)
    assert acct.unrealized_pnl == pytest.approx(upnl)
    assert acct.equity == pytest.approx(cash + upnl)
    assert acct.available_balance == pytest.approx(cash - margin)
    pos = acct.position
    assert pos is not None
    assert pos.qty == pytest.approx(0.1) and pos.direction is Direction.LONG
    assert pos.mark_price == 51_000.0
    assert pos.isolated_margin == pytest.approx(margin)
    assert pos.leverage == 3
    assert pos.liquidation_price == pytest.approx(plan.liquidation_price)
    assert acct.ts == pos.updated_at
    assert all(p.exchange_id is None and p.close_position for p in acct.protective_orders)


def test_liquidation_loss(
    make_broker: Callable[..., PaperBroker],
    market: FakeMarket,
    storage: Storage,
    candle_factory: Callable[..., pd.DataFrame],
    ohlc_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker()
    seed_cursor(broker, candle_factory, T0)
    # 10x long with a (deliberately) wide stop below the liquidation price
    plan = make_plan(Direction.LONG, stop="44000.0", tp=None, leverage=10, liq=45_500.0)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id=None, ref_price=50_000.0, bar_time=T0 + H)
    active = active_from(plan, out, entry_bar_time=T0 + H)
    market.add_funding(T0 + 8 * H, 0.0001, 50_000.0)
    cash0 = state_of(storage)["cash"]

    bars = ohlc_factory([(50_000, 50_050, 49_950, 50_000)] * 8, start_ms=T0 + H)  # 01:00 .. 08:00
    gap = ohlc_factory([(45_400, 45_600, 45_000, 45_300)], start_ms=T0 + 9 * H)  # gaps through the liquidation price
    candles = candles_from_df(pd.concat([bars, gap], ignore_index=True))
    res = broker.sync(SYMBOL, active, candles)
    c = res.closure
    assert c is not None
    assert c.reason is ExitReason.LIQUIDATION
    assert c.exit_price == pytest.approx(45_500.0)
    assert c.exit_fee == 0.0
    funding_paid = 0.1 * 50_000.0 * 0.0001
    im = 0.1 * out.avg_price / 10
    assert c.funding == pytest.approx(funding_paid)
    assert c.gross_pnl == pytest.approx(-(im - funding_paid))
    trade = Trade.from_closure(active, c, source="paper")
    assert trade.net_pnl == pytest.approx(-im - trade.fees)
    assert state_of(storage)["cash"] == pytest.approx(cash0 - im)  # exactly the isolated margin is lost


def test_first_sync_does_not_simulate_history(
    make_broker: Callable[..., PaperBroker],
    market: FakeMarket,
    storage: Storage,
    candle_factory: Callable[..., pd.DataFrame],
) -> None:
    broker = make_broker()
    assert state_of(storage) is None
    res = broker.sync(SYMBOL, None, flat_bars(candle_factory, 10, T0))
    st = state_of(storage)
    assert st["last_bar_open_time"] == T0 + 9 * H
    assert st["last_close"] == 50_000.0
    assert st["funding_cursor"] is None
    assert st["cash"] == INITIAL
    assert st["position"] is None
    assert market.funding_calls == []
    assert res.closure is None and res.issues == ()
    assert res.account.equity == INITIAL and res.account.position is None


def test_sync_issues_untracked_and_unknown_closure(
    make_broker: Callable[..., PaperBroker], candle_factory: Callable[..., pd.DataFrame]
) -> None:
    broker = make_broker(include_funding=False)
    seed_cursor(broker, candle_factory, T0 - H)
    plan = make_plan(Direction.LONG)
    out = broker.open_position(plan, entry_client_id="EN", sl_client_id="SL", tp_client_id="TP", ref_price=50_000.0, bar_time=T0)
    active = active_from(plan, out, entry_bar_time=T0)
    # the trader lost track of the position -> it must adopt it
    assert broker.sync(SYMBOL, None, []).issues == ("UNTRACKED_POSITION",)
    # ensure_protection never places anything in paper mode
    acct = broker.sync(SYMBOL, active, []).account
    assert broker.ensure_protection(active, acct, sl_client_id="SL-2", tp_client_id="TP-2") == acct.protective_orders
    # the trader believes in a position the simulation does not have -> closure UNKNOWN at the last close
    broker.close_position(SYMBOL, None, reason=ExitReason.SIGNAL, client_id="EX", ref_price=50_000.0, bar_time=T0 + H)
    res = broker.sync(SYMBOL, active, [])
    assert res.issues == ("CLOSURE_DETAILS_UNKNOWN",)
    assert res.closure is not None
    assert res.closure.reason is ExitReason.UNKNOWN
    assert res.closure.exit_price == 50_000.0
    assert res.closure.gross_pnl is None
