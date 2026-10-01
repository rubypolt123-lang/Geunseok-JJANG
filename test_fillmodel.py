"""U4 — bot/fillmodel.py (SPEC §9.1, §14.2)."""

from __future__ import annotations

import math

import pytest

from bot.config import ExecutionConfig, FeeConfig
from bot.fillmodel import FillModel, funding_payment, liquidation_loss, resolve_intrabar_exit
from bot.models import ActiveTrade, Direction, ExitReason, PositionClosure, Side, Trade

LONG = Direction.LONG
SHORT = Direction.SHORT


@pytest.fixture
def fm() -> FillModel:
    return FillModel(maker_fee=0.0002, taker_fee=0.0005, slippage_bps=5)


def test_from_config_and_slip() -> None:
    execution = ExecutionConfig(
        fees=FeeConfig(maker=0.0002, taker=0.0005),
        slippage_bps=5,
        working_type="MARK_PRICE",
        price_protect=False,
        protective_mode="close_position",
        candle_close_delay_sec=3,
        kline_limit=500,
        recv_window_ms=5000,
        heartbeat_sec=30,
        bot_id="mab1",
    )
    model = FillModel.from_config(execution)
    assert model == FillModel(maker_fee=0.0002, taker_fee=0.0005, slippage_bps=5.0)
    assert model.slip == pytest.approx(0.0005)
    assert type(model.slippage_bps) is float


def test_invalid_parameters_rejected() -> None:
    with pytest.raises(ValueError):
        FillModel(maker_fee=-0.1, taker_fee=0.0005, slippage_bps=5)
    with pytest.raises(ValueError):
        FillModel(maker_fee=0.0002, taker_fee=math.nan, slippage_bps=5)
    with pytest.raises(ValueError):
        FillModel(maker_fee=0.0002, taker_fee=0.0005, slippage_bps=-1)


def test_market_fill_slippage_direction(fm: FillModel) -> None:
    # adverse: buys fill higher, sells fill lower
    assert fm.market_fill_price(50_000.0, Side.BUY) == pytest.approx(50_025.0)
    assert fm.market_fill_price(50_000.0, Side.SELL) == pytest.approx(49_975.0)
    assert fm.market_fill_price(50_000.0, Side.BUY) > 50_000.0 > fm.market_fill_price(50_000.0, Side.SELL)
    # exits follow the same adverse rule for the closing side
    assert fm.exit_fill_price(49_000.0, LONG.closing_side) == pytest.approx(49_000.0 * (1 - 0.0005))
    assert fm.exit_fill_price(51_000.0, SHORT.closing_side) == pytest.approx(51_000.0 * (1 + 0.0005))
    # zero slippage -> exact reference price
    flat = FillModel(maker_fee=0.0, taker_fee=0.0, slippage_bps=0)
    assert flat.market_fill_price(123.45, Side.BUY) == 123.45
    assert flat.market_fill_price(123.45, "SELL") == 123.45  # plain strings are accepted


def test_fee_taker_maker(fm: FillModel) -> None:
    assert fm.fee(0.5, 100.0) == pytest.approx(0.025)  # taker by default
    assert fm.fee(0.5, 100.0, taker=True) == pytest.approx(0.025)
    assert fm.fee(0.5, 100.0, taker=False) == pytest.approx(0.01)
    assert fm.fee(-0.5, 100.0) == pytest.approx(0.025)  # abs(qty)
    assert fm.fee(0.0, 100.0) == 0.0


def test_long_stop_gap_fills_at_open() -> None:
    # open below the stop: the stop fills at the open (worse than the trigger)
    assert resolve_intrabar_exit(LONG, 95.0, 96.0, 90.0, 97.0, 110.0, 50.0) == (ExitReason.STOP_LOSS, 95.0)
    # open exactly at the stop counts as a gap too
    assert resolve_intrabar_exit(LONG, 97.0, 98.0, 96.0, 97.0, 110.0, None) == (ExitReason.STOP_LOSS, 97.0)


def test_gap_open_beyond_tp_fills_tp() -> None:
    # LONG: o >= tp even though the low also reaches the stop -> the open is the first price: TP at the open
    assert resolve_intrabar_exit(LONG, 112.0, 113.0, 96.0, 97.0, 110.0, 50.0) == (ExitReason.TAKE_PROFIT, 112.0)
    # SHORT mirror: o <= tp and the high also reaches the stop
    assert resolve_intrabar_exit(SHORT, 88.0, 104.0, 87.0, 103.0, 90.0, 150.0) == (ExitReason.TAKE_PROFIT, 88.0)


def test_sl_first_when_both_touched() -> None:
    # neither level gapped at the open, both touched inside the bar -> STOP_LOSS at the stop
    assert resolve_intrabar_exit(LONG, 100.0, 111.0, 96.0, 97.0, 110.0, 50.0) == (ExitReason.STOP_LOSS, 97.0)
    assert resolve_intrabar_exit(SHORT, 100.0, 104.0, 89.0, 103.0, 90.0, 150.0) == (ExitReason.STOP_LOSS, 103.0)


