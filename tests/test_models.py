"""Tests for bot/models.py and bot/errors.py (SPEC §4.1, §4.2, §14.2 U1)."""

from __future__ import annotations

import ast
import json
import math
import pickle
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pandas as pd
import pytest

from bot import errors
from bot.errors import DataError
from bot.models import (
    CLIENT_ID_RE,
    KLINE_COLUMNS,
    KLINE_DTYPES,
    METRIC_LABELS_KO,
    PERCENT_METRICS,
    TRADE_COLUMNS,
    AccountSnapshot,
    Action,
    ActiveTrade,
    BotState,
    Candle,
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
    RiskDecision,
    Side,
    Signal,
    SignalAction,
    SymbolFilters,
    Trade,
    TradePlan,
    candles_from_df,
    client_id_prefix,
    make_client_id,
    next_client_id,
    symbol_tag,
    to_jsonable,
    validate_candles_df,
)
from tests.conftest import REPO_ROOT

BAR_MS = 1_790_769_600_000


def make_active(direction: Direction = Direction.LONG, **overrides: object) -> ActiveTrade:
    values: dict[str, object] = dict(
        trade_id="paper-BTCUSDT-1790769600000-L",
        symbol="BTCUSDT",
        direction=direction,
        qty=0.1,
        entry_price=50_000.0,
        entry_time=BAR_MS,
        entry_bar_open_time=BAR_MS,
        stop_price=49_000.0 if direction is Direction.LONG else 51_000.0,
        take_profit_price=52_000.0 if direction is Direction.LONG else 48_000.0,
        liquidation_price=33_636.06 if direction is Direction.LONG else 66_000.0,
        leverage=3,
        risk_amount=110.0,
        entry_fee=2.5,
        entry_client_id="mab1-4314-EN-1790766000-0",
        protect_seq=1,
        entry_order_id="123456789",
    )
    values.update(overrides)
    return ActiveTrade(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# Client ids
# ---------------------------------------------------------------------------------------------


def test_make_client_id_format() -> None:
    cid = make_client_id("mab1", "BTCUSDT", "EN", 1_790_769_600_000)
    assert cid == "mab1-4314-EN-1790769600-0"
    assert len(cid) <= 36
    assert CLIENT_ID_RE.fullmatch(cid)
    np_cid = make_client_id("mab1", "BTCUSDT", "SL", np.int64(1_790_769_600_000), np.int64(2))
    assert np_cid == "mab1-4314-SL-1790769600-2"
    assert ".0" not in np_cid
    assert make_client_id("mab1", "BTCUSDT", "TP", np.float64(1_790_769_600_000.0), 3) == "mab1-4314-TP-1790769600-3"
    longest = make_client_id("ABCDEFGH", "1000000PEPEUSDT", "KS", 9_999_999_999_000, 9999)
    assert len(longest) == 32 and CLIENT_ID_RE.fullmatch(longest)
    for kind in ("EN", "EX", "SL", "TP", "FL", "KS"):
        assert f"-{kind}-" in make_client_id("mab1", "BTCUSDT", kind, BAR_MS)


def test_make_client_id_rejects_bad_bot_id() -> None:
    for bad in ("", "toolongid", "ab-1", "ab_1", "ab c", "봇1"):
        with pytest.raises(ValueError):
            make_client_id(bad, "BTCUSDT", "EN", BAR_MS)
    with pytest.raises(ValueError):
        make_client_id("mab1", "BTCUSDT", "XX", BAR_MS)
    with pytest.raises(ValueError):
        make_client_id("mab1", "BTCUSDT", "EN", BAR_MS, -1)
    with pytest.raises(ValueError):
        make_client_id("mab1", "BTCUSDT", "EN", -1)


def test_client_ids_differ_per_symbol() -> None:
    btc = make_client_id("mab1", "BTCUSDT", "EN", BAR_MS)
    eth = make_client_id("mab1", "ETHUSDT", "EN", BAR_MS)
    assert btc != eth
    assert symbol_tag("BTCUSDT") == "4314"
    assert symbol_tag("BTCUSDT") != symbol_tag("ETHUSDT")
    assert len(symbol_tag("ETHUSDT")) == 4


def test_next_client_id_increments_seq() -> None:
    assert next_client_id("mab1-4314-FL-1790769600-0") == "mab1-4314-FL-1790769600-1"
    assert next_client_id("mab1-4314-FL-1790769600-9") == "mab1-4314-FL-1790769600-10"
    with pytest.raises(ValueError):
        next_client_id("mab1-4314-FL-1790769600-x")
    with pytest.raises(ValueError):
        next_client_id("noseq")


def test_client_id_prefix() -> None:
    prefix = client_id_prefix("mab1", "BTCUSDT")
    assert prefix == "mab1-4314-"
    assert make_client_id("mab1", "BTCUSDT", "SL", BAR_MS, 3).startswith(prefix)
    assert not make_client_id("mab1", "ETHUSDT", "SL", BAR_MS, 3).startswith(prefix)
    assert not make_client_id("mab2", "BTCUSDT", "SL", BAR_MS, 3).startswith(prefix)


# ---------------------------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------------------------


def test_enum_values_are_explicit_upper() -> None:
    assert Side.BUY == "BUY" and Side.SELL.value == "SELL"
    assert Mode.PAPER == "paper" and Mode.TESTNET == "testnet" and Mode.LIVE == "live"
    for enum_cls in (Side, Direction, SignalAction, Action, OrderType, OrderPurpose, OrderStatus, ExitReason, BotState):
        for member in enum_cls:
            assert member.value == member.name, (enum_cls, member)
            assert member.value.isupper()
    assert str(Direction.LONG) == "LONG"
    assert f"{OrderStatus.EXPIRED_IN_MATCH}" == "EXPIRED_IN_MATCH"
    assert {m.value for m in ExitReason} >= {"PROTECTION_FAILED", "KILL_SWITCH", "END_OF_DATA"}


def test_direction_helpers() -> None:
    assert Direction.LONG.sign == 1 and Direction.SHORT.sign == -1 and Direction.FLAT.sign == 0
    assert Direction.LONG.opening_side is Side.BUY and Direction.LONG.closing_side is Side.SELL
    assert Direction.SHORT.opening_side is Side.SELL and Direction.SHORT.closing_side is Side.BUY
    with pytest.raises(ValueError):
        _ = Direction.FLAT.opening_side
    with pytest.raises(ValueError):
        _ = Direction.FLAT.closing_side
    assert Direction.from_qty(0.5) is Direction.LONG
    assert Direction.from_qty(-0.001) is Direction.SHORT
    assert Direction.from_qty(0) is Direction.FLAT
    assert Direction.from_qty(1e-13) is Direction.FLAT
    assert Direction.from_qty(np.float64(-2.0)) is Direction.SHORT
    pos = Position("BTCUSDT", -0.2, 50_000.0, 50_100.0, -20.0, 60_000.0, 3_333.3, 3, BAR_MS)
    assert pos.direction is Direction.SHORT


# ---------------------------------------------------------------------------------------------
# Trade booking
# ---------------------------------------------------------------------------------------------


def test_trade_from_closure_long() -> None:
    active = make_active(Direction.LONG)
    closure = PositionClosure(
        exit_time=BAR_MS + 7_200_000,
        exit_price=51_000.0,
        qty=0.1,
        reason=ExitReason.TAKE_PROFIT,
        exit_fee=2.55,
        funding=1.0,
        gross_pnl=None,
    )
    trade = Trade.from_closure(active, closure, source="paper")
    assert trade.gross_pnl == pytest.approx(100.0)
    assert trade.fees == pytest.approx(5.05)
    assert trade.funding == 1.0
    assert trade.net_pnl == pytest.approx(100.0 - 5.05 - 1.0)
    assert trade.r_multiple == pytest.approx((100.0 - 5.05 - 1.0) / 110.0)
    assert trade.direction is Direction.LONG
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert trade.initial_stop == 49_000.0 and trade.take_profit == 52_000.0
    assert trade.qty == 0.1 and trade.leverage == 3
    assert trade.entry_time == BAR_MS and trade.exit_time == BAR_MS + 7_200_000
    assert trade.trade_id == active.trade_id and trade.source == "paper" and trade.run_id is None
    d = trade.to_dict()
    assert tuple(d) == TRADE_COLUMNS
    assert d["direction"] == "LONG" and d["exit_reason"] == "TAKE_PROFIT"
    assert type(d["direction"]) is str
    assert Trade.from_dict(d) == trade
    assert Trade.from_dict(json.loads(json.dumps(d))) == trade


def test_trade_from_closure_short() -> None:
    active = make_active(Direction.SHORT, risk_amount=0.0)
    closure = PositionClosure(
        exit_time=BAR_MS + 3_600_000,
        exit_price=51_000.0,
        qty=0.1,
        reason=ExitReason.STOP_LOSS,
        exit_fee=2.55,
        funding=-0.5,
        gross_pnl=None,
    )
    trade = Trade.from_closure(active, closure, source=Mode.TESTNET, run_id="r1")
    assert trade.gross_pnl == pytest.approx(-100.0)
    assert trade.net_pnl == pytest.approx(-100.0 - 5.05 + 0.5)
    assert trade.r_multiple is None  # risk_amount == 0
    assert trade.source == "testnet" and type(trade.source) is str
    assert trade.run_id == "r1"


def test_trade_from_closure_prefers_exchange_gross() -> None:
    active = make_active(Direction.LONG)
    closure = PositionClosure(
        exit_time=BAR_MS + 3_600_000,
        exit_price=51_000.0,
        qty=0.1,
        reason=ExitReason.MANUAL,
        exit_fee=0.0,
        funding=0.0,
        gross_pnl=-3_000.0,
    )
    trade = Trade.from_closure(active, closure, source="live")
    assert trade.gross_pnl == -3_000.0
    assert trade.net_pnl == pytest.approx(-3_000.0 - 2.5)


# ---------------------------------------------------------------------------------------------
# JSON conversion
# ---------------------------------------------------------------------------------------------


def test_to_jsonable_handles_enum_decimal_nan_dataclass() -> None:
    signal = Signal(SignalAction.LONG, BAR_MS, 50_000.0, "golden_cross", {"ma_fast": 1.5, "ma_slow": float("nan")})
    account = AccountSnapshot(
        ts=BAR_MS,
        wallet_balance=10_000.0,
        equity=10_050.0,
        available_balance=9_000.0,
        unrealized_pnl=50.0,
        position=Position("BTCUSDT", 0.1, 50_000.0, 50_500.0, 50.0, None, 1_666.7, 3, BAR_MS),
        protective_orders=(
            ProtectiveOrder(OrderPurpose.STOP_LOSS, "mab1-4314-SL-1790769600-1", None, Side.SELL, 49_000.0, "NEW", True, None),
        ),
    )
    data = {
        "mode": Mode.PAPER,
        "dec": Decimal("0.10"),
        "nan": float("nan"),
        "inf": float("inf"),
        "tuple": (1, Direction.SHORT),
        "path": Path("data") / "bot.db",
        "proxy": MappingProxyType({"a": 1}),
        "signal": signal,
        "account": account,
        Side.BUY: "enum key",
    }
    out = to_jsonable(data)
    assert out["mode"] == "paper" and type(out["mode"]) is str
    assert out["dec"] == "0.10"
    assert out["nan"] is None and out["inf"] is None
    assert out["tuple"] == [1, "SHORT"]
    assert out["path"] == str(Path("data") / "bot.db")
    assert out["proxy"] == {"a": 1}
    assert out["signal"] == {
        "action": "LONG",
        "bar_open_time": BAR_MS,
        "price": 50_000.0,
        "reason": "golden_cross",
        "meta": {"ma_fast": 1.5, "ma_slow": None},
    }
    assert out["account"]["position"]["qty"] == 0.1
    assert out["account"]["position"]["liquidation_price"] is None
    assert out["account"]["protective_orders"][0]["kind"] == "STOP_LOSS"
    assert out["BUY"] == "enum key"
    json.dumps(out, allow_nan=False)
    # DataFrame -> list of records
    df = pd.DataFrame({"time": np.array([1, 2], dtype="int64"), "equity": [1.0, float("nan")], "flag": [True, False]})
    assert to_jsonable(df) == [{"time": 1, "equity": 1.0, "flag": True}, {"time": 2, "equity": None, "flag": False}]
    # unknown objects are returned unchanged
    marker = object()
    assert to_jsonable(marker) is marker


def test_to_jsonable_converts_numpy_scalars() -> None:
    out = to_jsonable({"i": np.int64(7), "f": np.float64("nan"), "b": np.bool_(True), "g": np.float32(1.5), "arr": np.array([1, 2])})
    assert out == {"i": 7, "f": None, "b": True, "g": 1.5, "arr": [1, 2]}
    assert type(out["i"]) is int and type(out["b"]) is bool and type(out["g"]) is float
    assert to_jsonable(np.int64(5)) == 5 and type(to_jsonable(np.int64(5))) is int
    assert to_jsonable(np.float64(2.5)) == 2.5 and type(to_jsonable(np.float64(2.5))) is float
    json.dumps(out, allow_nan=False)


# ---------------------------------------------------------------------------------------------
# Candles / signals coercion
# ---------------------------------------------------------------------------------------------


def test_candle_from_row_coerces_numpy(candle_factory: Callable[..., pd.DataFrame]) -> None:
    df = candle_factory([100.0, 101.0, 102.0])
    row = df.iloc[1]  # mixed dtypes -> a float64 Series: open_time comes back as numpy.float64
    assert isinstance(row["open_time"], np.floating)
    candle = Candle.from_row(row)
    assert type(candle.open_time) is int and type(candle.close_time) is int
    assert candle.open_time == int(df.open_time.iloc[1])
    assert type(candle.close) is float and candle.close == 101.0
    direct = Candle(np.int64(1), np.float64(2.0), 3, 1, 2, 5, np.int64(2))
    assert type(direct.open_time) is int and type(direct.high) is float
    candles = candles_from_df(df)
    assert len(candles) == 3
    assert all(type(c.open_time) is int and type(c.close_time) is int for c in candles)
    assert candles[2] == Candle.from_row(df.iloc[2].to_dict())
    assert candles_from_df(df.iloc[0:0]) == []


def test_signal_coerces_numpy() -> None:
    sig = Signal(
        SignalAction.SHORT,
        np.int64(BAR_MS),
        np.float64(123.5),
        "dead_cross",
        {"ma_fast": np.float64(1.25), "ma_slow": np.float64("nan"), "n": np.int64(3)},
    )
    assert type(sig.bar_open_time) is int and sig.bar_open_time == BAR_MS
    assert type(sig.price) is float
    assert type(sig.meta["ma_fast"]) is float and math.isnan(sig.meta["ma_slow"])
    assert type(sig.meta["n"]) is float
    assert Signal("NONE", BAR_MS, 1.0).action is SignalAction.NONE
    assert Signal(SignalAction.NONE, BAR_MS, 1.0).meta == {}


def test_signal_roundtrip() -> None:
    sig = Signal(SignalAction.LONG, BAR_MS, 50_000.5, "golden_cross", {"ma_fast": 1.0, "ma_slow": float("nan")})
    d = sig.to_dict()
    text = json.dumps(d, allow_nan=False)
    back = Signal.from_dict(json.loads(text))
    assert back.action is SignalAction.LONG
    assert back.bar_open_time == BAR_MS and back.price == 50_000.5 and back.reason == "golden_cross"
    assert back.meta["ma_fast"] == 1.0 and math.isnan(back.meta["ma_slow"])
    assert to_jsonable(sig) == d


# ---------------------------------------------------------------------------------------------
# Mutable state / outcomes / filters
# ---------------------------------------------------------------------------------------------


def test_active_trade_roundtrip() -> None:
    active = make_active(Direction.SHORT)
    d = active.to_dict()
    assert d["direction"] == "SHORT" and d["entry_order_id"] == "123456789" and d["protect_seq"] == 1
    back = ActiveTrade.from_dict(json.loads(json.dumps(d)))
    assert back == active
    # tolerate a missing entry_order_id (state written before the field existed / adopted)
    d_old = dict(d)
    del d_old["entry_order_id"]
    old = ActiveTrade.from_dict(d_old)
    assert old.entry_order_id is None
    assert old.protect_seq == 1
    # adopted variant
    adopted = make_active(Direction.LONG, protect_seq=0, entry_order_id=None, take_profit_price=None, liquidation_price=None)
    assert ActiveTrade.from_dict(adopted.to_dict()) == adopted
    # numpy values are coerced at construction; exchange order ids become str
    coerced = make_active(qty=np.float64(0.2), entry_time=np.int64(BAR_MS), entry_order_id=987)
    assert type(coerced.qty) is float and type(coerced.entry_time) is int and coerced.entry_order_id == "987"
    # mutable
    active.protect_seq += 1
    assert active.protect_seq == 2


def test_open_outcome_defaults() -> None:
    out = OpenOutcome(filled=False)
    assert out.filled is False
    assert out.qty == 0.0 and out.avg_price == 0.0 and out.entry_fee == 0.0 and out.entry_time == 0
    assert out.entry_order is None and out.protective == () and out.message == ""
    filled = OpenOutcome(filled=True, qty=0.09, avg_price=50_025.0, entry_fee=2.25, entry_time=BAR_MS, message="ok")
    assert filled.filled and filled.qty == 0.09


def test_symbol_filters_roundtrip(btc_filters: SymbolFilters) -> None:
    d = btc_filters.to_dict()
    assert all(isinstance(v, str) for v in d.values())
    assert d["tick_size"] == "0.10" and d["multiplier_up"] == "1.0500" and d["contract_type"] == "PERPETUAL"
    assert SymbolFilters.from_dict(d) == btc_filters
    # downloader cache adds metadata keys, which are ignored
    cached = json.loads(json.dumps(d | {"fetched_at": BAR_MS, "host": "https://fapi.binance.com"}))
    assert SymbolFilters.from_dict(cached) == btc_filters
    assert isinstance(SymbolFilters.from_dict(cached).tick_size, Decimal)
    with pytest.raises(DataError):
        SymbolFilters.from_dict({k: v for k, v in d.items() if k != "step_size"})
    with pytest.raises(DataError):
        SymbolFilters.from_dict(d | {"min_qty": "abc"})


def test_risk_decision_and_trade_plan() -> None:
    plan = TradePlan(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        ref_price=50_000.0,
        qty=Decimal("0.090"),
        stop_price=Decimal("49000.0"),
        take_profit_price=Decimal("52000.0"),
        notional=4_500.0,
        risk_amount=98.9100225,
        leverage=3,
        liquidation_price=33_636.06,
    )
    assert plan.sizing_cap == "risk"
    assert RiskDecision(plan, "ok").ok
    assert not RiskDecision(None, "below_min_notional").ok


def test_order_result_raw_not_in_repr() -> None:
    res = OrderResult(
        client_id="mab1-4314-EN-1790769600-0",
        exchange_id="1",
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        purpose=OrderPurpose.ENTRY,
        status=OrderStatus.FILLED,
        requested_qty=0.1,
        executed_qty=0.1,
        avg_price=50_000.0,
        trigger_price=None,
        fee=2.5,
        ts=BAR_MS,
        raw={"orderId": 1, "clientOrderId": "x"},
    )
    assert "clientOrderId" not in repr(res)
    assert to_jsonable(res)["raw"] == {"orderId": 1, "clientOrderId": "x"}


# ---------------------------------------------------------------------------------------------
# Candle frame validation
# ---------------------------------------------------------------------------------------------


def test_validate_candles_df_rejects_unsorted_and_dupes(candle_factory: Callable[..., pd.DataFrame]) -> None:
    df = candle_factory([1.0, 2.0, 3.0, 4.0])
    validate_candles_df(df, 3_600_000)
    validate_candles_df(df)
    validate_candles_df(df.iloc[0:0])

    unsorted = df.iloc[[1, 0, 2, 3]].reset_index(drop=True)
    with pytest.raises(DataError, match="sorted"):
        validate_candles_df(unsorted)

    dupes = pd.concat([df, df.iloc[[3]]], ignore_index=True)
    with pytest.raises(DataError, match="duplicate"):
        validate_candles_df(dupes)

    with pytest.raises(DataError, match="close_time"):
        validate_candles_df(df, 900_000)

    with pytest.raises(DataError, match="missing columns"):
        validate_candles_df(df.drop(columns=["trades"]))

    wrong_dtype = df.astype({"open_time": "float64"})
    with pytest.raises(DataError, match="dtypes"):
        validate_candles_df(wrong_dtype)

    with pytest.raises(DataError):
        validate_candles_df([1, 2, 3])  # type: ignore[arg-type]

    extra = df.assign(ma_fast=1.0)  # strategies may append indicator columns
    validate_candles_df(extra, 3_600_000)
    assert tuple(KLINE_DTYPES) == KLINE_COLUMNS


def test_metric_labels_and_percent_metrics() -> None:
    assert METRIC_LABELS_KO["total_return"] == "총 수익률"
    assert METRIC_LABELS_KO["funding_events"] == "펀딩 적용 횟수"
    assert PERCENT_METRICS == frozenset({"total_return", "cagr", "max_drawdown", "win_rate", "exposure"})
    assert PERCENT_METRICS <= set(METRIC_LABELS_KO)
    assert len(METRIC_LABELS_KO) == 30


# ---------------------------------------------------------------------------------------------
# errors.py
# ---------------------------------------------------------------------------------------------


def _inside_type_checking(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.If):
            test = current.test
            if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            ):
                return True
        current = parents.get(current)
    return False


