"""Shared domain models: constants, enums, dataclasses and helpers (SPEC §4.1).

numpy boundary (§0.2): every value that enters a model dataclass, ``Storage``, JSON or a client id must be a
native ``int``/``float``/``bool``. ``Candle``/``Signal`` (and ``ActiveTrade``/``Trade``) coerce in
``__post_init__``; ``to_jsonable`` converts ``numpy.generic`` via ``.item()``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from pathlib import PurePath
from typing import Any, Final

import numpy as np
import pandas as pd

from bot.errors import DataError

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

KLINE_COLUMNS: Final = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
)
KLINE_DTYPES: Final = {
    "open_time": "int64",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
    "close_time": "int64",
    "quote_volume": "float64",
    "trades": "int64",
    "taker_buy_base": "float64",
    "taker_buy_quote": "float64",
}
TRADE_COLUMNS: Final = (
    "trade_id",
    "source",
    "run_id",
    "symbol",
    "direction",
    "qty",
    "entry_time",
    "entry_price",
    "exit_time",
    "exit_price",
    "exit_reason",
    "gross_pnl",
    "fees",
    "funding",
    "net_pnl",
    "r_multiple",
    "initial_stop",
    "take_profit",
    "leverage",
)
CLIENT_ID_RE: Final = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")
CLIENT_ID_MAX_LEN: Final = 36
CLIENT_ID_KINDS: Final[frozenset[str]] = frozenset({"EN", "EX", "SL", "TP", "FL", "KS"})
BOT_ID_RE: Final = re.compile(r"^[A-Za-z0-9]{1,8}$")

# Korean metric labels: defined HERE (single source), re-exported by bot/backtest/report.py and served by the
# dashboard in /api/meta (so app.js never hand-copies them).
METRIC_LABELS_KO: Final[dict[str, str]] = {
    "total_return": "총 수익률",
    "cagr": "연환산 수익률(CAGR)",
    "max_drawdown": "최대 낙폭(MDD)",
    "max_drawdown_duration_bars": "최대 낙폭 지속(봉)",
    "sharpe": "샤프 지수",
    "sharpe_daily": "샤프 지수(일간)",
    "sortino": "소르티노 지수",
    "n_trades": "거래 수",
    "win_rate": "승률",
    "profit_factor": "손익비(Profit Factor)",
    "expectancy": "기대값(USDT/거래)",
    "expectancy_r": "기대값(R)",
    "avg_win": "평균 수익",
    "avg_loss": "평균 손실",
    "best_trade": "최고 거래",
    "worst_trade": "최악 거래",
    "avg_holding_hours": "평균 보유 시간(h)",
    "exposure": "시장 노출 비율",
    "total_fees": "총 수수료",
    "total_funding": "총 펀딩비",
    "n_liquidations": "강제청산 횟수",
    "n_stop_losses": "손절 횟수",
    "n_take_profits": "익절 횟수",
    "max_consecutive_losses": "최대 연속 손실",
    "final_equity": "최종 자산",
    "initial_balance": "초기 자산",
    "bars": "봉 개수",
    "rejected_entries": "리스크 거부 진입",
    "entries_capped_by_notional": "명목가 상한 적용 진입",
    "funding_events": "펀딩 적용 횟수",
}
PERCENT_METRICS: Final[frozenset[str]] = frozenset({"total_return", "cagr", "max_drawdown", "win_rate", "exposure"})

_QTY_EPS: Final = 1e-12


# ---------------------------------------------------------------------------------------------
# Enums (explicit upper-case string values; enum.auto() is forbidden)
# ---------------------------------------------------------------------------------------------


class Mode(StrEnum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"

    @property
    def sign(self) -> int:
        """LONG +1, SHORT -1, FLAT 0."""
        if self is Direction.LONG:
            return 1
        if self is Direction.SHORT:
            return -1
        return 0

    @property
    def opening_side(self) -> Side:
        """Order side that opens this direction (LONG BUY, SHORT SELL)."""
        if self is Direction.LONG:
            return Side.BUY
        if self is Direction.SHORT:
            return Side.SELL
        raise ValueError("FLAT has no opening side")

    @property
    def closing_side(self) -> Side:
        """Order side that closes this direction (LONG SELL, SHORT BUY)."""
        if self is Direction.LONG:
            return Side.SELL
        if self is Direction.SHORT:
            return Side.BUY
        raise ValueError("FLAT has no closing side")

    @staticmethod
    def from_qty(qty: float) -> Direction:
        """>0 LONG, <0 SHORT, 0 FLAT (abs(qty) < 1e-12 counts as 0)."""
        q = float(qty)
        if abs(q) < _QTY_EPS:
            return Direction.FLAT
        return Direction.LONG if q > 0 else Direction.SHORT


class SignalAction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE = "CLOSE"
    NONE = "NONE"


class Action(StrEnum):
    NONE = "NONE"
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE = "CLOSE"
    FLIP_LONG = "FLIP_LONG"
    FLIP_SHORT = "FLIP_SHORT"


class OrderType(StrEnum):
    MARKET = "MARKET"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class OrderPurpose(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    FLATTEN = "FLATTEN"


class OrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"
    UNKNOWN = "UNKNOWN"


class ExitReason(StrEnum):
    SIGNAL = "SIGNAL"
    FLIP = "FLIP"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    LIQUIDATION = "LIQUIDATION"
    KILL_SWITCH = "KILL_SWITCH"
    END_OF_DATA = "END_OF_DATA"
    PROTECTION_FAILED = "PROTECTION_FAILED"
    MANUAL = "MANUAL"
    UNKNOWN = "UNKNOWN"


class BotState(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    HALTED = "HALTED"
    KILL_SWITCH = "KILL_SWITCH"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


# ---------------------------------------------------------------------------------------------
# Small coercion helpers (numpy boundary)
# ---------------------------------------------------------------------------------------------


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def _opt_str(v: Any) -> str | None:
    return None if v is None else str(v)


def _plain_str(v: Any) -> str:
    """Enum -> its value, anything else -> ``str(v)`` (always an exact ``str``)."""
    if isinstance(v, Enum):
        return str(v.value)
    return str(v)


def _float_or_nan(v: Any) -> float:
    return math.nan if v is None else float(v)


# ---------------------------------------------------------------------------------------------
# Candles and signals
# ---------------------------------------------------------------------------------------------

_CANDLE_FIELDS: Final = ("open_time", "open", "high", "low", "close", "volume", "close_time")


@dataclass(frozen=True, slots=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "open_time", int(self.open_time))
        object.__setattr__(self, "close_time", int(self.close_time))
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, float(getattr(self, name)))

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Candle:
        """Build from a DataFrame row (``df.iloc[i]``) or a dict; every field goes through int()/float()."""
        return cls(
            open_time=int(row["open_time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            close_time=int(row["close_time"]),
        )


def candles_from_df(df: pd.DataFrame) -> list[Candle]:
    """Convert a candle DataFrame to ``Candle`` objects (itertuples keeps the int64 times exact)."""
    if df is None or len(df) == 0:
        return []
    missing = [c for c in _CANDLE_FIELDS if c not in df.columns]
    if missing:
        raise DataError(f"candle frame is missing columns: {', '.join(missing)}")
    return [
        Candle(
            open_time=int(ot),
            open=float(o),
            high=float(h),
            low=float(lo),
            close=float(c),
            volume=float(v),
            close_time=int(ct),
        )
        for ot, o, h, lo, c, v, ct in df.loc[:, list(_CANDLE_FIELDS)].itertuples(index=False, name=None)
    ]


@dataclass(frozen=True, slots=True)
class Signal:
    action: SignalAction
    bar_open_time: int  # open_time of the CLOSED bar that produced the signal
    price: float  # close of that bar
    reason: str = ""  # e.g. "golden_cross", "dead_cross", "warmup"
    meta: Mapping[str, float] = field(default_factory=dict)  # indicator values

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", SignalAction(self.action))
        object.__setattr__(self, "bar_open_time", int(self.bar_open_time))
        object.__setattr__(self, "price", float(self.price))
        object.__setattr__(self, "reason", "" if self.reason is None else str(self.reason))
        meta = self.meta if self.meta is not None else {}
        object.__setattr__(self, "meta", {str(k): _float_or_nan(v) for k, v in dict(meta).items()})

    def to_dict(self) -> dict[str, Any]:
        """JSON-able dict (NaN meta values become None)."""
        return {
            "action": self.action.value,
            "bar_open_time": self.bar_open_time,
            "price": to_jsonable(self.price),
            "reason": self.reason,
            "meta": to_jsonable(self.meta),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Signal:
        return cls(
            action=SignalAction(d["action"]),
            bar_open_time=d["bar_open_time"],
            price=_float_or_nan(d["price"]),
            reason=d.get("reason") or "",
            meta=d.get("meta") or {},
        )


# ---------------------------------------------------------------------------------------------
# Exchange symbol filters
# ---------------------------------------------------------------------------------------------

_SF_STR_FIELDS: Final = ("symbol", "status", "contract_type")
_SF_DECIMAL_FIELDS: Final = (
    "tick_size",
    "min_price",
    "max_price",
    "step_size",
    "min_qty",
    "max_qty",
    "market_step_size",
    "market_min_qty",
    "market_max_qty",
    "min_notional",
    "multiplier_up",
    "multiplier_down",
    "trigger_protect",
    "market_take_bound",
)


def _to_decimal_field(name: str, v: Any) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool) or v is None:
        raise DataError(f"invalid symbol filter {name}={v!r}")
    try:
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        raise DataError(f"invalid symbol filter {name}={v!r}") from None


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    symbol: str
    status: str
    contract_type: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal  # LOT_SIZE
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal  # MARKET_LOT_SIZE
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal  # MIN_NOTIONAL.notional
    multiplier_up: Decimal  # PERCENT_PRICE
    multiplier_down: Decimal
    trigger_protect: Decimal
    market_take_bound: Decimal

    def __post_init__(self) -> None:
        for name in _SF_STR_FIELDS:
            object.__setattr__(self, name, str(getattr(self, name)))
        for name in _SF_DECIMAL_FIELDS:
            object.__setattr__(self, name, _to_decimal_field(name, getattr(self, name)))

    def to_dict(self) -> dict[str, str]:
        """All values as str (Decimals keep their exact textual form, e.g. "0.10")."""
        out: dict[str, str] = {name: str(getattr(self, name)) for name in _SF_STR_FIELDS}
        out.update({name: str(getattr(self, name)) for name in _SF_DECIMAL_FIELDS})
        return out

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> SymbolFilters:
        """Inverse of ``to_dict``; unknown keys (e.g. cache metadata ``fetched_at``/``host``) are ignored."""
        missing = [k for k in (*_SF_STR_FIELDS, *_SF_DECIMAL_FIELDS) if k not in d]
        if missing:
            raise DataError(f"symbol filters are missing keys: {', '.join(missing)}")
        kwargs: dict[str, Any] = {k: d[k] for k in _SF_STR_FIELDS}
        kwargs.update({k: _to_decimal_field(k, d[k]) for k in _SF_DECIMAL_FIELDS})
        return cls(**kwargs)


# ---------------------------------------------------------------------------------------------
# Risk / orders / account
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TradePlan:
    symbol: str
    direction: Direction
    ref_price: float  # reference entry price used for sizing (next-bar open / forming candle open)
    qty: Decimal  # already floored to market step
    stop_price: Decimal  # tick-rounded toward entry
    take_profit_price: Decimal | None  # tick-rounded toward entry
    notional: float  # float(qty) * ref_price
    risk_amount: float  # float(qty) * per_unit_loss
    leverage: int
    liquidation_price: float  # conservative approximation
    sizing_cap: str = "risk"  # "risk" | "margin" | "notional"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    plan: TradePlan | None
    reason: str  # "ok" or a rejection code (§8.6)

    @property
    def ok(self) -> bool:
        return self.plan is not None


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: Side
    order_type: OrderType
    purpose: OrderPurpose
    client_id: str
    quantity: Decimal | None = None
    reduce_only: bool = False
    close_position: bool = False
    trigger_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    client_id: str
    exchange_id: str | None
    symbol: str
    side: Side
    order_type: OrderType
    purpose: OrderPurpose
    status: OrderStatus
    requested_qty: float | None
    executed_qty: float
    avg_price: float | None
    trigger_price: float | None
    fee: float
    ts: int
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class ProtectiveOrder:
    kind: OrderPurpose  # STOP_LOSS or TAKE_PROFIT
    client_id: str
    exchange_id: str | None
    side: Side
    trigger_price: float
    status: str  # algoStatus string ("NEW", ...); paper uses "NEW"
    close_position: bool
    quantity: float | None


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    qty: float  # signed: >0 long, <0 short (never 0: flat is represented by None)
    entry_price: float
    mark_price: float | None
    unrealized_pnl: float
    liquidation_price: float | None
    isolated_margin: float | None
    leverage: int | None
    updated_at: int

    @property
    def direction(self) -> Direction:
        return Direction.from_qty(self.qty)


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    ts: int
    wallet_balance: float  # USDT wallet balance (paper: cash)
    equity: float  # wallet + unrealized (exchange: USDT marginBalance)
    available_balance: float
    unrealized_pnl: float
    position: Position | None
    protective_orders: tuple[ProtectiveOrder, ...] = ()
    open_orders_count: int = 0  # regular (non-algo) open orders on the symbol


@dataclass(slots=True)
class ActiveTrade:
    """The bot's view of its open position (mutable)."""

    trade_id: str
    symbol: str
    direction: Direction
    qty: float  # absolute
    entry_price: float  # actual average fill
    entry_time: int  # ms of the fill (paper/backtest: forming/next bar open_time)
    entry_bar_open_time: int  # open_time of the bar in which the entry filled
    stop_price: float
    take_profit_price: float | None
    liquidation_price: float | None
    leverage: int
    risk_amount: float
    entry_fee: float
    entry_client_id: str
    protect_seq: int = 0  # incremented each time protective orders are (re)placed
    entry_order_id: str | None = None  # exchange orderId of the entry; None for adopted/paper/backtest

    def __post_init__(self) -> None:
        self.trade_id = str(self.trade_id)
        self.symbol = str(self.symbol)
        self.direction = Direction(self.direction)
        self.qty = float(self.qty)
        self.entry_price = float(self.entry_price)
        self.entry_time = int(self.entry_time)
        self.entry_bar_open_time = int(self.entry_bar_open_time)
        self.stop_price = float(self.stop_price)
        self.take_profit_price = _opt_float(self.take_profit_price)
        self.liquidation_price = _opt_float(self.liquidation_price)
        self.leverage = int(self.leverage)
        self.risk_amount = float(self.risk_amount)
        self.entry_fee = float(self.entry_fee)
        self.entry_client_id = str(self.entry_client_id)
        self.protect_seq = int(self.protect_seq)
        self.entry_order_id = _opt_str(self.entry_order_id)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ActiveTrade:
        """Inverse of ``to_dict``; tolerates a missing ``entry_order_id`` (and ``protect_seq``)."""
        return cls(
            trade_id=d["trade_id"],
            symbol=d["symbol"],
            direction=Direction(d["direction"]),
            qty=d["qty"],
            entry_price=d["entry_price"],
            entry_time=d["entry_time"],
            entry_bar_open_time=d["entry_bar_open_time"],
            stop_price=d["stop_price"],
            take_profit_price=d.get("take_profit_price"),
            liquidation_price=d.get("liquidation_price"),
            leverage=d["leverage"],
            risk_amount=d["risk_amount"],
            entry_fee=d["entry_fee"],
            entry_client_id=d["entry_client_id"],
            protect_seq=d.get("protect_seq") or 0,
            entry_order_id=d.get("entry_order_id"),
        )


