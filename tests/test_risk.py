"""Risk: stops, sizing, decide_action, kill switch, cooldown (SPEC §8.5, §14.2 U3)."""

from __future__ import annotations

import dataclasses
import json
import math
from decimal import Decimal
from typing import Any

import pytest

from bot.config import FeeConfig, RiskConfig, StopLossConfig
from bot.models import Action, Direction, SignalAction, SymbolFilters
from bot.risk import (
    Cooldown,
    DailyLossKillSwitch,
    approx_liquidation_price,
    compute_stop_price,
    compute_take_profit,
    decide_action,
    plan_entry,
    stop_out_loss_per_unit,
)
from bot.timeutil import DAY_MS, INTERVAL_MS

D0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z (= 09:00 KST)
H = INTERVAL_MS["1h"]
FEES = FeeConfig(maker=0.0002, taker=0.0005)
SLIP_BPS = 5.0
PERCENT_2 = StopLossConfig(mode="percent", percent=2.0, atr_period=14, atr_multiple=2.0)
ATR_2X = StopLossConfig(mode="atr", percent=2.0, atr_period=14, atr_multiple=2.0)


def make_risk(**overrides: Any) -> RiskConfig:
    """The §8.5 worked-example risk config (explicit max notional 5000), with overrides."""
    base: dict[str, Any] = {
        "leverage": 3,
        "max_leverage": 10,
        "risk_per_trade_pct": 1.0,
        "stop_loss": PERCENT_2,
        "take_profit_r": 2.0,
        "max_position_notional": 5000.0,
        "max_margin_fraction": 0.9,
        "max_daily_loss_pct": 5.0,
        "kill_switch_flatten": True,
        "cooldown_bars_after_stop": 3,
        "min_liq_distance_multiple": 2.0,
        "maint_margin_rate": 0.004,
        "liq_mmr_buffer": 0.005,
    }
    base.update(overrides)
    return RiskConfig(**base)


def plan(
    filters: SymbolFilters,
    *,
    direction: Direction = Direction.LONG,
    ref_price: float = 50_000.0,
    equity: float = 10_000.0,
    atr_value: float | None = None,
    risk: RiskConfig | None = None,
    fees: FeeConfig = FEES,
    slippage_bps: float = SLIP_BPS,
):
    return plan_entry(
        direction=direction,
        ref_price=ref_price,
        equity=equity,
        atr_value=atr_value,
        filters=filters,
        risk=risk or make_risk(),
        fees=fees,
        slippage_bps=slippage_bps,
    )


# ---------------------------------------------------------------------------------------------
# Stops, take-profit, liquidation
# ---------------------------------------------------------------------------------------------


def test_stop_percent_long_short() -> None:
    assert compute_stop_price(50_000.0, Direction.LONG, PERCENT_2, None) == pytest.approx(49_000.0)
    assert compute_stop_price(50_000.0, Direction.SHORT, PERCENT_2, None) == pytest.approx(51_000.0)
    # percent mode ignores the ATR value entirely
    assert compute_stop_price(50_000.0, Direction.LONG, PERCENT_2, math.nan) == pytest.approx(49_000.0)
    with pytest.raises(ValueError):
        compute_stop_price(50_000.0, Direction.FLAT, PERCENT_2, None)


def test_stop_atr() -> None:
    assert compute_stop_price(50_000.0, Direction.LONG, ATR_2X, 500.0) == pytest.approx(49_000.0)
    assert compute_stop_price(50_000.0, Direction.SHORT, ATR_2X, 500.0) == pytest.approx(51_000.0)
    three_x = dataclasses.replace(ATR_2X, atr_multiple=3.0)
    assert compute_stop_price(100.0, Direction.LONG, three_x, 1.5) == pytest.approx(95.5)
    # a stop at or below zero is unusable
    assert compute_stop_price(100.0, Direction.LONG, ATR_2X, 60.0) is None


