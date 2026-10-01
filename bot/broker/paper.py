"""Paper broker: local simulation on real public market data (SPEC §9.3).

Fills, fees, slippage, protective exits, liquidation and funding follow exactly the rules of the backtest
(``bot.fillmodel``), so a paper run and a backtest over the same bars book the same trades. The REST client
behind ``market`` has NO credentials, so nothing here can ever sign a request or place an order.

State (``paper_state:{symbol}`` in ``Storage``, persisted after every mutation)::

    {"cash": float,
     "position": null | {"direction", "qty", "entry_price", "entry_time", "entry_bar_open_time", "stop", "tp",
                         "liq", "leverage", "sl_client_id", "tp_client_id", "funding"},
     "last_bar_open_time": int | null, "last_close": float | null, "funding_cursor": int | null}

``funding_cursor`` is the ``funding_time`` of the last funding event actually APPLIED (never the end of a
queried range), so a record that Binance publishes a few seconds late is picked up by the next fetch.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Final

from bot.broker.base import (
    ISSUE_CLOSURE_DETAILS_UNKNOWN,
    ISSUE_UNTRACKED_POSITION,
    Broker,
    exit_purpose,
)
from bot.errors import BotError, ConfigError
from bot.fillmodel import FillModel, funding_payment, liquidation_loss, resolve_intrabar_exit
from bot.models import (
    AccountSnapshot,
    ActiveTrade,
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
    SymbolFilters,
    SyncResult,
    TradePlan,
)
from bot.timeutil import ms_to_iso, now_ms

if TYPE_CHECKING:
    from bot.exchange.market import MarketData
    from bot.storage import Storage

logger = logging.getLogger(__name__)

DEFAULT_FUNDING_INTERVAL_MS: Final = 28_800_000  # 8 h
LATE_FUNDING_REFETCHES: Final = 3
LATE_FUNDING_SLEEP_SEC: Final = 1.0
_KNOWN_FUNDING_TIMES_KEEP: Final = 16

# (funding_time ms, rate, mark price or None)
FundingEvent = tuple[int, float, float | None]


def paper_state_key(symbol: str) -> str:
    """Storage key of the paper simulation state of ``symbol`` (§5.2)."""
    return f"paper_state:{symbol}"


class PaperBroker(Broker):
    """Local simulation broker (never signs, never sends orders)."""

    mode = Mode.PAPER

    def __init__(
        self,
        *,
        market: MarketData,
        fill_model: FillModel,
        storage: Storage,
        initial_balance: float,
        include_funding: bool = True,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        client = getattr(market, "client", None)
        if client is not None and bool(getattr(client, "has_credentials", False)):
            raise ConfigError("paper mode must use a REST client WITHOUT API credentials (paper never signs)")
        balance = float(initial_balance)
        if not math.isfinite(balance) or balance <= 0:
            raise ConfigError(f"paper.initial_balance must be > 0, got {initial_balance!r}")
        self.market = market
        self.fill_model = fill_model
        self.storage = storage
        self.initial_balance = balance
        self.include_funding = bool(include_funding)
        self.clock = clock
        self.sleep = sleep
        self._leverage: dict[str, int] = {}
        # Recently seen real funding timestamps per symbol (used to infer the funding schedule, late-record rule).
        self._known_funding_times: dict[str, list[int]] = {}

    # ------------------------------------------------------------------------------------------ state

    def _default_state(self) -> dict[str, Any]:
        return {
            "cash": self.initial_balance,
            "position": None,
            "last_bar_open_time": None,
            "last_close": None,
            "funding_cursor": None,
        }

    def _load(self, symbol: str) -> dict[str, Any]:
        raw = self.storage.get_state(paper_state_key(symbol))
        state = self._default_state()
        if not isinstance(raw, dict):
            return state
        if raw.get("cash") is not None:
            state["cash"] = float(raw["cash"])
        state["last_bar_open_time"] = _opt_int(raw.get("last_bar_open_time"))
        state["last_close"] = _opt_float(raw.get("last_close"))
        state["funding_cursor"] = _opt_int(raw.get("funding_cursor"))
        pos = raw.get("position")
        state["position"] = _normalize_position(pos) if isinstance(pos, dict) else None
        return state

    def _save(self, symbol: str, state: dict[str, Any]) -> None:
        self.storage.set_state(paper_state_key(symbol), state)

    # ------------------------------------------------------------------------------------------ Broker API

    def prepare_symbol(self, symbol: str, leverage: int) -> SymbolFilters:
        """Mainnet filters from public data; the leverage is only recorded (nothing to configure locally)."""
        filters = self.market.symbol_filters(symbol)
        self._leverage[symbol] = int(leverage)
        return filters

    def sync(self, symbol: str, active: ActiveTrade | None, closed_candles: Sequence[Candle]) -> SyncResult:
        state = self._load(symbol)
        candles = sorted(closed_candles, key=lambda c: int(c.open_time))
        closure: PositionClosure | None = None
        if candles:
            # Steps 1-3 (simulation) only run with candles; an empty list keeps every cursor unchanged.
            closure = self._simulate(symbol, state, candles)
            self._save(symbol, state)

        account = self._account(symbol, state)
        issues: list[str] = []
        pos = state["position"]
        if active is None and pos is not None:
            issues.append(ISSUE_UNTRACKED_POSITION)
        if active is not None and pos is None and closure is None:
            # The trader believes a position exists that the simulation does not have (e.g. reset paper state):
            # report a closure at the last known price so the trader's state stays consistent.
            issues.append(ISSUE_CLOSURE_DETAILS_UNKNOWN)
            last_close = state["last_close"]
            closure = PositionClosure(
                exit_time=now_ms(self.clock),
                exit_price=float(last_close) if last_close is not None else float(active.entry_price),
                qty=float(active.qty),
                reason=ExitReason.UNKNOWN,
                exit_fee=0.0,
                funding=0.0,
                gross_pnl=None,
            )
        return SyncResult(account=account, closure=closure, issues=tuple(issues))

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
        symbol = plan.symbol
        direction = Direction(plan.direction)
        if direction is Direction.FLAT:
            raise ValueError("cannot open a FLAT position")
        qty = float(plan.qty)
        if not math.isfinite(qty) or qty <= 0:
            raise ValueError(f"plan quantity must be > 0, got {plan.qty!r}")
        ref = float(ref_price)
        if not math.isfinite(ref) or ref <= 0:
            raise ValueError(f"ref_price must be > 0, got {ref_price!r}")
        state = self._load(symbol)
        if state["position"] is not None:
            raise BotError(f"paper position already open on {symbol}; the trader only opens when flat")

        bar_ms = int(bar_time)
        side = direction.opening_side
        fill = float(self.fill_model.market_fill_price(ref, side))
        fee = float(self.fill_model.fee(qty, fill))
        state["cash"] = float(state["cash"]) - fee
        position = {
            "direction": direction.value,
            "qty": qty,
            "entry_price": fill,
            "entry_time": bar_ms,
            "entry_bar_open_time": bar_ms,
            "stop": float(plan.stop_price),
            "tp": None if plan.take_profit_price is None else float(plan.take_profit_price),
            "liq": _opt_float(plan.liquidation_price),
            "leverage": int(plan.leverage),
            "sl_client_id": str(sl_client_id),
            "tp_client_id": None if tp_client_id is None else str(tp_client_id),
            "funding": 0.0,
        }
        state["position"] = position
        state["funding_cursor"] = bar_ms
        self._save(symbol, state)

        entry_order = OrderResult(
            client_id=str(entry_client_id),
            exchange_id=None,
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            purpose=OrderPurpose.ENTRY,
            status=OrderStatus.FILLED,
            requested_qty=qty,
            executed_qty=qty,
            avg_price=fill,
            trigger_price=None,
            fee=fee,
            ts=bar_ms,
        )
        logger.info(
            "paper entry %s %s qty=%s fill=%.8g fee=%.8g at %s",
            symbol,
            direction.value,
            qty,
            fill,
            fee,
            ms_to_iso(bar_ms),
        )
        return OpenOutcome(
            filled=True,
            qty=qty,
            avg_price=fill,
            entry_fee=fee,
            entry_time=bar_ms,
            entry_order=entry_order,
            protective=self._protective(position),
            message="paper fill",
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
        if ref_price is None:
            raise ValueError("PaperBroker.close_position requires ref_price")
        ref = float(ref_price)
        if not math.isfinite(ref) or ref <= 0:
            raise ValueError(f"ref_price must be > 0, got {ref_price!r}")
        state = self._load(symbol)
        pos = state["position"]
        if pos is None:
            return None

        bar_ms = int(bar_time)
        if self.include_funding:
            self._settle_funding_for_close(symbol, state, pos, bar_ms, fallback_price=ref)

        direction = Direction(pos["direction"])
        closing = direction.closing_side
        qty = float(pos["qty"])
        exit_price = float(self.fill_model.exit_fill_price(ref, closing))
        exit_fee = float(self.fill_model.fee(qty, exit_price))
        gross = direction.sign * qty * (exit_price - float(pos["entry_price"]))
        state["cash"] = float(state["cash"]) + gross - exit_fee
        order = OrderResult(
            client_id=str(client_id),
            exchange_id=None,
            symbol=symbol,
            side=closing,
            order_type=OrderType.MARKET,
            purpose=exit_purpose(ExitReason(reason)),
            status=OrderStatus.FILLED,
            requested_qty=qty,
            executed_qty=qty,
            avg_price=exit_price,
            trigger_price=None,
            fee=exit_fee,
            ts=bar_ms,
        )
        closure = PositionClosure(
            exit_time=bar_ms,
            exit_price=exit_price,
            qty=qty,
            reason=ExitReason(reason),
            exit_fee=exit_fee,
            funding=float(pos["funding"]),
            gross_pnl=gross,
            order=order,
        )
        state["position"] = None
        self._save(symbol, state)
        logger.info(
            "paper close %s %s qty=%s exit=%.8g reason=%s",
            symbol,
            direction.value,
            qty,
            exit_price,
            ExitReason(reason).value,
        )
        return closure

    def ensure_protection(
        self,
        active: ActiveTrade,
        account: AccountSnapshot,
        *,
        sl_client_id: str,
        tp_client_id: str | None,
    ) -> tuple[ProtectiveOrder, ...]:
        """Paper stops are simulated per candle: return the synthesized orders, never place anything."""
        state = self._load(active.symbol)
        pos = state["position"]
        if pos is None:
            return ()
        return self._protective(pos)

    # ------------------------------------------------------------------------------------------ simulation

    def _simulate(self, symbol: str, state: dict[str, Any], candles: list[Candle]) -> PositionClosure | None:
        """§9.3 sync steps 1-3 over ``candles`` (ascending, non-empty). Mutates ``state``."""
        last = candles[-1]
        pos = state["position"]
        cursor_bar = state["last_bar_open_time"]
        if cursor_bar is None:
            if pos is None:
                # Step 1: first sync -> start here, never simulate history.
                state["last_bar_open_time"] = int(last.open_time)
                state["last_close"] = float(last.close)
                return None
            # SPEC-GAP: a position opened before the very first sync; simulate from its entry bar instead of
            # silently skipping bars in which its stop could have been hit (conservative).
            cursor_bar = int(pos["entry_bar_open_time"]) - 1

        closure: PositionClosure | None = None
        if pos is not None:
            relevant = [
                c for c in candles if c.open_time > cursor_bar and c.open_time >= int(pos["entry_bar_open_time"])
            ]
            if relevant:
                events = self._sync_funding_events(symbol, state, pos, relevant[-1].close_time)
                closure = self._run_candles(symbol, state, pos, relevant, events)

        # Step 3: advance the candle cursor (never backwards).
        if state["last_bar_open_time"] is None or int(last.open_time) >= int(state["last_bar_open_time"]):
            state["last_bar_open_time"] = int(last.open_time)
            state["last_close"] = float(last.close)
        return closure

    def _sync_funding_events(
        self, symbol: str, state: dict[str, Any], pos: dict[str, Any], end_ms: int
    ) -> list[FundingEvent]:
        """Funding rows for the whole sync range, fetched ONCE per sync (§9.3 step 2a)."""
        if not self.include_funding:
            return []
        cursor = self._funding_cursor(state, pos)
        start = cursor + 1
        if start > int(end_ms):
            return []
        return self._fetch_funding(symbol, start, int(end_ms))

    def _run_candles(
        self,
        symbol: str,
        state: dict[str, Any],
        pos: dict[str, Any],
        candles: list[Candle],
        events: list[FundingEvent],
    ) -> PositionClosure | None:
        direction = Direction(pos["direction"])
        stop = float(pos["stop"])
        tp = _opt_float(pos["tp"])
        liq = _opt_float(pos["liq"])
        for c in candles:
            # a. funding events up to (and including) this bar's open.
            self._apply_funding_events(state, pos, events, int(c.open_time), fallback_price=float(c.open))
            # b. protective exits / liquidation inside the bar (gap rules, then SL-first).
            hit = resolve_intrabar_exit(
                direction, float(c.open), float(c.high), float(c.low), stop, tp, liq
            )
            if hit is None:
                continue
            self._apply_funding_events(state, pos, events, int(c.close_time), fallback_price=float(c.open))
            reason, base = hit
            closure = self._book_protective_exit(symbol, state, pos, ExitReason(reason), float(base), c)
            state["position"] = None
            return closure
        return None

    def _book_protective_exit(
        self,
        symbol: str,
        state: dict[str, Any],
        pos: dict[str, Any],
        reason: ExitReason,
        base_price: float,
        candle: Candle,
    ) -> PositionClosure:
        direction = Direction(pos["direction"])
        closing = direction.closing_side
        qty = float(pos["qty"])
        entry = float(pos["entry_price"])
        funding_paid = float(pos["funding"])
        exit_time = int(candle.close_time)
        order: OrderResult | None
        if reason is ExitReason.LIQUIDATION:
            exit_price = float(base_price)
            gross = float(liquidation_loss(qty, entry, int(pos["leverage"]), funding_paid=funding_paid))
            exit_fee = 0.0
            order = None
        else:
            exit_price = float(self.fill_model.exit_fill_price(base_price, closing))
            exit_fee = float(self.fill_model.fee(qty, exit_price))
            gross = direction.sign * qty * (exit_price - entry)
            is_sl = reason is ExitReason.STOP_LOSS
            order = OrderResult(
                client_id=str(pos["sl_client_id"] if is_sl else (pos.get("tp_client_id") or "")),
                exchange_id=None,
                symbol=symbol,
                side=closing,
                order_type=OrderType.STOP_MARKET if is_sl else OrderType.TAKE_PROFIT_MARKET,
                purpose=OrderPurpose.STOP_LOSS if is_sl else OrderPurpose.TAKE_PROFIT,
                status=OrderStatus.FILLED,
                requested_qty=None,
                executed_qty=qty,
                avg_price=exit_price,
                trigger_price=float(pos["stop"]) if is_sl else _opt_float(pos["tp"]),
                fee=exit_fee,
                ts=exit_time,
            )
        state["cash"] = float(state["cash"]) + gross - exit_fee
        logger.info(
            "paper %s %s %s qty=%s exit=%.8g at %s",
            reason.value,
            symbol,
            direction.value,
            qty,
            exit_price,
            ms_to_iso(exit_time),
        )
        return PositionClosure(
            exit_time=exit_time,
            exit_price=exit_price,
            qty=qty,
            reason=reason,
            exit_fee=exit_fee,
            funding=funding_paid,
            gross_pnl=gross,
            order=order,
        )

    # ------------------------------------------------------------------------------------------ funding

    @staticmethod
    def _funding_cursor(state: dict[str, Any], pos: dict[str, Any]) -> int:
        cursor = state["funding_cursor"]
        return int(pos["entry_time"]) if cursor is None else int(cursor)

    def _fetch_funding(self, symbol: str, start_ms: int, end_ms: int) -> list[FundingEvent]:
        df = self.market.funding_rates(symbol, int(start_ms), int(end_ms))
        events: dict[int, FundingEvent] = {}
        if df is not None and len(df) > 0:
            has_mark = "mark_price" in df.columns
            cols = ["funding_time", "funding_rate"] + (["mark_price"] if has_mark else [])
            for row in df.loc[:, cols].itertuples(index=False, name=None):
                ft = int(row[0])
                rate = float(row[1])
                if not math.isfinite(rate):
                    continue
                mark = _finite_positive(row[2]) if has_mark else None
                events[ft] = (ft, rate, mark)
        out = [events[k] for k in sorted(events)]
        if out:
            known = self._known_funding_times.setdefault(symbol, [])
            known.extend(ft for ft, _, _ in out)
            self._known_funding_times[symbol] = sorted(set(known))[-_KNOWN_FUNDING_TIMES_KEEP:]
        return out

    def _apply_funding_events(
        self,
        state: dict[str, Any],
        pos: dict[str, Any],
        events: list[FundingEvent],
        upto_ms: int,
        *,
        fallback_price: float,
    ) -> int:
        """Charge every event with ``entry_time < ft <= upto_ms`` and ``ft > funding_cursor``. Returns the count."""
        applied = 0
        direction = Direction(pos["direction"])
        qty_signed = direction.sign * float(pos["qty"])
        entry_time = int(pos["entry_time"])
        for ft, rate, mark in events:
            if ft > upto_ms:
                break
            if ft <= entry_time or ft <= self._funding_cursor(state, pos):
                continue
            price = mark if mark is not None else float(fallback_price)
            payment = float(funding_payment(qty_signed, price, rate))
            state["cash"] = float(state["cash"]) - payment
            pos["funding"] = float(pos["funding"]) + payment
            state["funding_cursor"] = int(ft)
            applied += 1
        return applied

    def _settle_funding_for_close(
        self, symbol: str, state: dict[str, Any], pos: dict[str, Any], bar_time: int, *, fallback_price: float
    ) -> None:
        """Charge pending funding up to ``bar_time`` (inclusive), incl. the late-record rule (§9.3)."""
        entry_time = int(pos["entry_time"])
        start_cursor = self._funding_cursor(state, pos)
        if bar_time <= start_cursor:
            return
        rows = self._fetch_funding(symbol, start_cursor + 1, bar_time)
        self._apply_funding_events(state, pos, rows, bar_time, fallback_price=fallback_price)
        if any(ft == bar_time for ft, _, _ in rows) or bar_time <= entry_time:
            return

        # Late-record rule: is a settlement due exactly at bar_time whose record is not published yet?
        real_times = {ft for ft, _, _ in rows}
        real_times.update(self._known_funding_times.get(symbol, ()))
        if start_cursor > entry_time:  # the cursor was set by an applied event -> a real funding time
            real_times.add(start_cursor)
        real_sorted = sorted(t for t in real_times if t <= bar_time)
        last_known_ft = max([start_cursor, *real_sorted])
        interval = DEFAULT_FUNDING_INTERVAL_MS
        if len(real_sorted) >= 2 and real_sorted[-1] - real_sorted[-2] > 0:
            interval = real_sorted[-1] - real_sorted[-2]
        # SPEC-GAP: when no real funding time is known yet (the cursor is still the entry bar time), anchor the
        # schedule at the epoch: Binance settlements (1h/4h/8h) are aligned to 00:00 UTC.
        anchor = real_sorted[-1] if real_sorted else 0
        if not (bar_time > last_known_ft and (bar_time - anchor) % interval == 0):
            return

        for _ in range(LATE_FUNDING_REFETCHES):
            self.sleep(LATE_FUNDING_SLEEP_SEC)
            cursor = self._funding_cursor(state, pos)
            if bar_time <= cursor:
                return
            more = self._fetch_funding(symbol, cursor + 1, bar_time)
            self._apply_funding_events(state, pos, more, bar_time, fallback_price=fallback_price)
            if any(ft == bar_time for ft, _, _ in more):
                return

        # Still unpublished: charge an estimate from premiumIndex.
        p = self.market.premium_index(symbol)
        mark = float(p["mark_price"])
        rate = float(p["last_funding_rate"])
        direction = Direction(pos["direction"])
        payment = float(funding_payment(direction.sign * float(pos["qty"]), mark, rate))
        state["cash"] = float(state["cash"]) - payment
        pos["funding"] = float(pos["funding"]) + payment
        state["funding_cursor"] = int(bar_time)
        when = ms_to_iso(bar_time)
        logger.warning("funding at %s estimated from premiumIndex", when)
        self.storage.log_event(
            "WARNING",
            Mode.PAPER.value,
            "FUNDING_ESTIMATED",
            f"funding at {when} estimated from premiumIndex (rate {rate:.8f}, mark {mark:.8g}, payment {payment:.8g})",
            ts_ms=now_ms(self.clock),
        )

    # ------------------------------------------------------------------------------------------ account

    def _protective(self, pos: dict[str, Any]) -> tuple[ProtectiveOrder, ...]:
        closing = Direction(pos["direction"]).closing_side
        orders = [
            ProtectiveOrder(
                kind=OrderPurpose.STOP_LOSS,
                client_id=str(pos.get("sl_client_id") or ""),
                exchange_id=None,
                side=closing,
                trigger_price=float(pos["stop"]),
                status="NEW",
                close_position=True,
                quantity=None,
            )
        ]
        if pos.get("tp") is not None:
            orders.append(
                ProtectiveOrder(
                    kind=OrderPurpose.TAKE_PROFIT,
                    client_id=str(pos.get("tp_client_id") or ""),
                    exchange_id=None,
                    side=closing,
                    trigger_price=float(pos["tp"]),
                    status="NEW",
                    close_position=True,
                    quantity=None,
                )
            )
        return tuple(orders)

    def _account(self, symbol: str, state: dict[str, Any]) -> AccountSnapshot:
        ts = now_ms(self.clock)
        cash = float(state["cash"])
        pos = state["position"]
        if pos is None:
            return AccountSnapshot(
                ts=ts,
                wallet_balance=cash,
                equity=cash,
                available_balance=cash,
                unrealized_pnl=0.0,
                position=None,
            )
        direction = Direction(pos["direction"])
        qty = float(pos["qty"])
        entry = float(pos["entry_price"])
        leverage = int(pos["leverage"])
        mark = float(state["last_close"]) if state["last_close"] is not None else entry
        upnl = direction.sign * qty * (mark - entry)
        margin = qty * entry / leverage
        position = Position(
            symbol=symbol,
            qty=direction.sign * qty,
            entry_price=entry,
            mark_price=mark,
            unrealized_pnl=upnl,
            liquidation_price=_opt_float(pos["liq"]),
            isolated_margin=margin,
            leverage=leverage,
            updated_at=ts,
        )
        return AccountSnapshot(
            ts=ts,
            wallet_balance=cash,
            equity=cash + upnl,
            available_balance=cash - margin,
            unrealized_pnl=upnl,
            position=position,
            protective_orders=self._protective(pos),
            open_orders_count=0,
        )


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def _opt_int(v: Any) -> int | None:
    return None if v is None else int(v)


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def _finite_positive(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _normalize_position(pos: dict[str, Any]) -> dict[str, Any]:
    """Coerce a persisted position dict to native types (JSON round-trips keep them, this is defensive)."""
    return {
        "direction": Direction(pos["direction"]).value,
        "qty": float(pos["qty"]),
        "entry_price": float(pos["entry_price"]),
        "entry_time": int(pos["entry_time"]),
        "entry_bar_open_time": int(pos.get("entry_bar_open_time", pos["entry_time"])),
        "stop": float(pos["stop"]),
        "tp": _opt_float(pos.get("tp")),
        "liq": _opt_float(pos.get("liq")),
        "leverage": int(pos["leverage"]),
        "sl_client_id": str(pos.get("sl_client_id") or ""),
        "tp_client_id": None if pos.get("tp_client_id") is None else str(pos["tp_client_id"]),
        "funding": float(pos.get("funding") or 0.0),
    }