@dataclass(frozen=True, slots=True)
class OpenOutcome:
    filled: bool
    qty: float = 0.0
    avg_price: float = 0.0
    entry_fee: float = 0.0
    entry_time: int = 0
    entry_order: OrderResult | None = None
    protective: tuple[ProtectiveOrder, ...] = ()
    message: str = ""
    # Callers MUST check `filled` before reading any other field (defaults are placeholders when filled is False).


@dataclass(frozen=True, slots=True)
class PositionClosure:
    exit_time: int
    exit_price: float
    qty: float
    reason: ExitReason
    exit_fee: float  # USDT, >= 0
    funding: float  # total funding over the trade's life; + = paid, - = received
    gross_pnl: float | None  # exchange realized PnL if known; None -> computed from prices
    order: OrderResult | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    account: AccountSnapshot
    closure: PositionClosure | None
    issues: tuple[str, ...] = ()
    # codes: UNTRACKED_POSITION, QTY_MISMATCH, ORPHAN_PROTECTIVE_CANCELED, FOREIGN_OPEN_ORDERS, SL_MISSING,
    #        PROTECTION_QTY_MISMATCH, CLOSURE_DETAILS_UNKNOWN


@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: str
    source: str  # "backtest" | "paper" | "testnet" | "live"
    run_id: str | None
    symbol: str
    direction: Direction
    qty: float
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    exit_reason: ExitReason
    gross_pnl: float
    fees: float
    funding: float
    net_pnl: float
    r_multiple: float | None
    initial_stop: float | None
    take_profit: float | None
    leverage: int

    def __post_init__(self) -> None:
        setv = object.__setattr__
        setv(self, "trade_id", str(self.trade_id))
        setv(self, "source", _plain_str(self.source))
        setv(self, "run_id", _opt_str(self.run_id))
        setv(self, "symbol", str(self.symbol))
        setv(self, "direction", Direction(self.direction))
        setv(self, "qty", float(self.qty))
        setv(self, "entry_time", int(self.entry_time))
        setv(self, "entry_price", float(self.entry_price))
        setv(self, "exit_time", int(self.exit_time))
        setv(self, "exit_price", float(self.exit_price))
        setv(self, "exit_reason", ExitReason(self.exit_reason))
        for name in ("gross_pnl", "fees", "funding", "net_pnl"):
            setv(self, name, float(getattr(self, name)))
        for name in ("r_multiple", "initial_stop", "take_profit"):
            setv(self, name, _opt_float(getattr(self, name)))
        setv(self, "leverage", int(self.leverage))

    @classmethod
    def from_closure(
        cls,
        active: ActiveTrade,
        closure: PositionClosure,
        *,
        source: str,
        run_id: str | None = None,
    ) -> Trade:
        """Book a closed trade (SPEC §4.1 exact math; + funding = paid)."""
        sign = active.direction.sign
        if closure.gross_pnl is not None:
            gross = float(closure.gross_pnl)
        else:
            gross = sign * active.qty * (float(closure.exit_price) - active.entry_price)
        fees = active.entry_fee + float(closure.exit_fee)
        funding = float(closure.funding)
        net = gross - fees - funding
        r_multiple = net / active.risk_amount if active.risk_amount > 0 else None
        return cls(
            trade_id=active.trade_id,
            source=source,
            run_id=run_id,
            symbol=active.symbol,
            direction=active.direction,
            qty=active.qty,
            entry_time=active.entry_time,
            entry_price=active.entry_price,
            exit_time=closure.exit_time,
            exit_price=closure.exit_price,
            exit_reason=closure.reason,
            gross_pnl=gross,
            fees=fees,
            funding=funding,
            net_pnl=net,
            r_multiple=r_multiple,
            initial_stop=active.stop_price,
            take_profit=active.take_profit_price,
            leverage=active.leverage,
        )

    def to_dict(self) -> dict[str, Any]:
        """Keys == TRADE_COLUMNS (in that order), enums as str."""
        out: dict[str, Any] = {}
        for name in TRADE_COLUMNS:
            value = getattr(self, name)
            out[name] = value.value if isinstance(value, Enum) else value
        return out

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Trade:
        optional = {"run_id", "r_multiple", "initial_stop", "take_profit"}
        kwargs = {name: (d.get(name) if name in optional else d[name]) for name in TRADE_COLUMNS}
        return cls(**kwargs)