def test_stop_atr_nan_rejected(btc_filters: SymbolFilters) -> None:
    for bad in (None, math.nan, 0.0, -5.0, math.inf):
        assert compute_stop_price(50_000.0, Direction.LONG, ATR_2X, bad) is None
        assert compute_stop_price(50_000.0, Direction.SHORT, ATR_2X, bad) is None
    decision = plan(btc_filters, risk=make_risk(stop_loss=ATR_2X), atr_value=math.nan)
    assert not decision.ok
    assert decision.plan is None
    assert decision.reason == "stop_unavailable"
    assert plan(btc_filters, risk=make_risk(stop_loss=ATR_2X), atr_value=None).reason == "stop_unavailable"


def test_take_profit_r() -> None:
    assert compute_take_profit(50_000.0, 49_000.0, Direction.LONG, 2.0) == pytest.approx(52_000.0)
    assert compute_take_profit(50_000.0, 51_000.0, Direction.SHORT, 2.0) == pytest.approx(48_000.0)
    assert compute_take_profit(50_000.0, 49_000.0, Direction.LONG, 1.5) == pytest.approx(51_500.0)
    assert compute_take_profit(50_000.0, 49_000.0, Direction.LONG, None) is None


def test_liquidation_price_research_example() -> None:
    long_lp = approx_liquidation_price(84_000.0, Direction.LONG, 10, 0.004)
    short_lp = approx_liquidation_price(84_000.0, Direction.SHORT, 10, 0.004)
    assert long_lp == pytest.approx(75_903.6145, abs=1e-4)
    assert short_lp == pytest.approx(92_031.8725, abs=1e-4)
    assert round(long_lp, 2) == 75_903.61
    assert round(short_lp, 2) == 92_031.87
    with pytest.raises(ValueError):
        approx_liquidation_price(84_000.0, Direction.FLAT, 10, 0.004)
    with pytest.raises(ValueError):
        approx_liquidation_price(84_000.0, Direction.LONG, 0, 0.004)


# ---------------------------------------------------------------------------------------------
# plan_entry
# ---------------------------------------------------------------------------------------------


def test_plan_entry_reference_example(btc_filters: SymbolFilters) -> None:
    decision = plan(btc_filters)
    assert decision.ok and decision.reason == "ok"
    p = decision.plan
    assert p is not None
    assert p.symbol == "BTCUSDT"
    assert p.direction is Direction.LONG
    assert p.ref_price == 50_000.0
    assert p.stop_price == Decimal("49000.0")
    assert p.take_profit_price == Decimal("52000.0")
    assert isinstance(p.qty, Decimal) and p.qty == Decimal("0.090")
    assert p.qty.as_tuple().exponent == -3  # quantized to the market step
    assert p.sizing_cap == "risk"
    per_unit_loss = stop_out_loss_per_unit(Direction.LONG, 50_000.0, 49_000.0, FEES.taker, SLIP_BPS)
    assert per_unit_loss == pytest.approx(1_000 + 25 + 24.5 + 25.0125 + 24.48775, rel=1e-12)
    assert per_unit_loss == pytest.approx(1_099.00025, rel=1e-12)
    assert 10_000 * 0.01 / per_unit_loss == pytest.approx(0.0909918, rel=1e-6)  # qty_risk
    assert p.risk_amount == pytest.approx(98.9100225, rel=1e-12)
    assert p.notional == pytest.approx(4_500.0)
    assert p.leverage == 3
    assert p.liquidation_price == pytest.approx(33_636.06, abs=0.01)
    assert abs(p.ref_price - p.liquidation_price) >= 2 * 1_000
    # native types only (numpy boundary)
    assert type(p.notional) is float and type(p.risk_amount) is float and type(p.liquidation_price) is float