def test_tp_only() -> None:
    assert resolve_intrabar_exit(LONG, 100.0, 111.0, 99.0, 97.0, 110.0, 50.0) == (ExitReason.TAKE_PROFIT, 110.0)
    # exactly touching the TP counts
    assert resolve_intrabar_exit(LONG, 100.0, 110.0, 99.0, 97.0, 110.0, 50.0) == (ExitReason.TAKE_PROFIT, 110.0)
    # nothing touched
    assert resolve_intrabar_exit(LONG, 100.0, 109.0, 98.0, 97.0, 110.0, 50.0) is None
    # no TP configured: a huge high does nothing
    assert resolve_intrabar_exit(LONG, 100.0, 500.0, 98.0, 97.0, None, 50.0) is None


def test_short_mirror() -> None:
    stop, tp, liq = 103.0, 90.0, 150.0
    assert resolve_intrabar_exit(SHORT, 100.0, 104.0, 99.0, stop, tp, liq) == (ExitReason.STOP_LOSS, 103.0)
    assert resolve_intrabar_exit(SHORT, 105.0, 106.0, 99.0, stop, tp, liq) == (ExitReason.STOP_LOSS, 105.0)
    assert resolve_intrabar_exit(SHORT, 100.0, 101.0, 89.0, stop, tp, liq) == (ExitReason.TAKE_PROFIT, 90.0)
    assert resolve_intrabar_exit(SHORT, 100.0, 102.0, 91.0, stop, tp, liq) is None


def test_liquidation_on_gap_open() -> None:
    # LONG: open below the liquidation price -> LIQUIDATION at liq (checked before the stop gap rule)
    assert resolve_intrabar_exit(LONG, 75.0, 76.0, 70.0, 97.0, 110.0, 80.0) == (ExitReason.LIQUIDATION, 80.0)
    # SHORT mirror
    assert resolve_intrabar_exit(SHORT, 125.0, 130.0, 120.0, 103.0, 90.0, 120.0) == (ExitReason.LIQUIDATION, 120.0)
    # inside the bar the stop still wins when it sits inside the liquidation price
    assert resolve_intrabar_exit(LONG, 100.0, 101.0, 75.0, 97.0, None, 80.0) == (ExitReason.STOP_LOSS, 97.0)
    # a (mis-configured) stop beyond liquidation: the low reaches liq first
    assert resolve_intrabar_exit(LONG, 100.0, 101.0, 75.0, 70.0, None, 80.0) == (ExitReason.LIQUIDATION, 80.0)
    assert resolve_intrabar_exit(SHORT, 100.0, 125.0, 99.0, 130.0, None, 120.0) == (ExitReason.LIQUIDATION, 120.0)


def test_flat_direction_rejected() -> None:
    with pytest.raises(ValueError):
        resolve_intrabar_exit(Direction.FLAT, 100.0, 101.0, 99.0, 97.0, None, None)


def test_funding_payment_sign() -> None:
    # positive rate: longs pay (+), shorts receive (-)
    assert funding_payment(0.1, 50_000.0, 0.0001) == pytest.approx(0.5)
    assert funding_payment(-0.1, 50_000.0, 0.0001) == pytest.approx(-0.5)
    # negative rate: longs receive, shorts pay
    assert funding_payment(0.1, 50_000.0, -0.0002) == pytest.approx(-1.0)
    assert funding_payment(-0.1, 50_000.0, -0.0002) == pytest.approx(1.0)


def test_liquidation_loss_nets_out_funding() -> None:
    qty, entry, leverage = 0.1, 50_000.0, 5
    im = qty * entry / leverage  # 1000
    assert liquidation_loss(qty, entry, leverage) == pytest.approx(-im)
    assert liquidation_loss(-qty, entry, leverage) == pytest.approx(-im)  # abs(qty)
    gross = liquidation_loss(qty, entry, leverage, funding_paid=5.0)
    assert gross == pytest.approx(-(im - 5.0))

    # booked through Trade.from_closure: net = gross - fees - funding = -IM - fees
    entry_fee = 2.5
    active = ActiveTrade(
        trade_id="t1", symbol="BTCUSDT", direction=LONG, qty=qty, entry_price=entry, entry_time=0,
        entry_bar_open_time=0, stop_price=49_000.0, take_profit_price=None, liquidation_price=40_160.0,
        leverage=leverage, risk_amount=100.0, entry_fee=entry_fee, entry_client_id="bt",
    )
    closure = PositionClosure(
        exit_time=3_600_000, exit_price=40_160.0, qty=qty, reason=ExitReason.LIQUIDATION, exit_fee=0.0,
        funding=5.0, gross_pnl=gross,
    )
    trade = Trade.from_closure(active, closure, source="backtest", run_id="bt-x")
    assert trade.net_pnl == pytest.approx(-im - entry_fee)
    # funding received (negative) is also netted: the loss is still exactly the initial margin
    gross_recv = liquidation_loss(qty, entry, leverage, funding_paid=-3.0)
    assert gross_recv - 0.0 - (-3.0) == pytest.approx(-im)


def test_liquidation_loss_rejects_bad_leverage() -> None:
    with pytest.raises(ValueError):
        liquidation_loss(0.1, 50_000.0, 0)
