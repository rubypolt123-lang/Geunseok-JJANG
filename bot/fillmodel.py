"""Fill model shared by the backtest engine and the paper broker (SPEC §9.1).

Exit accounting rule (backtest and paper):
- STOP_LOSS / TAKE_PROFIT fill at ``exit_fill_price(base, closing_side)`` with the taker fee.
- LIQUIDATION: ``exit_price = liq``, ``gross_pnl = liquidation_loss(qty, entry, leverage, funding_paid=...)``,
  ``exit_fee = 0`` (the whole isolated margin is lost; that already covers the clearance fee).

Gap rules and the conservative SL-first rule are implemented ONCE, in ``resolve_intrabar_exit``.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot.models import Direction, ExitReason, Side

if TYPE_CHECKING:
    from bot.config import ExecutionConfig

logger = logging.getLogger(__name__)

__all__ = [
    "FillModel",
    "funding_payment",
    "liquidation_loss",
    "resolve_intrabar_exit",
]


def _finite(name: str, value: float) -> float:
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number (got {value!r})")
    return v


@dataclass(frozen=True, slots=True)
class FillModel:
    """Market/stop fills with adverse slippage and taker/maker fees."""

    maker_fee: float
    taker_fee: float
    slippage_bps: float

    def __post_init__(self) -> None:
        for name in ("maker_fee", "taker_fee", "slippage_bps"):
            v = _finite(name, getattr(self, name))
            if v < 0:
                raise ValueError(f"{name} must be >= 0 (got {v!r})")
            object.__setattr__(self, name, v)

    @classmethod
    def from_config(cls, execution: ExecutionConfig) -> FillModel:
        return cls(
            maker_fee=float(execution.fees.maker),
            taker_fee=float(execution.fees.taker),
            slippage_bps=float(execution.slippage_bps),
        )

    @property
    def slip(self) -> float:
        """Slippage as a fraction (5 bps -> 0.0005)."""
        return self.slippage_bps / 10_000

    def market_fill_price(self, ref_price: float, side: Side) -> float:
        """Adverse slippage: BUY ``ref*(1+slip)``, SELL ``ref*(1-slip)``."""
        s = Side(side)
        ref = float(ref_price)
        if s is Side.BUY:
            return ref * (1 + self.slip)
        return ref * (1 - self.slip)

    def exit_fill_price(self, base_price: float, closing_side: Side) -> float:
        """Protective/end-of-data exits: same adverse rule as ``market_fill_price``."""
        return self.market_fill_price(base_price, closing_side)

    def fee(self, qty: float, price: float, *, taker: bool = True) -> float:
        """``abs(qty) * price * (taker_fee or maker_fee)`` in USDT."""
        rate = self.taker_fee if taker else self.maker_fee
        return abs(float(qty)) * float(price) * rate


def resolve_intrabar_exit(
    direction: Direction,
    o: float,
    h: float,
    l: float,  # noqa: E741 - OHLC naming used throughout the SPEC
    stop: float,
    tp: float | None,
    liq: float | None,
) -> tuple[ExitReason, float] | None:
    """Decide whether a protective level was hit inside a bar.

    Returns ``(reason, base_price)`` (base = price BEFORE slippage) or None. Gap rules on the OPEN come first
    (the open is the first price of the bar, so an open beyond a level is unambiguous), then the conservative
    SL-first rules inside the bar: if both SL and TP are touched inside the same bar, STOP_LOSS wins.
    """
    d = Direction(direction)
    o = float(o)
    h = float(h)
    lo = float(l)
    stop = float(stop)
    tp_f = None if tp is None else float(tp)
    liq_f = None if liq is None else float(liq)

    if d is Direction.LONG:
        if liq_f is not None and o <= liq_f:
            return ExitReason.LIQUIDATION, liq_f  # gap through liquidation
        if o <= stop:
            return ExitReason.STOP_LOSS, o  # gap through the stop fills at the open
        if tp_f is not None and o >= tp_f:
            return ExitReason.TAKE_PROFIT, o  # gap beyond TP fills at the open
        if lo <= stop:
            return ExitReason.STOP_LOSS, stop
        if liq_f is not None and lo <= liq_f:
            return ExitReason.LIQUIDATION, liq_f
        if tp_f is not None and h >= tp_f:
            return ExitReason.TAKE_PROFIT, tp_f
        return None

    if d is Direction.SHORT:
        if liq_f is not None and o >= liq_f:
            return ExitReason.LIQUIDATION, liq_f
        if o >= stop:
            return ExitReason.STOP_LOSS, o
        if tp_f is not None and o <= tp_f:
            return ExitReason.TAKE_PROFIT, o
        if h >= stop:
            return ExitReason.STOP_LOSS, stop
        if liq_f is not None and h >= liq_f:
            return ExitReason.LIQUIDATION, liq_f
        if tp_f is not None and lo <= tp_f:
            return ExitReason.TAKE_PROFIT, tp_f
        return None

    raise ValueError("resolve_intrabar_exit needs a LONG or SHORT direction")


def funding_payment(qty_signed: float, mark_price: float, rate: float) -> float:
    """``qty_signed * mark * rate``; positive = the position PAYS, negative = it receives."""
    return float(qty_signed) * float(mark_price) * float(rate)


def liquidation_loss(qty: float, entry_price: float, leverage: int, funding_paid: float = 0.0) -> float:
    """Gross PnL booked at liquidation: ``-(abs(qty) * entry_price / leverage - funding_paid)``.

    Under isolated margin, funding is paid from / received into the position's isolated margin, so the total
    loss at liquidation is exactly the initial margin: with ``net = gross - fees - funding`` this gives
    ``net = -IM - fees`` (``funding_paid`` is the position's accumulated funding, + = paid, - = received).
    """
    lev = float(leverage)
    if not lev > 0:
        raise ValueError(f"leverage must be > 0 (got {leverage!r})")
    initial_margin = abs(float(qty)) * float(entry_price) / lev
    return -(initial_margin - float(funding_paid))