def test_per_unit_loss_short_mirror(btc_filters: SymbolFilters) -> None:
    per_unit_loss = stop_out_loss_per_unit(Direction.SHORT, 50_000.0, 51_000.0, FEES.taker, SLIP_BPS)
    assert per_unit_loss == pytest.approx(1_000 + 25 + 25.5 + 24.9875 + 25.51275, rel=1e-12)
    assert per_unit_loss == pytest.approx(1_101.00025, rel=1e-12)
    decision = plan(btc_filters, direction=Direction.SHORT)
    p = decision.plan
    assert decision.ok and p is not None
    assert p.direction is Direction.SHORT
    assert p.stop_price == Decimal("51000.0")
    assert p.take_profit_price == Decimal("48000.0")
    assert p.qty == Decimal("0.090")
    assert p.risk_amount == pytest.approx(0.09 * 1_101.00025, rel=1e-12)
    assert p.liquidation_price == pytest.approx(50_000 * (1 + 1 / 3) / (1 + 0.009))


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_clean_stop_out_books_exactly_minus_one_r(btc_filters: SymbolFilters, direction: Direction) -> None:
    """Replays the §9.1 fill-model accounting: entry at ref and exit at the stop, both slipped, taker fees."""
    p = plan(btc_filters, direction=direction).plan
    assert p is not None
    slip = SLIP_BPS / 10_000
    qty = float(p.qty)
    stop = float(p.stop_price)
    sign = direction.sign
    entry_fill = p.ref_price * (1 + sign * slip)  # BUY for long, SELL for short: adverse
    exit_fill = stop * (1 - sign * slip)  # closing side: adverse
    gross = sign * qty * (exit_fill - entry_fill)
    fees = qty * entry_fill * FEES.taker + qty * exit_fill * FEES.taker
    assert (gross - fees) / p.risk_amount == pytest.approx(-1.0, abs=1e-9)


def test_qty_floored_to_step(btc_filters: SymbolFilters) -> None:
    # qty_risk = 109.8 / 1099.00025 = 0.0999090.. -> floored to 0.099 (never rounded up to 0.100)
    decision = plan(btc_filters, equity=10_980.0, risk=make_risk(max_position_notional=20_000.0))
    p = decision.plan
    assert p is not None
    raw = 10_980.0 * 0.01 / 1_099.00025
    assert raw == pytest.approx(0.099909, abs=1e-6)
    assert p.qty == Decimal("0.099")
    assert p.qty.as_tuple().exponent == -3
    assert float(p.qty) <= raw < float(p.qty) + 0.001
    assert p.risk_amount <= 10_980.0 * 0.01
    assert p.sizing_cap == "risk"


def test_notional_cap_binds(btc_filters: SymbolFilters) -> None:
    p = plan(btc_filters, risk=make_risk(max_position_notional=2_000.0)).plan
    assert p is not None
    assert p.sizing_cap == "notional"
    assert p.qty == Decimal("0.040")
    assert p.notional == pytest.approx(2_000.0)
    assert p.risk_amount == pytest.approx(0.04 * 1_099.00025)


def test_margin_cap_binds(btc_filters: SymbolFilters) -> None:
    # qty_margin = 10000 * 0.1 * 1 / 50000 = 0.02 < qty_risk 0.0909 < qty_notional 0.1
    p = plan(btc_filters, risk=make_risk(leverage=1, max_margin_fraction=0.1)).plan
    assert p is not None
    assert p.sizing_cap == "margin"
    assert p.qty == Decimal("0.020")
    assert p.leverage == 1


def test_sizing_cap_tie_resolved_in_order(btc_filters: SymbolFilters) -> None:
    # qty_margin == qty_notional == 0.1 exactly; qty_risk (5 %) is larger -> "margin" wins the tie
    risk = make_risk(leverage=1, max_margin_fraction=0.5, max_position_notional=5_000.0, risk_per_trade_pct=5.0)
    p = plan(btc_filters, risk=risk).plan
    assert p is not None
    assert p.sizing_cap == "margin"
    assert p.qty == Decimal("0.100")


def test_below_min_notional_rejected_not_sized_up(btc_filters: SymbolFilters) -> None:
    # ref 20000: qty_risk = 1 / 439.6 = 0.00227 -> 0.002 -> notional 40 < min_notional 50: rejected, not sized up
    decision = plan(btc_filters, ref_price=20_000.0, equity=100.0)
    assert not decision.ok
    assert decision.plan is None
    assert decision.reason == "below_min_notional"
    # qty that floors to zero
    assert plan(btc_filters, equity=20.0).reason == "below_min_qty"


def test_liquidation_too_close_rejected(btc_filters: SymbolFilters) -> None:
    stop_5 = dataclasses.replace(PERCENT_2, percent=5.0)
    tight = make_risk(leverage=20, max_leverage=20, stop_loss=stop_5)
    for direction in (Direction.LONG, Direction.SHORT):
        decision = plan(btc_filters, direction=direction, risk=tight)
        assert decision.reason == "liquidation_too_close", direction
        assert decision.plan is None
    # the same 5 % stop is fine at 3x
    assert plan(btc_filters, risk=make_risk(stop_loss=stop_5)).ok


