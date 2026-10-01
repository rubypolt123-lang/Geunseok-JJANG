"""Bar-by-bar backtest engine (SPEC §11.1).

Timeline of one bar ``i`` (``o, h, l, c``; ``t_open``; ``t_close``):
1. funding events with ``ft <= t_open`` (charged only if the position was entered before ``ft``);
2. the action decided at the close of bar ``i-1`` executes at this bar's open (close leg, then open leg);
3. protective exits inside the bar (``fillmodel.resolve_intrabar_exit``: gap rules, then SL-first);
4. last bar: close any position (END_OF_DATA);
5. mark-to-market at the close;
6. daily-loss kill switch on the bar's close time (flatten at the next open if configured);
7. strategy signal at the close -> pending action for bar ``i+1``.

Look-ahead guarantees: decisions at bar ``i`` only use rows ``<= i``; fills use row ``i+1``'s open; intrabar exits
use the bar's own OHLC after the entry at its open. The same ``plan_entry`` / ``decide_action`` / kill switch /
cooldown code as the live trader is used, so backtest and live agree on the same bars.
"""

from __future__ import annotations

import logging
import math
import secrets
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pandas as pd

from bot.backtest.metrics import compute_metrics
from bot.config import ExecutionConfig, RiskConfig
from bot.errors import DataError
from bot.fillmodel import FillModel, funding_payment, liquidation_loss, resolve_intrabar_exit
from bot.models import (
    Action,
    ActiveTrade,
    BacktestResult,
    Direction,
    ExitReason,
    PositionClosure,
    SymbolFilters,
    Trade,
    to_jsonable,
    validate_candles_df,
)
from bot.risk import Cooldown, DailyLossKillSwitch, decide_action, plan_entry
from bot.strategy import indicators
from bot.timeutil import interval_to_ms, ms_to_iso, now_ms

if TYPE_CHECKING:
    from bot.strategy.base import Strategy

logger = logging.getLogger(__name__)

__all__ = ["TRADE_SOURCE", "new_run_id", "run_backtest"]

TRADE_SOURCE: Final = "backtest"
ENTRY_CLIENT_ID: Final = "bt"

_CLOSE_ACTIONS: Final = frozenset({Action.CLOSE, Action.FLIP_LONG, Action.FLIP_SHORT})
_OPEN_DIRECTION: Final[dict[Action, Direction]] = {
    Action.OPEN_LONG: Direction.LONG,
    Action.FLIP_LONG: Direction.LONG,
    Action.OPEN_SHORT: Direction.SHORT,
    Action.FLIP_SHORT: Direction.SHORT,
}