@dataclass(slots=True)
class BacktestResult:
    run_id: str
    created_at: int
    symbol: str
    interval: str
    strategy: str
    params: dict[str, Any]
    config: dict[str, Any]
    start_time: int  # first equity bar open_time
    end_time: int  # last bar close_time
    initial_balance: float
    metrics: dict[str, float | int | None]
    equity: pd.DataFrame  # columns: time(int64 ms = bar open_time), equity, in_position(bool), position_qty
    trades: list[Trade]
    result_dir: str | None = None


@dataclass(slots=True)
class BotStatus:
    updated_at: int
    started_at: int
    mode: Mode
    symbol: str
    interval: str
    strategy: str
    state: BotState
    message: str
    account: AccountSnapshot | None
    last_signal: Signal | None
    last_bar_open_time: int | None
    entries_blocked_reason: str | None
    pid: int


# ---------------------------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------------------------


def _json_key(k: Any) -> Any:
    if isinstance(k, Enum):
        return _json_key(k.value)
    if isinstance(k, np.generic):
        return _json_key(k.item())
    if k is None or isinstance(k, (str, int, float, bool)):
        return k
    return str(k)


def to_jsonable(obj: Any) -> Any:
    """Convert ``obj`` into plain JSON-compatible Python values.

    dataclass -> dict (recursive via ``dataclasses.fields``; never ``asdict``), StrEnum -> value,
    Decimal -> str, numpy.generic -> ``.item()`` (then the float rule), tuple/list -> list,
    DataFrame -> list of records, float NaN/inf -> None, Mapping (incl. MappingProxyType) -> dict,
    Path -> str. Fields marked ``metadata={"secret": True}`` are masked as "***". Everything else unchanged.
    """
    if obj is None or obj is pd.NA or obj is pd.NaT:
        return None
    if isinstance(obj, Enum):
        return to_jsonable(obj.value)
    if isinstance(obj, np.generic):
        return to_jsonable(obj.item())
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return float(obj) if math.isfinite(obj) else None
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, str):
        return str(obj)
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, PurePath):
        return str(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            if f.metadata.get("secret"):
                out[f.name] = "***"
            else:
                out[f.name] = to_jsonable(getattr(obj, f.name))
        return out
    if isinstance(obj, pd.DataFrame):
        return [
            {_json_key(k): to_jsonable(v) for k, v in record.items()}
            for record in obj.to_dict(orient="records")
        ]
    if isinstance(obj, pd.Series):
        return [to_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, np.ndarray):
        return [to_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, Mapping):
        return {_json_key(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        items = [to_jsonable(v) for v in obj]
        try:
            return sorted(items)
        except TypeError:
            return items
    return obj


def symbol_tag(symbol: str) -> str:
    """4-hex tag of the symbol ("BTCUSDT" -> "4314"), used inside client ids."""
    return hashlib.sha1(str(symbol).encode()).hexdigest()[:4]


def client_id_prefix(bot_id: str, symbol: str) -> str:
    """Orders "owned" by this bot on this symbol start with this prefix."""
    return f"{bot_id}-{symbol_tag(symbol)}-"


def _check_client_id(cid: str) -> str:
    if len(cid) > CLIENT_ID_MAX_LEN or not CLIENT_ID_RE.fullmatch(cid):
        raise ValueError(f"invalid client id {cid!r} (max {CLIENT_ID_MAX_LEN} chars of [.A-Z:/a-z0-9_-])")
    return cid


def make_client_id(bot_id: str, symbol: str, kind: str, bar_open_time_ms: int, seq: int = 0) -> str:
    """``"<bot_id>-<symbol tag>-<kind>-<bar open time in s>-<seq>"``.

    Example: ``make_client_id("mab1", "BTCUSDT", "EN", 1_790_769_600_000) == "mab1-4314-EN-1790769600-0"``.
    """
    if not isinstance(bot_id, str) or not BOT_ID_RE.fullmatch(bot_id):
        raise ValueError(f"invalid bot_id {bot_id!r}: 1-8 ASCII letters/digits")
    if kind not in CLIENT_ID_KINDS:
        raise ValueError(f"invalid client id kind {kind!r}; expected one of {sorted(CLIENT_ID_KINDS)}")
    if isinstance(seq, bool) or isinstance(bar_open_time_ms, bool):
        raise ValueError("bar_open_time_ms and seq must be integers, not bool")
    bar_ms = int(bar_open_time_ms)
    seq_i = int(seq)
    if bar_ms < 0:
        raise ValueError(f"bar_open_time_ms must be >= 0, got {bar_ms}")
    if seq_i < 0:
        raise ValueError(f"seq must be >= 0, got {seq_i}")
    return _check_client_id(f"{bot_id}-{symbol_tag(symbol)}-{kind}-{bar_ms // 1000}-{seq_i}")


_TRAILING_SEQ_RE: Final = re.compile(r"^(?P<head>.*-)(?P<seq>\d+)$")


def next_client_id(client_id: str) -> str:
    """Increment the trailing ``-<seq>``: "...-1790769600-0" -> "...-1790769600-1"."""
    m = _TRAILING_SEQ_RE.fullmatch(client_id) if isinstance(client_id, str) else None
    if m is None:
        raise ValueError(f"client id {client_id!r} has no trailing -<seq>")
    return _check_client_id(f"{m.group('head')}{int(m.group('seq')) + 1}")


def validate_candles_df(df: pd.DataFrame, interval_ms: int | None = None) -> None:
    """Raise ``DataError`` unless ``df`` follows the candle DataFrame convention (§4.1)."""
    if not isinstance(df, pd.DataFrame):
        raise DataError(f"candles must be a pandas DataFrame, got {type(df).__name__}")
    missing = [c for c in KLINE_COLUMNS if c not in df.columns]
    if missing:
        raise DataError(f"candle frame is missing columns: {', '.join(missing)}")
    wrong = [
        f"{c}={df[c].dtype}(expected {dtype})" for c, dtype in KLINE_DTYPES.items() if str(df[c].dtype) != dtype
    ]
    if wrong:
        raise DataError(f"candle frame has wrong dtypes: {', '.join(wrong)}")
    open_time = df["open_time"]
    if not open_time.is_unique:
        raise DataError("candle frame has duplicate open_time values")
    if not open_time.is_monotonic_increasing:
        raise DataError("candle frame is not sorted ascending by open_time")
    if interval_ms is not None and len(df):
        bad = df["close_time"] != open_time + (int(interval_ms) - 1)
        if bool(bad.any()):
            first = int(open_time[bad].iloc[0])
            raise DataError(
                f"candle close_time != open_time + interval - 1 (interval {int(interval_ms)} ms) at open_time {first}"
            )