def test_plan_entry_input_rejections(btc_filters: SymbolFilters) -> None:
    for equity in (0.0, -1.0, math.nan):
        assert plan(btc_filters, equity=equity).reason == "no_equity"
    for ref in (0.0, -1.0, math.nan):
        assert plan(btc_filters, ref_price=ref).reason == "bad_price"
    with pytest.raises(ValueError):
        plan(btc_filters, direction=Direction.FLAT)


def test_invalid_stop_when_rounded_onto_entry(btc_filters: SymbolFilters) -> None:
    # ATR stop 0.02 below entry is rounded toward the entry onto the entry price itself
    decision = plan(btc_filters, risk=make_risk(stop_loss=ATR_2X), atr_value=0.01)
    assert decision.reason == "invalid_stop"


def test_invalid_take_profit(btc_filters: SymbolFilters) -> None:
    # TP a hair above the entry rounds (toward the entry) onto the entry
    assert plan(btc_filters, risk=make_risk(take_profit_r=0.00001)).reason == "invalid_take_profit"
    # a short TP below zero cannot exist
    huge_r = make_risk(take_profit_r=60.0)
    assert plan(btc_filters, direction=Direction.SHORT, risk=huge_r).reason == "invalid_take_profit"
    # no TP configured -> plan without take-profit
    p = plan(btc_filters, risk=make_risk(take_profit_r=None)).plan
    assert p is not None and p.take_profit_price is None


def test_plan_entry_with_example_config(app_config, btc_filters: SymbolFilters) -> None:
    """config.example.yaml defaults (ATR 2x stop, max notional 20000)."""
    decision = plan_entry(
        direction=Direction.LONG,
        ref_price=50_000.0,
        equity=10_000.0,
        atr_value=500.0,
        filters=btc_filters,
        risk=app_config.risk,
        fees=app_config.execution.fees,
        slippage_bps=app_config.execution.slippage_bps,
    )
    p = decision.plan
    assert decision.ok and p is not None
    assert p.stop_price == Decimal("49000.0")
    assert p.take_profit_price == Decimal("52000.0")
    assert p.qty == Decimal("0.090")
    assert p.sizing_cap == "risk"


def test_protective_prices_rounded_toward_entry(btc_filters: SymbolFilters) -> None:
    # ATR stop at 50000 - 2*500.035 = 48999.93 -> rounded UP (toward entry) to 49000.0 ; TP 2R from 49000.0
    p = plan(btc_filters, risk=make_risk(stop_loss=ATR_2X), atr_value=500.035).plan
    assert p is not None
    assert p.stop_price == Decimal("49000.0")
    assert p.take_profit_price == Decimal("52000.0")
    p_short = plan(btc_filters, direction=Direction.SHORT, risk=make_risk(stop_loss=ATR_2X), atr_value=500.035).plan
    assert p_short is not None
    assert p_short.stop_price == Decimal("51000.0")  # 51000.07 rounded DOWN toward entry


# ---------------------------------------------------------------------------------------------
# decide_action
# ---------------------------------------------------------------------------------------------

_TABLE: dict[tuple[SignalAction, Direction], tuple[Action, Action]] = {
    # (signal, position): (allowed, blocked)
    (SignalAction.NONE, Direction.FLAT): (Action.NONE, Action.NONE),
    (SignalAction.NONE, Direction.LONG): (Action.NONE, Action.NONE),
    (SignalAction.NONE, Direction.SHORT): (Action.NONE, Action.NONE),
    (SignalAction.LONG, Direction.FLAT): (Action.OPEN_LONG, Action.NONE),
    (SignalAction.LONG, Direction.LONG): (Action.NONE, Action.NONE),
    (SignalAction.LONG, Direction.SHORT): (Action.FLIP_LONG, Action.CLOSE),
    (SignalAction.SHORT, Direction.FLAT): (Action.OPEN_SHORT, Action.NONE),
    (SignalAction.SHORT, Direction.LONG): (Action.FLIP_SHORT, Action.CLOSE),
    (SignalAction.SHORT, Direction.SHORT): (Action.NONE, Action.NONE),
    (SignalAction.CLOSE, Direction.FLAT): (Action.NONE, Action.NONE),
    (SignalAction.CLOSE, Direction.LONG): (Action.CLOSE, Action.CLOSE),
    (SignalAction.CLOSE, Direction.SHORT): (Action.NONE, Action.NONE),  # CLOSE never closes a short
}
_CASES = [
    (sig, pos, allowed, expected[0] if allowed else expected[1])
    for (sig, pos), expected in _TABLE.items()
    for allowed in (True, False)
]