def test_errors_module_has_no_runtime_bot_imports() -> None:
    source = (REPO_ROOT / "bot" / "errors.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    bot_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "bot" or alias.name.startswith("bot.") for alias in node.names):
                bot_imports.append(node)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level > 0 or module == "bot" or module.startswith("bot."):
                bot_imports.append(node)
    assert bot_imports, "errors.py should import the models for type checking only"
    for node in bot_imports:
        assert _inside_type_checking(node, parents), f"runtime bot import at line {node.lineno}"


def test_exchange_error_str_and_attributes() -> None:
    err = errors.TransientError("Service Unavailable", code=-1008, http_status=503, path="/fapi/v1/order", not_executed=True)
    assert str(err) == "[503 -1008] /fapi/v1/order: Service Unavailable"
    assert err.msg == "Service Unavailable" and err.code == -1008 and err.http_status == 503
    assert err.path == "/fapi/v1/order" and err.retry_after is None and err.not_executed is True
    assert errors.TransientError("x").not_executed is False
    rl = errors.IpBannedError("banned", http_status=418, retry_after=120.0)
    assert isinstance(rl, errors.RateLimitError) and rl.retry_after == 120.0
    restored = pickle.loads(pickle.dumps(err))
    assert type(restored) is errors.TransientError and str(restored) == str(err) and restored.not_executed
    # hierarchy
    assert issubclass(errors.LiveTradingNotConfirmed, errors.ConfigError)
    assert issubclass(errors.StaleDataError, errors.DataError)
    for cls in (
        errors.InsufficientMarginError,
        errors.ImmediateTriggerError,
        errors.ReduceOnlyRejectedError,
        errors.MinNotionalError,
        errors.DuplicateClientIdError,
        errors.AlgoLimitError,
        errors.ReduceOnlyModeError,
    ):
        assert issubclass(cls, errors.OrderRejectedError)
    for cls in (
        errors.TransientError,
        errors.RateLimitError,
        errors.TimestampError,
        errors.AuthError,
        errors.UnknownOrderStatusError,
        errors.NoChangeNeededError,
        errors.OrderRejectedError,
        errors.NoSuchOrderError,
    ):
        assert issubclass(cls, errors.ExchangeError) and issubclass(cls, errors.BotError)


def test_protection_and_emergency_errors_carry_outcomes() -> None:
    outcome = OpenOutcome(filled=True, qty=0.1, avg_price=50_000.0)
    closure = PositionClosure(BAR_MS, 49_990.0, 0.1, ExitReason.PROTECTION_FAILED, 2.5, 0.0, None)
    pf = errors.ProtectionFailedError("sl failed", flattened=True, closure=closure, entry=outcome)
    assert pf.flattened is True and pf.closure is closure and pf.entry is outcome and str(pf) == "sl failed"
    em = errors.EmergencyError("entry filled but unprotected and flatten failed", entry=outcome)
    assert em.entry is outcome
    assert errors.EmergencyError("x").entry is None
    restored = pickle.loads(pickle.dumps(pf))
    assert restored.flattened is True and restored.closure == closure