def new_run_id(clock: Callable[[], float] = time.time) -> str:
    """``"bt-YYYYMMDD-HHMMSS-<6 hex>"`` in UTC."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(clock()))
    return f"bt-{stamp}-{secrets.token_hex(3)}"


def _funding_events(funding: pd.DataFrame | None) -> tuple[list[int], list[float], list[float]]:
    """Sorted, de-duplicated ``(funding_time, funding_rate, mark_price)`` lists (mark may be NaN)."""
    if funding is None or len(funding) == 0:
        return [], [], []
    missing = [c for c in ("funding_time", "funding_rate") if c not in funding.columns]
    if missing:
        raise DataError(f"funding frame is missing columns: {', '.join(missing)}")
    marks = (
        pd.to_numeric(funding["mark_price"], errors="coerce")
        if "mark_price" in funding.columns
        else pd.Series(np.nan, index=funding.index)
    )
    frame = pd.DataFrame(
        {
            "t": pd.to_numeric(funding["funding_time"], errors="coerce"),
            "r": pd.to_numeric(funding["funding_rate"], errors="coerce"),
            "m": marks,
        }
    )
    bad = frame["t"].isna() | ~np.isfinite(frame["r"].to_numpy(dtype=np.float64, na_value=np.nan))
    if bool(bad.any()):
        logger.warning("ignoring %d funding rows without a valid time/rate", int(bad.sum()))
        frame = frame.loc[~bad]
    frame = frame.astype({"t": "int64", "r": "float64", "m": "float64"})
    frame = frame.sort_values("t", kind="stable").drop_duplicates("t", keep="last")
    return frame["t"].tolist(), frame["r"].tolist(), frame["m"].tolist()


class _Book:
    """Cash, the open position and the closed trades of one backtest run."""

    def __init__(
        self,
        *,
        symbol: str,
        run_id: str,
        fm: FillModel,
        filters: SymbolFilters,
        risk: RiskConfig,
        execution: ExecutionConfig,
        cash: float,
        funding: tuple[list[int], list[float], list[float]],
    ) -> None:
        self.symbol = symbol
        self.run_id = run_id
        self.fm = fm
        self.filters = filters
        self.risk = risk
        self.execution = execution
        self.cash = cash
        self.pos: ActiveTrade | None = None
        self.pos_funding = 0.0  # funding of the open position so far (+ = paid)
        self.trades: list[Trade] = []
        self.n_entries = 0
        self.rejected_entries = 0
        self.entries_capped_by_notional = 0
        self.funding_events = 0
        self._ft, self._rate, self._mark = funding
        self._fp = 0

    # ------------------------------------------------------------------ funding
    def skip_funding_through(self, t_ms: int) -> None:
        """Advance the funding pointer past every event with ``ft <= t_ms`` without charging it."""
        while self._fp < len(self._ft) and self._ft[self._fp] <= t_ms:
            self._fp += 1

    def apply_funding_until(self, t_ms: int, fallback_price: float) -> None:
        """Charge every unprocessed event with ``ft <= t_ms``; only a position entered BEFORE ``ft`` pays.

        (A fill at a bar open equal to ``ft`` happens just after the settlement: entry at ``ft`` does not pay,
        an exit at ``ft`` does.)
        """
        while self._fp < len(self._ft) and self._ft[self._fp] <= t_ms:
            ft = self._ft[self._fp]
            pos = self.pos
            if pos is not None and pos.entry_time < ft:
                mark = self._mark[self._fp]
                price = mark if (math.isfinite(mark) and mark > 0) else fallback_price
                pay = funding_payment(pos.direction.sign * pos.qty, price, self._rate[self._fp])
                self.cash -= pay
                self.pos_funding += pay
                self.funding_events += 1
            self._fp += 1

    # ------------------------------------------------------------------ exits
    def _book_exit(
        self,
        *,
        exit_time: int,
        exit_price: float,
        reason: ExitReason,
        exit_fee: float,
        gross: float,
    ) -> Trade:
        pos = self.pos
        if pos is None:
            raise RuntimeError("no open position to close")
        self.cash += gross - exit_fee
        closure = PositionClosure(
            exit_time=int(exit_time),
            exit_price=float(exit_price),
            qty=pos.qty,
            reason=reason,
            exit_fee=float(exit_fee),
            funding=self.pos_funding,
            gross_pnl=float(gross),
        )
        trade = Trade.from_closure(pos, closure, source=TRADE_SOURCE, run_id=self.run_id)
        self.trades.append(trade)
        self.pos = None
        self.pos_funding = 0.0
        return trade

    def exit_at(self, base_price: float, *, exit_time: int, reason: ExitReason) -> Trade:
        """Market-style exit: adverse slippage on ``base_price`` and the taker fee."""
        pos = self.pos
        if pos is None:
            raise RuntimeError("no open position to close")
        price = self.fm.exit_fill_price(base_price, pos.direction.closing_side)
        fee = self.fm.fee(pos.qty, price, taker=True)
        gross = pos.direction.sign * pos.qty * (price - pos.entry_price)
        return self._book_exit(exit_time=exit_time, exit_price=price, reason=reason, exit_fee=fee, gross=gross)

    def liquidate(self, liq_price: float, *, exit_time: int) -> Trade:
        """Isolated-margin liquidation: the whole initial margin is lost (funding netted), no exit fee."""
        pos = self.pos
        if pos is None:
            raise RuntimeError("no open position to liquidate")
        gross = liquidation_loss(pos.qty, pos.entry_price, pos.leverage, funding_paid=self.pos_funding)
        return self._book_exit(
            exit_time=exit_time, exit_price=liq_price, reason=ExitReason.LIQUIDATION, exit_fee=0.0, gross=gross
        )

    # ------------------------------------------------------------------ entries
    def try_open(self, direction: Direction, *, ref_price: float, atr_value: float | None, t_open: int) -> bool:
        """Size with ``plan_entry`` from the current cash and fill at ``ref_price`` (+ slippage, taker fee)."""
        if self.pos is not None:
            raise RuntimeError("cannot open a position while another one is open")
        decision = plan_entry(
            direction=direction,
            ref_price=ref_price,
            equity=self.cash,
            atr_value=atr_value,
            filters=self.filters,
            risk=self.risk,
            fees=self.execution.fees,
            slippage_bps=self.execution.slippage_bps,
        )
        plan = decision.plan
        if plan is None:
            self.rejected_entries += 1
            logger.debug("entry %s at %s rejected: %s", direction.value, ms_to_iso(t_open), decision.reason)
            return False
        fill = self.fm.market_fill_price(ref_price, plan.direction.opening_side)
        qty = float(plan.qty)
        fee = self.fm.fee(qty, fill, taker=True)
        self.cash -= fee
        self.n_entries += 1
        self.pos = ActiveTrade(
            trade_id=f"{self.run_id}-{self.n_entries:05d}",
            symbol=self.symbol,
            direction=plan.direction,
            qty=qty,
            entry_price=fill,
            entry_time=t_open,
            entry_bar_open_time=t_open,
            stop_price=float(plan.stop_price),
            take_profit_price=None if plan.take_profit_price is None else float(plan.take_profit_price),
            liquidation_price=float(plan.liquidation_price),
            leverage=int(plan.leverage),
            risk_amount=float(plan.risk_amount),
            entry_fee=fee,
            entry_client_id=ENTRY_CLIENT_ID,
        )
        self.pos_funding = 0.0
        if plan.sizing_cap == "notional":
            self.entries_capped_by_notional += 1
        return True

    # ------------------------------------------------------------------ state
    @property
    def direction(self) -> Direction:
        return Direction.FLAT if self.pos is None else self.pos.direction

    def signed_qty(self) -> float:
        return 0.0 if self.pos is None else self.pos.direction.sign * self.pos.qty

    def equity_at(self, price: float) -> float:
        pos = self.pos
        if pos is None:
            return self.cash
        return self.cash + pos.direction.sign * pos.qty * (price - pos.entry_price)


def _default_config_snapshot(
    *,
    symbol: str,
    interval: str,
    risk: RiskConfig,
    execution: ExecutionConfig,
    initial_balance: float,
    trade_start_ms: int | None,
    include_funding: bool,
) -> dict[str, Any]:
    return to_jsonable(
        {
            "symbol": symbol,
            "interval": interval,
            "risk": risk,
            "execution": execution,
            "initial_balance": initial_balance,
            "trade_start_ms": trade_start_ms,
            "funding_included": include_funding,
        }
    )


def _start_index(
    open_times: np.ndarray,
    *,
    warmup_bars: int,
    atr_period: int,
    trade_start_ms: int | None,
) -> int:
    first_trade_idx = 0
    if trade_start_ms is not None:
        first_trade_idx = int(np.searchsorted(open_times, int(trade_start_ms), side="left"))
    return max(int(warmup_bars) - 1, int(atr_period), first_trade_idx, 0)


def run_backtest(
    candles: pd.DataFrame,
    strategy: Strategy,
    *,
    symbol: str,
    interval: str,
    filters: SymbolFilters,
    risk: RiskConfig,
    execution: ExecutionConfig,
    initial_balance: float,
    funding: pd.DataFrame | None = None,
    trade_start_ms: int | None = None,
    run_id: str | None = None,
    config_snapshot: dict | None = None,
) -> BacktestResult:
    """Simulate ``strategy`` over closed ``candles`` (signal at the close, fill at the next open).

    ``funding`` (columns ``funding_time``, ``funding_rate``, ``mark_price``) None or empty -> no funding is
    charged; ``metrics["funding_events"]`` counts the events actually charged. ``trade_start_ms``: the first bar
    that may produce a decision (earlier rows only warm up indicators).
    """
    interval_ms = interval_to_ms(interval)
    balance = float(initial_balance)
    if not math.isfinite(balance) or balance <= 0:
        raise ValueError(f"initial_balance must be a positive number (got {initial_balance!r})")
    validate_candles_df(candles, interval_ms)
    n = len(candles)

    atr_mode = risk.stop_loss.mode == "atr"
    atr_period = int(risk.stop_loss.atr_period) if atr_mode else 0
    open_times_np = candles["open_time"].to_numpy(dtype=np.int64)
    s = _start_index(
        open_times_np,
        warmup_bars=strategy.warmup_bars,
        atr_period=atr_period,
        trade_start_ms=trade_start_ms,
    )
    if s >= n - 1:
        raise DataError(
            f"not enough candles: {n} bars, first decision bar index {s} "
            f"(warmup {strategy.warmup_bars}, atr {atr_period}, trade start {trade_start_ms})"
        )

    prepared = strategy.prepare(candles)
    if len(prepared) != n:
        raise DataError(f"strategy.prepare returned {len(prepared)} rows for {n} candles")
    atr_values: list[float] | None = None
    if atr_mode:
        atr_values = [float(x) for x in indicators.atr(candles, atr_period).to_numpy(dtype=np.float64)]

    run_id = run_id or new_run_id()
    fm = FillModel.from_config(execution)
    kill = DailyLossKillSwitch(risk.max_daily_loss_pct)
    kill.seed(balance)
    cool = Cooldown(risk.cooldown_bars_after_stop, interval_ms)
    events = _funding_events(funding)
    book = _Book(
        symbol=symbol,
        run_id=run_id,
        fm=fm,
        filters=filters,
        risk=risk,
        execution=execution,
        cash=balance,
        funding=events,
    )

    # native Python values (numpy boundary, §0.2)
    open_time = open_times_np.tolist()
    close_time = candles["close_time"].to_numpy(dtype=np.int64).tolist()
    opens = candles["open"].to_numpy(dtype=np.float64).tolist()
    highs = candles["high"].to_numpy(dtype=np.float64).tolist()
    lows = candles["low"].to_numpy(dtype=np.float64).tolist()
    closes = candles["close"].to_numpy(dtype=np.float64).tolist()

    book.skip_funding_through(open_time[s])  # events before the first simulated bar are never charged

    eq_time: list[int] = []
    eq_value: list[float] = []
    eq_in_pos: list[bool] = []
    eq_qty: list[float] = []

    # (action, reason of the close leg) decided at the close of the previous bar
    pending: tuple[Action, ExitReason | None] | None = None

    for i in range(s, n):
        o, h, lo, c = opens[i], highs[i], lows[i], closes[i]
        t_open, t_close = open_time[i], close_time[i]

        # 1. funding up to (and including) this bar's open
        book.apply_funding_until(t_open, o)

        # 2. pending action at the open
        if pending is not None:
            action, close_reason = pending
            pending = None
            if action in _CLOSE_ACTIONS and book.pos is not None:
                book.exit_at(o, exit_time=t_open, reason=close_reason or ExitReason.SIGNAL)
            open_dir = _OPEN_DIRECTION.get(action)
            if open_dir is not None:
                if book.pos is None:
                    atr_prev: float | None = None
                    if atr_values is not None:
                        a = atr_values[i - 1]
                        atr_prev = a if math.isfinite(a) else None
                    # FLIP: cash is already the cash AFTER the close leg at this same open.
                    book.try_open(open_dir, ref_price=o, atr_value=atr_prev, t_open=t_open)
                else:
                    logger.warning("skipping %s at %s: position still open", action.value, ms_to_iso(t_open))
        in_position = book.pos is not None

        # 3. intrabar protective exits
        pos = book.pos
        if pos is not None:
            hit = resolve_intrabar_exit(
                pos.direction, o, h, lo, pos.stop_price, pos.take_profit_price, pos.liquidation_price
            )
            if hit is not None:
                reason, base = hit
                book.apply_funding_until(t_close, o)
                if reason is ExitReason.LIQUIDATION:
                    book.liquidate(base, exit_time=t_close)
                else:
                    book.exit_at(base, exit_time=t_close, reason=reason)
                if reason in (ExitReason.STOP_LOSS, ExitReason.LIQUIDATION):
                    cool.trigger(t_open)

        # 4. end of data
        if i == n - 1 and book.pos is not None:
            # SPEC-GAP: §11.1 step 4 does not mention funding; the timestamp rule (entry < ft <= exit) charges
            # events inside the last bar (only possible for intervals longer than the funding interval).
            book.apply_funding_until(t_close, o)
            book.exit_at(c, exit_time=t_close, reason=ExitReason.END_OF_DATA)

        # 5. mark-to-market at the close
        equity_i = book.equity_at(c)
        eq_time.append(t_open)
        eq_value.append(equity_i)
        eq_in_pos.append(in_position)
        eq_qty.append(book.signed_qty())

        # 6. daily-loss kill switch (bar CLOSE time, identical to the trader)
        if kill.update(t_close, equity_i):
            logger.debug("backtest kill switch tripped at %s: %s", ms_to_iso(t_close), kill.reason)
        if kill.tripped and risk.kill_switch_flatten and book.pos is not None:
            pending = (Action.CLOSE, ExitReason.KILL_SWITCH)
            continue

        # 7. strategy signal at the close -> action for the next open
        if i < n - 1:
            sig = strategy.signal_at(prepared, i)
            entries_allowed = kill.entries_allowed and not cool.active(t_open)
            action = Action(decide_action(sig.action, book.direction, entries_allowed))
            if action is not Action.NONE:
                if action in (Action.FLIP_LONG, Action.FLIP_SHORT):
                    pending = (action, ExitReason.FLIP)
                elif action is Action.CLOSE:
                    pending = (action, ExitReason.SIGNAL)
                else:
                    pending = (action, None)

    equity_df = pd.DataFrame(
        {
            "time": np.asarray(eq_time, dtype=np.int64),
            "equity": np.asarray(eq_value, dtype=np.float64),
            "in_position": np.asarray(eq_in_pos, dtype=bool),
            "position_qty": np.asarray(eq_qty, dtype=np.float64),
        }
    )
    metrics = compute_metrics(equity_df, book.trades, initial_balance=balance, interval=interval)
    metrics["rejected_entries"] = int(book.rejected_entries)
    metrics["entries_capped_by_notional"] = int(book.entries_capped_by_notional)
    metrics["funding_events"] = int(book.funding_events)

    params = strategy.params if isinstance(strategy.params, Mapping) else {}
    config = (
        to_jsonable(dict(config_snapshot))
        if config_snapshot is not None
        else _default_config_snapshot(
            symbol=symbol,
            interval=interval,
            risk=risk,
            execution=execution,
            initial_balance=balance,
            trade_start_ms=trade_start_ms,
            include_funding=bool(events[0]),
        )
    )
    result = BacktestResult(
        run_id=run_id,
        created_at=now_ms(),
        symbol=symbol,
        interval=interval,
        strategy=str(strategy.name),
        params=to_jsonable(dict(params)),
        config=config,
        start_time=int(open_time[s]),
        end_time=int(close_time[n - 1]),
        initial_balance=balance,
        metrics=metrics,
        equity=equity_df,
        trades=book.trades,
    )
    logger.info(
        "backtest %s %s %s: %d bars (%s .. %s), %d trades, final equity %.2f, %d funding events",
        run_id,
        symbol,
        interval,
        len(eq_time),
        ms_to_iso(result.start_time),
        ms_to_iso(result.end_time),
        len(book.trades),
        metrics["final_equity"] if metrics["final_equity"] is not None else float("nan"),
        book.funding_events,
    )
    return result