@pytest.mark.parametrize(("signal", "position", "allowed", "expected"), _CASES)
def test_decide_action_table(signal: SignalAction, position: Direction, allowed: bool, expected: Action) -> None:
    assert len(_CASES) == 24
    assert decide_action(signal, position, allowed) is expected
    # plain strings (e.g. read back from storage) are accepted too
    assert decide_action(SignalAction(signal).value, Direction(position).value, allowed) is expected


# ---------------------------------------------------------------------------------------------
# Daily loss kill switch
# ---------------------------------------------------------------------------------------------


def test_kill_switch_trips_at_threshold() -> None:
    ks = DailyLossKillSwitch(5.0)
    ks.seed(10_000.0)
    assert ks.entries_allowed
    assert ks.update(D0 + H - 1, 9_501.0) is False  # -4.99 %
    assert ks.update(D0 + 2 * H - 1, 9_500.5) is False  # -4.995 %
    assert ks.entries_allowed and not ks.tripped
    assert ks.update(D0 + 3 * H - 1, 9_500.0) is True  # -5 % exactly
    assert ks.tripped and not ks.entries_allowed
    assert ks.tripped_at == D0 + 3 * H - 1
    assert ks.reason == "daily loss 5.00% >= 5.0%"
    assert ks.update(D0 + 4 * H - 1, 9_000.0) is False  # already tripped: not newly
    assert ks.update(D0 + 5 * H - 1, 10_500.0) is False  # recovering intraday does not re-arm
    assert ks.tripped and ks.tripped_at == D0 + 3 * H - 1


def test_kill_switch_disabled_with_zero_pct() -> None:
    ks = DailyLossKillSwitch(0.0)
    ks.seed(10_000.0)
    assert ks.update(D0 + H - 1, 1_000.0) is False
    assert ks.entries_allowed
    with pytest.raises(ValueError):
        DailyLossKillSwitch(100.0)
    with pytest.raises(ValueError):
        DailyLossKillSwitch(-1.0)


def test_kill_switch_baseline_is_previous_day_last_equity() -> None:
    ks = DailyLossKillSwitch(5.0)
    ks.seed(10_000.0)
    for k in range(24):  # day D: equity climbs from 10000 to 11000 on 1h bars
        assert ks.update(D0 + (k + 1) * H - 1, 10_000.0 + k * 1_000.0 / 23) is False
    assert ks.day == "2024-01-01"
    assert ks.day_start_equity == 10_000.0
    assert ks.last_equity == pytest.approx(11_000.0)
    # first bar of D+1 closes at 10340: -6 % vs the previous day's last equity (but +3.4 % vs the seed) -> trips
    assert ks.update(D0 + DAY_MS + H - 1, 10_340.0) is True
    assert ks.day == "2024-01-02"
    assert ks.day_start_equity == pytest.approx(11_000.0)
    assert ks.reason == "daily loss 6.00% >= 5.0%"


def test_kill_switch_trips_on_1d_single_bar(candle_factory) -> None:
    df = candle_factory([100.0, 100.0, 94.0, 94.0], interval="1d")
    closes = [int(t) for t in df["close_time"]]
    ks = DailyLossKillSwitch(5.0)
    ks.seed(10_000.0)
    assert ks.update(closes[0], 10_000.0) is False
    assert ks.update(closes[1], 9_400.0) is True  # a single 1d bar closing 6 % lower trips on that bar
    assert ks.tripped_at == closes[1]
    # next UTC day re-arms (baseline = 9400)
    assert ks.update(closes[2], 9_400.0) is False
    assert ks.entries_allowed
    # the very first bar also counts when seeded (baseline = seed)
    first = DailyLossKillSwitch(5.0)
    first.seed(10_000.0)
    assert first.update(closes[0], 9_400.0) is True


