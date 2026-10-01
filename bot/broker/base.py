"""Broker interface shared by the paper and exchange brokers (SPEC §9.2).

A broker hides *where* orders are executed (local simulation or Binance) behind one API that the trader (U6)
drives. Contract highlights:

- ``sync`` never opens positions. With an EMPTY ``closed_candles`` it is read-only for the simulation state.
- ``open_position`` returns ``OpenOutcome(filled=False)`` ONLY when the broker has confirmed that no position
  resulted; an entry whose mandatory stop-loss cannot be placed is flattened and reported with
  ``ProtectionFailedError`` (or ``EmergencyError`` when the flatten fails too).
- Only orders whose client id starts with ``client_id_prefix(bot_id, symbol)`` are ever cancelled.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Final

from bot.models import (
    AccountSnapshot,
    ActiveTrade,
    Candle,
    ExitReason,
    Mode,
    OpenOutcome,
    OrderPurpose,
    PositionClosure,
    ProtectiveOrder,
    Side,
    SymbolFilters,
    SyncResult,
    TradePlan,
)

logger = logging.getLogger(__name__)

# algoStatus values of a stop that still protects the position (§9.2 "valid SL").
LIVE_PROTECTIVE_STATUSES: Final[frozenset[str]] = frozenset({"NEW", "TRIGGERING"})
# Tolerance used when comparing quantities (positions/orders are exchange-step multiples).
QTY_EPS: Final = 1e-12

# SyncResult issue codes (§4.1 SyncResult).
ISSUE_UNTRACKED_POSITION: Final = "UNTRACKED_POSITION"
ISSUE_QTY_MISMATCH: Final = "QTY_MISMATCH"
ISSUE_ORPHAN_PROTECTIVE_CANCELED: Final = "ORPHAN_PROTECTIVE_CANCELED"
ISSUE_FOREIGN_OPEN_ORDERS: Final = "FOREIGN_OPEN_ORDERS"
ISSUE_SL_MISSING: Final = "SL_MISSING"
ISSUE_PROTECTION_QTY_MISMATCH: Final = "PROTECTION_QTY_MISMATCH"
ISSUE_CLOSURE_DETAILS_UNKNOWN: Final = "CLOSURE_DETAILS_UNKNOWN"


class Broker(ABC):
    """Abstract broker (paper simulation or Binance testnet/live)."""

    mode: Mode

    @abstractmethod
    def prepare_symbol(self, symbol: str, leverage: int) -> SymbolFilters:
        """Validate the symbol, set up account/margin/leverage (exchange) and return its filters."""

    @abstractmethod
    def sync(self, symbol: str, active: ActiveTrade | None, closed_candles: Sequence[Candle]) -> SyncResult:
        """Bring broker state up to date and report it.

        Paper: simulate protective exits / funding / liquidation over candles not yet processed.
        Exchange: read the account, detect a closure of ``active``, clean orphan own orders.
        Never opens positions. ``closed_candles`` may be EMPTY: then no simulation happens and no cursor/state
        changes; only the account and issues are built (read-only).
        """

    @abstractmethod
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
        """Market entry, then the protective SL (+TP if ``plan.take_profit_price``).

        If the SL cannot be placed: flatten and raise ``ProtectionFailedError(flattened=True, closure=..., entry=...)``;
        if the flatten also fails: raise ``EmergencyError(entry=...)``. ``filled=False`` is returned ONLY when the
        broker has confirmed that no position resulted.
        """

    @abstractmethod
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
        """Market reduce-only close of the whole position, then cancel THIS bot's own orders on the symbol.

        Foreign orders are never cancelled. Returns None if already flat (after still cancelling own orphans).
        """

    @abstractmethod
    def ensure_protection(
        self,
        active: ActiveTrade,
        account: AccountSnapshot,
        *,
        sl_client_id: str,
        tp_client_id: str | None,
    ) -> tuple[ProtectiveOrder, ...]:
        """Guarantee a VALID own SL for the current position (and TP if planned and missing/invalid).

        Place-before-cancel: the new generation is placed first, stale own protective orders are cancelled after.
        SL failure -> flatten + ``ProtectionFailedError`` as in ``open_position``.
        """

    def max_notional(self, symbol: str) -> float | None:
        """Cached leverage-bracket ``maxNotionalValue`` (exchange brokers); None when unknown / not applicable."""
        return None


# ---------------------------------------------------------------------------------------------
# Small helpers shared by the broker implementations
# ---------------------------------------------------------------------------------------------


def is_own(client_id: str | None, prefix: str) -> bool:
    """True iff the order id carries this bot's ownership prefix (``client_id_prefix(bot_id, symbol)``)."""
    return isinstance(client_id, str) and client_id.startswith(prefix)


def exit_purpose(reason: ExitReason) -> OrderPurpose:
    """Order purpose of a close: strategy exits are EXIT, everything else (kill switch, protection) FLATTEN."""
    return OrderPurpose.EXIT if reason in (ExitReason.SIGNAL, ExitReason.FLIP) else OrderPurpose.FLATTEN


def covers_position(order: ProtectiveOrder, position_qty: float) -> bool:
    """A stop covers the position if it closes all of it (closePosition) or its quantity is large enough."""
    if order.close_position:
        return True
    if order.quantity is None:
        return False
    return float(order.quantity) >= abs(float(position_qty)) - QTY_EPS


def tp_matches_position(order: ProtectiveOrder, position_qty: float) -> bool:
    """A take-profit is valid if it closes the position (closePosition) or its quantity equals the position size."""
    if order.close_position:
        return True
    if order.quantity is None:
        return False
    return abs(float(order.quantity) - abs(float(position_qty))) <= QTY_EPS


def live_own_orders(
    orders: Iterable[ProtectiveOrder], prefix: str, kind: OrderPurpose, closing_side: Side | None = None
) -> list[ProtectiveOrder]:
    """Own protective orders of ``kind`` that are still live (NEW/TRIGGERING), optionally on ``closing_side``."""
    return [
        o
        for o in orders
        if o.kind == kind
        and is_own(o.client_id, prefix)
        and str(o.status) in LIVE_PROTECTIVE_STATUSES
        and (closing_side is None or o.side == closing_side)
    ]