def test_kill_switch_unseeded_first_bar_is_its_own_baseline() -> None:
    ks = DailyLossKillSwitch(5.0)
    assert ks.update(D0 + H - 1, 9_000.0) is False
    assert ks.day_start_equity == 9_000.0
    ks.seed(1.0)  # no-op: last_equity already known
    assert ks.last_equity == 9_000.0


def test_kill_switch_resets_next_utc_day() -> None:
    ks = DailyLossKillSwitch(5.0)
    ks.seed(10_000.0)
    assert ks.update(D0 + H - 1, 9_400.0) is True
    assert ks.update(D0 + DAY_MS - 1, 9_300.0) is False  # last ms of the UTC day: still day D
    assert ks.tripped
    # 00:00 UTC (09:00 KST): re-armed, baseline = last equity of day D
    assert ks.update(D0 + DAY_MS + H - 1, 9_300.0) is False
    assert not ks.tripped and ks.entries_allowed
    assert ks.tripped_at is None and ks.reason is None
    assert ks.day == "2024-01-02"
    assert ks.day_start_equity == 9_300.0
    assert ks.update(D0 + DAY_MS + 2 * H - 1, 9_300.0 * 0.95 - 1) is True


def test_kill_switch_roundtrip() -> None:
    ks = DailyLossKillSwitch(5.0)
    ks.seed(10_000.0)
    ks.update(D0 + H - 1, 9_400.0)
    d = ks.to_dict()
    assert d["last_equity"] == 9_400.0
    assert {"day", "day_start_equity", "last_equity", "tripped", "tripped_at", "reason"} <= set(d)
    restored = DailyLossKillSwitch.from_dict(json.loads(json.dumps(d)), 5.0)
    assert restored.to_dict() == d
    assert restored.tripped and not restored.entries_allowed
    restored.seed(1.0)  # persisted last_equity wins over the startup seed
    assert restored.last_equity == 9_400.0
    assert restored.update(D0 + 2 * H - 1, 9_999.0) is False and restored.tripped  # same day: still tripped
    # threshold always comes from the current config
    assert DailyLossKillSwitch.from_dict(d, 3.0).max_daily_loss_pct == 3.0
    # legacy state without last_equity is tolerated; seed() then applies
    legacy = {k: v for k, v in d.items() if k != "last_equity"}
    old = DailyLossKillSwitch.from_dict(legacy, 5.0)
    assert old.last_equity is None
    old.seed(9_100.0)
    assert old.last_equity == 9_100.0
    empty = DailyLossKillSwitch.from_dict({}, 5.0)
    assert empty.day is None and not empty.tripped and empty.last_equity is None


# ---------------------------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------------------------


def test_cooldown_semantics() -> None:
    t = D0 + 10 * H
    cd = Cooldown(3, H)
    assert not cd.active(t)
    cd.trigger(t)
    assert cd.until_ms == t + 3 * H
    # decisions at the close of T, T+i, T+2i are blocked; T+3i may open (fill at T+4i)
    assert [cd.active(t + k * H) for k in range(5)] == [True, True, True, False, False]
    restored = Cooldown.from_dict(json.loads(json.dumps(cd.to_dict())), 3, H)
    assert restored.until_ms == cd.until_ms
    assert [restored.active(t + k * H) for k in range(5)] == [True, True, True, False, False]
    assert Cooldown.from_dict({}, 3, H).until_ms is None
    # a later stop-out extends; an earlier one never shortens a running cooldown
    cd.trigger(t + 5 * H)
    assert cd.until_ms == t + 8 * H
    cd.trigger(t)
    assert cd.until_ms == t + 8 * H


def test_cooldown_zero_blocks_nothing() -> None:
    t = D0 + 10 * H
    cd = Cooldown(0, H)
    cd.trigger(t)
    assert not cd.active(t)
    assert not cd.active(t + H)
    with pytest.raises(ValueError):
        Cooldown(-1, H)
    with pytest.raises(ValueError):
        Cooldown(3, 0)
