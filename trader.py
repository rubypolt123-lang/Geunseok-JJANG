"""Trader loop (SPEC §10): one decision per CLOSED candle, exchange-side protection, crash-safe state.

Timeline of one iteration (``run_once``), in this exact order:
 1. recent klines (closed + forming candle, clock re-sync)        8. skip a bar that was already processed
 2. staleness check (3 retries, 2 s apart)                        9. strategy signal + entries gate
 3. store candles for the dashboard                              10. decide the action, record the signal
 4. iteration constants (ids, ref price, ATR)                    11. execute (close / flip / open)
 5. broker sync -> ``_handle_sync`` (closures, adoption, SL)     12. persist ``last_bar`` (even on failure)
 6. daily-loss kill switch (flatten while tripped)               13./14. post-execution sync, equity, status
 7. stale data -> no new decisions

Safety invariants: never act on a forming candle or twice on the same bar; never open unless flat; a position
always carries an exchange stop (re-checked between candles on testnet/live); kill switch / cooldown / halt file /
``halted`` block ENTRIES only; a stop request never interrupts an order sequence.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import signal as signal_module
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pandas as pd
import requests

from bot.broker.base import Broker
from bot.broker.exchange_broker import ExchangeBroker
from bot.broker.paper import PaperBroker
from bot.config import (
    MAINNET_REST_URL,
    TESTNET_REST_URL,
    AppConfig,
    RiskConfig,
    assert_live_allowed,
    load_credentials,
)
from bot.errors import (
    AuthError,
    BotError,
    ConfigError,
    EmergencyError,
    IpBannedError,
    ProtectionFailedError,
    RateLimitError,
    StaleDataError,
    TimestampError,
    TransientError,
)
from bot.exchange.filters import round_protective_price
from bot.exchange.market import MarketData
from bot.exchange.rest import BinanceRestClient
from bot.fillmodel import FillModel
from bot.models import (
    AccountSnapshot,
    Action,
    ActiveTrade,
    BotState,
    BotStatus,
    Candle,
    Direction,
    ExitReason,
    Mode,
    OpenOutcome,
    Position,
    PositionClosure,
    Signal,
    SignalAction,
    SymbolFilters,
    SyncResult,
    Trade,
    TradePlan,
    candles_from_df,
    make_client_id,
)
from bot.risk import Cooldown, DailyLossKillSwitch, compute_stop_price, compute_take_profit, decide_action, plan_entry
from bot.strategy import create_strategy, indicators, load_strategy_modules
from bot.timeutil import expected_last_closed_open, floor_time, interval_to_ms, ms_to_iso, next_close_ms, now_ms

if TYPE_CHECKING:
    from bot.storage import Storage
    from bot.strategy.base import Strategy

# module level: conditional imports only (SPEC §10.1)
if os.name == "nt":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

__all__ = [
    "IterationReport",
    "SingleInstanceLock",
    "Trader",
    "active_trade_key",
    "build_trader",
    "cooldown_key",
    "emergency_key",
    "halted_key",
    "kill_switch_key",
    "last_bar_key",
    "live_banner",
]

LOCK_FILE: Final = "data/trader.lock"  # ONE global lock: only one trader process of any mode (§5)
MAX_KLINES: Final = 1500
CANDLES_TO_STORE: Final = 500
STALE_RETRIES: Final = 3
STALE_RETRY_SLEEP_SEC: Final = 2.0
LIVE_COUNTDOWN_SEC: Final = 10
EMERGENCY_RETRY_SEC: Final = 10.0
MAX_CONSECUTIVE_ERRORS: Final = 5
PROTECTION_CHECK_MAX_SEC: Final = 60
DEFAULT_RATE_LIMIT_PAUSE_SEC: Final = 60.0
QTY_EPS: Final = 1e-12

USER_STOP_MESSAGE: Final = "stopped by user (positions and exchange stop orders are kept)"
ONCE_MESSAGE: Final = "single run finished (--once)"
ABORTED_MESSAGE: Final = "stopped by user before startup finished (no new orders were sent)"

# SyncResult issue codes (§4.1)
ISSUE_UNTRACKED_POSITION: Final = "UNTRACKED_POSITION"
ISSUE_QTY_MISMATCH: Final = "QTY_MISMATCH"
ISSUE_ORPHAN_PROTECTIVE_CANCELED: Final = "ORPHAN_PROTECTIVE_CANCELED"
ISSUE_FOREIGN_OPEN_ORDERS: Final = "FOREIGN_OPEN_ORDERS"
ISSUE_SL_MISSING: Final = "SL_MISSING"
ISSUE_PROTECTION_QTY_MISMATCH: Final = "PROTECTION_QTY_MISMATCH"
ISSUE_CLOSURE_DETAILS_UNKNOWN: Final = "CLOSURE_DETAILS_UNKNOWN"

_OPEN_DIRECTION: Final[dict[Action, Direction]] = {
    Action.OPEN_LONG: Direction.LONG,
    Action.FLIP_LONG: Direction.LONG,
    Action.OPEN_SHORT: Direction.SHORT,
    Action.FLIP_SHORT: Direction.SHORT,
}
_CLOSE_ACTIONS: Final = frozenset({Action.CLOSE, Action.FLIP_LONG, Action.FLIP_SHORT})

# Exception policy (§10.5)
_FATAL_ERRORS: Final = (ConfigError, AuthError, IpBannedError)
_SKIP_ERRORS: Final = (TransientError, TimestampError, StaleDataError, requests.RequestException)


# ---------------------------------------------------------------------------------------------
# Persisted state keys (§5.2)
# ---------------------------------------------------------------------------------------------


def _mode_text(mode: Mode | str) -> str:
    return Mode(mode).value


def active_trade_key(mode: Mode | str, symbol: str) -> str:
    return f"active_trade:{_mode_text(mode)}:{symbol}"


def last_bar_key(mode: Mode | str, symbol: str, interval: str) -> str:
    return f"last_bar:{_mode_text(mode)}:{symbol}:{interval}"


def kill_switch_key(mode: Mode | str, symbol: str) -> str:
    return f"kill_switch:{_mode_text(mode)}:{symbol}"


def cooldown_key(mode: Mode | str, symbol: str) -> str:
    return f"cooldown:{_mode_text(mode)}:{symbol}"


def halted_key(mode: Mode | str, symbol: str) -> str:
    return f"halted:{_mode_text(mode)}:{symbol}"


def emergency_key(mode: Mode | str, symbol: str) -> str:
    return f"emergency:{_mode_text(mode)}:{symbol}"


# ---------------------------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class IterationReport:
    server_time: int
    bar_open_time: int | None  # last closed bar processed (None if data stale)
    signal: SignalAction | None
    action: Action
    executed: bool
    skipped_reason: str | None  # "already_processed", "stale_data", "entries_blocked:<why>", "risk:<code>", ...
    closure: PositionClosure | None
    errors: list[str] = field(default_factory=list)


class SingleInstanceLock:
    """Exclusive, non-blocking lock on ONE global file (``data/trader.lock``): one trader process at a time.

    The file is never written to and never deleted. On Windows ``msvcrt.locking`` locks 1 byte at offset 0;
    elsewhere ``fcntl.flock``. NEVER probe other processes with ``os.kill(pid, 0)`` (it terminates on Windows).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def __enter__(self) -> SingleInstanceLock:
        if self._fd is not None:
            raise BotError(f"lock already held by this object ({self.path})")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # never "w" (truncates / PermissionError while locked), never append/text mode
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            os.lseek(fd, 0, os.SEEK_SET)  # msvcrt locks start at the CURRENT position
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise BotError(
                f"another trader instance is running ({self.path}) / 다른 트레이더 프로세스가 이미 실행 중입니다"
            ) from None
        self._fd = fd
        logger.debug("acquired trader lock %s", self.path)
        return self

    def __exit__(self, *exc: object) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc_unlock:
            logger.warning("could not unlock %s cleanly: %s", self.path, exc_unlock)
        finally:
            os.close(fd)  # closing the descriptor releases the lock in any case; the file is kept


def live_banner(cfg: AppConfig) -> str:
    """The CRITICAL banner logged before a live start (English + Korean)."""
    width = 63
    border = "#" * width

    def line(text: str) -> str:
        return f"#  {text}".ljust(width - 1) + "#"

    return "\n".join(
        [
            border,
            "#  LIVE TRADING ON BINANCE MAINNET - REAL MONEY AT RISK        #",
            "#  실거래 모드: 실제 자금이 사용됩니다                          #",
            line(
                f"symbol={cfg.symbol} interval={cfg.interval} leverage={cfg.risk.leverage} "
                f"risk={cfg.risk.risk_per_trade_pct:g}%/trade"
            ),
            "#  Press Ctrl+C within 10 seconds to abort / 10초 안에 Ctrl+C   #",
            border,
        ]
    )


# ---------------------------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------------------------


class Trader:
    """Runs the strategy on closed candles and keeps the broker state, protection and storage consistent."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        broker: Broker,
        market: MarketData,
        strategy: Strategy,
        storage: Storage,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.market = market
        self.strategy = strategy
        self.storage = storage
        self.clock = clock
        self.sleep = sleep

        self.mode: Mode = Mode(cfg.mode)
        self.symbol: str = cfg.symbol
        self.interval: str = cfg.interval
        self.interval_ms: int = interval_to_ms(cfg.interval)
        self.bot_id: str = cfg.execution.bot_id

        broker_mode = getattr(broker, "mode", None)
        if broker_mode is not None and Mode(broker_mode) is not self.mode:
            raise ConfigError(f"broker mode {Mode(broker_mode).value} does not match config mode {self.mode.value}")
        client = getattr(market, "client", None)
        if self.mode is Mode.PAPER and bool(getattr(client, "has_credentials", False)):
            raise ConfigError("paper mode must use a market data client WITHOUT API credentials (paper never signs)")

        self.stopped_before_start: bool = False
        self._stop_event = threading.Event()

        # normative trader state (§10.1)
        self.active: ActiveTrade | None = None
        self.kill = DailyLossKillSwitch(cfg.risk.max_daily_loss_pct)
        self.cooldown = Cooldown(cfg.risk.cooldown_bars_after_stop, self.interval_ms)
        self.last_bar: int | None = None
        self.halted: dict[str, Any] | None = None
        self.emergency: dict[str, Any] | None = None
        self.effective_limit: int = int(cfg.execution.kline_limit)
        self.filters: SymbolFilters | None = None

        # status / bookkeeping
        self.state: BotState = BotState.STARTING
        self.message: str = ""
        self.started_at: int = now_ms(clock)
        self.last_signal: Signal | None = None
        self.last_account: AccountSnapshot | None = None
        self.last_report: IterationReport | None = None
        self.entries_blocked_reason: str | None = None
        self.consecutive_errors: int = 0
        self._startup_complete = False
        self._iteration_bar: int | None = None  # bar_for_ids of the current / last iteration
        self._iteration_closures: list[PositionClosure] = []
        self._warned_max_notional: set[float] = set()
        self._foreign_orders_reported = False
        self._owned_resources: list[Any] = []  # closed by close() (build_trader registers the REST client)

    # ------------------------------------------------------------------------------------------ keys

    @property
    def _key_active(self) -> str:
        return active_trade_key(self.mode, self.symbol)

    @property
    def _key_last_bar(self) -> str:
        return last_bar_key(self.mode, self.symbol, self.interval)

    @property
    def _key_kill(self) -> str:
        return kill_switch_key(self.mode, self.symbol)

    @property
    def _key_cooldown(self) -> str:
        return cooldown_key(self.mode, self.symbol)

    @property
    def _key_halted(self) -> str:
        return halted_key(self.mode, self.symbol)

    @property
    def _key_emergency(self) -> str:
        return emergency_key(self.mode, self.symbol)

    # ------------------------------------------------------------------------------------------ lifecycle

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def request_stop(self) -> None:
        """Set the stop flag (signal-handler safe). Checked between iterations and every <= 1 s while waiting."""
        self._stop_event.set()

    def close(self) -> None:
        """Release resources this trader owns (the REST client created by ``build_trader``)."""
        while self._owned_resources:
            resource = self._owned_resources.pop()
            try:
                resource.close()
            except Exception:  # closing must never mask the real outcome
                logger.debug("error while closing %r", resource, exc_info=True)

    def __enter__(self) -> Trader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------------------------------ startup

    def startup(self) -> None:
        """§10.2: status STARTING, live countdown, prepare the symbol, load state, reconcile, seed the kill switch."""
        self._startup_complete = False
        self.stopped_before_start = False
        self.consecutive_errors = 0
        self.started_at = now_ms(self.clock)
        self._write_status(BotState.STARTING, f"starting {self.mode.value} {self.symbol} {self.interval}")

        if self.mode is Mode.LIVE:
            logger.critical("\n%s", live_banner(self.cfg))
            for remaining in range(LIVE_COUNTDOWN_SEC, 0, -1):
                logger.warning("live trading starts in %d s (Ctrl+C to abort / 중단하려면 Ctrl+C)", remaining)
                self.sleep(1)
                if self.stop_requested:
                    self.stopped_before_start = True
                    logger.warning("live start aborted by the user; nothing was sent to the exchange")
                    return

        self.effective_limit = self._compute_effective_limit()
        self.filters = self.broker.prepare_symbol(self.symbol, self.cfg.risk.leverage)

        self._load_state()
        self.storage.delete_state(self._key_halted)  # a restart clears a halt
        self.halted = None

        closed, _forming, server_now = self.market.recent_klines(self.symbol, self.interval, self.effective_limit)
        self._iteration_bar = (
            int(closed["open_time"].iloc[-1])
            if not closed.empty
            else expected_last_closed_open(server_now, self.interval_ms)
        )
        atr_last = self._atr_last(closed)
        self._iteration_closures = []
        result = self.broker.sync(self.symbol, self.active, candles_from_df(closed))
        account = result.account
        if self._handle_sync(result, atr_last):
            account = self._resync(atr_last)

        self.kill.seed(account.equity)  # no-op if a persisted last_equity exists
        self._save_kill()
        self.last_account = account
        self.entries_blocked_reason = self._entries_blocked_reason(self._iteration_bar)
        self._startup_complete = True
        if self.stop_requested:
            self.stopped_before_start = True
        logger.info(
            "trader started: mode=%s symbol=%s interval=%s strategy=%s klines=%d equity=%.2f position=%s",
            self.mode.value,
            self.symbol,
            self.interval,
            self._strategy_label(),
            self.effective_limit,
            account.equity,
            "none" if account.position is None else f"{account.position.qty:g}@{account.position.entry_price:g}",
        )
        self._write_status(None, "running" if self.emergency is None else "emergency flatten pending")

    def _compute_effective_limit(self) -> int:
        warm = int(self.strategy.warmup_bars)
        sl = self.cfg.risk.stop_loss
        kline_limit = int(self.cfg.execution.kline_limit)
        limit = max(kline_limit, 2 * warm, warm + 2, 3 * int(sl.atr_period) if sl.mode == "atr" else 0)
        if limit > MAX_KLINES:
            raise ConfigError(
                f"strategy warmup too long for {MAX_KLINES} klines (needs {limit}: warmup_bars={warm}, "
                f"stop_loss={sl.mode}/{sl.atr_period})"
            )
        if limit > kline_limit:
            logger.info(
                "kline window raised from execution.kline_limit=%d to %d (2 x strategy warmup %d) so live and "
                "backtest indicators agree",
                kline_limit,
                limit,
                warm,
            )
        return limit

    def _load_state(self) -> None:
        st = self.storage
        self.active = None
        raw = st.get_state(self._key_active)
        if raw is not None:
            try:
                if not isinstance(raw, Mapping):
                    raise TypeError(f"expected an object, got {type(raw).__name__}")
                self.active = ActiveTrade.from_dict(raw)
            except (KeyError, TypeError, ValueError) as exc:
                # Conservative: drop the unreadable record; a position that still exists is adopted (and protected)
                # by the reconcile sync right after.
                self._event("ERROR", "STATE_CORRUPT", f"unreadable active trade state dropped ({exc!r})")
                st.delete_state(self._key_active)

        lb = st.get_state(self._key_last_bar)
        try:
            self.last_bar = None if lb is None else int(lb)
        except (TypeError, ValueError):
            self.last_bar = None

        ks = st.get_state(self._key_kill)
        self.kill = (
            DailyLossKillSwitch.from_dict(ks, self.cfg.risk.max_daily_loss_pct)
            if isinstance(ks, Mapping)
            else DailyLossKillSwitch(self.cfg.risk.max_daily_loss_pct)
        )
        cd = st.get_state(self._key_cooldown)
        self.cooldown = (
            Cooldown.from_dict(cd, self.cfg.risk.cooldown_bars_after_stop, self.interval_ms)
            if isinstance(cd, Mapping)
            else Cooldown(self.cfg.risk.cooldown_bars_after_stop, self.interval_ms)
        )

        em = st.get_state(self._key_emergency)
        self.emergency = None
        if isinstance(em, Mapping):
            self.emergency = {
                "attempt": _as_int(em.get("attempt"), 0),
                "bar": _as_int(em.get("bar"), 0),
                "since": _as_int(em.get("since"), now_ms(self.clock)),
            }
            self._event(
                "CRITICAL",
                "EMERGENCY_PENDING",
                f"an emergency flatten is pending since {self.emergency['since']} "
                f"(attempt {self.emergency['attempt']}); it is retried first",
            )
        if self.active is not None:
            logger.info(
                "restored active trade %s: %s %g @ %g (stop %g, tp %s, protect_seq %d)",
                self.active.trade_id,
                self.active.direction.value,
                self.active.qty,
                self.active.entry_price,
                self.active.stop_price,
                self.active.take_profit_price,
                self.active.protect_seq,
            )

    # ------------------------------------------------------------------------------------------ iteration

    def run_once(self) -> IterationReport:
        """One iteration over the most recent CLOSED candle (§10.3, exact order)."""
        if self.filters is None:
            raise BotError("Trader.startup() must run before run_once()")
        self._iteration_closures = []
        errors: list[str] = []
        executed = False
        skipped: str | None = None
        signal_action: SignalAction | None = None
        action = Action.NONE

        # 1. klines (also re-syncs the clock)
        closed, forming, server_now = self.market.recent_klines(self.symbol, self.interval, self.effective_limit)

        # 2. staleness
        expected = expected_last_closed_open(server_now, self.interval_ms)
        retries = 0
        while self._behind(closed, expected) and retries < STALE_RETRIES:
            retries += 1
            logger.info(
                "klines not up to date (last closed %s, expected %s); retry %d/%d in %.0f s",
                "none" if closed.empty else ms_to_iso(int(closed["open_time"].iloc[-1])),
                ms_to_iso(expected),
                retries,
                STALE_RETRIES,
                STALE_RETRY_SLEEP_SEC,
            )
            self.sleep(STALE_RETRY_SLEEP_SEC)
            closed, forming, server_now = self.market.recent_klines(self.symbol, self.interval, self.effective_limit)
            expected = expected_last_closed_open(server_now, self.interval_ms)
        stale_reason: str | None = None
        warm = int(self.strategy.warmup_bars)
        if self._behind(closed, expected):
            stale_reason = (
                "no closed candles received"
                if closed.empty
                else f"last closed bar {ms_to_iso(int(closed['open_time'].iloc[-1]))} is older than the expected "
                f"{ms_to_iso(expected)}"
            )
        elif len(closed) < warm + 1:
            stale_reason = f"only {len(closed)} closed candles; the strategy needs {warm + 1}"
        if stale_reason is not None:
            self._event("WARNING", "STALE_DATA", f"{stale_reason}; no new signals or orders this iteration")

        # 3. candles for the dashboard chart
        if not closed.empty:
            self.storage.upsert_candles(self.symbol, self.interval, closed.tail(CANDLES_TO_STORE))

        # 4. iteration constants (defined BEFORE any order can be sent)
        bar_for_ids = int(closed["open_time"].iloc[-1]) if not closed.empty else int(expected)
        bar = bar_for_ids
        self._iteration_bar = bar_for_ids
        entry_bar_time = bar_for_ids + self.interval_ms
        ref_cache: list[float] = []

        def ref() -> float:  # lazy, at most once per iteration
            if not ref_cache:
                ref_cache.append(self._ref_price(forming, entry_bar_time))
            return ref_cache[0]

        atr_last = self._atr_last(closed)

        # 5. broker sync
        result = self.broker.sync(self.symbol, self.active, candles_from_df(closed))
        account = result.account
        if self._handle_sync(result, atr_last):
            account = self._resync(atr_last)

        # 6. daily-loss kill switch on the processed bar's CLOSE time (identical to the engine's t_close)
        newly = self.kill.update(bar_for_ids + self.interval_ms - 1, account.equity)
        self._save_kill()
        if newly:
            self._event(
                "CRITICAL",
                "KILL_SWITCH",
                f"daily loss kill switch tripped: {self.kill.reason}; new entries blocked until 00:00 UTC (09:00 KST)",
            )
        if self.kill.tripped and self.cfg.risk.kill_switch_flatten and account.position is not None:
            ks_id = make_client_id(self.bot_id, self.symbol, "KS", bar_for_ids)
            logger.warning("kill switch active: flattening the %s position (%s)", self.symbol, ks_id)
            closure = self.broker.close_position(
                self.symbol,
                self.active,
                reason=ExitReason.KILL_SWITCH,
                client_id=ks_id,
                ref_price=ref(),
                bar_time=entry_bar_time,
            )
            executed = True
            if closure is not None:
                self._record_closure(closure)
            account = self._resync(atr_last)

        # 7.-12. decisions
        if stale_reason is not None:
            skipped = "stale_data"
        elif self.last_bar is not None and bar == self.last_bar:
            skipped = "already_processed"
            logger.info("bar %s already processed; nothing to decide", ms_to_iso(bar))
        else:
            try:
                # 9. signal + entries gate
                sig = self.strategy.generate(closed)
                self.last_signal = sig
                signal_action = sig.action
                blocked = self._entries_blocked_reason(bar)
                position_dir = account.position.direction if account.position is not None else Direction.FLAT
                # 10. action
                action = Action(decide_action(sig.action, position_dir, blocked is None))
                self.storage.record_signal(
                    self.mode.value, self.symbol, self.interval, sig, action, now_ms(self.clock)
                )
                logger.info(
                    "last closed bar %s close=%.8g: signal %s (%s) -> decision %s%s",
                    ms_to_iso(bar),
                    float(closed["close"].iloc[-1]),
                    sig.action.value,
                    sig.reason,
                    action.value,
                    f" (entries blocked: {blocked})" if blocked else "",
                )
                if blocked is not None and Action(decide_action(sig.action, position_dir, True)) in _OPEN_DIRECTION:
                    skipped = f"entries_blocked:{blocked}"
                # 11. execute
                if action is not Action.NONE:
                    done, exec_skip = self._execute(
                        action,
                        bar=bar,
                        entry_bar_time=entry_bar_time,
                        ref=ref,
                        atr_last=atr_last,
                        account=account,
                        errors=errors,
                    )
                    executed = executed or done
                    if exec_skip is not None:
                        skipped = exec_skip
            finally:
                # 12. even if execution failed: a missed signal is safer than repeated attempts
                self.last_bar = bar
                self._save_last_bar()

        # 13./14. post-execution re-read, equity and status
        if executed or self._iteration_closures:
            account = self._resync(atr_last)
        self.last_account = account
        self.entries_blocked_reason = self._entries_blocked_reason(bar)
        self.storage.append_equity(self.mode.value, bar_for_ids, account.equity, account.wallet_balance)
        report = IterationReport(
            server_time=int(server_now),
            bar_open_time=None if stale_reason is not None else bar,
            signal=signal_action,
            action=action,
            executed=executed,
            skipped_reason=skipped,
            closure=self._iteration_closures[-1] if self._iteration_closures else None,
            errors=errors,
        )
        summary = f"bar {ms_to_iso(bar)}: {signal_action.value if signal_action else '-'} -> {action.value}"
        if skipped:
            summary += f" ({skipped})"
        self._write_status(None, summary)
        self.last_report = report
        return report

    def _execute(
        self,
        action: Action,
        *,
        bar: int,
        entry_bar_time: int,
        ref: Callable[[], float],
        atr_last: float | None,
        account: AccountSnapshot,
        errors: list[str],
    ) -> tuple[bool, str | None]:
        """§10.3 step 11. Returns ``(anything sent to the broker, skipped_reason)``."""
        executed = False
        if action in _CLOSE_ACTIONS:
            reason = ExitReason.SIGNAL if action is Action.CLOSE else ExitReason.FLIP
            ex_id = make_client_id(self.bot_id, self.symbol, "EX", bar)
            logger.info("closing the %s position (%s, %s)", self.symbol, reason.value, ex_id)
            closure = self.broker.close_position(
                self.symbol, self.active, reason=reason, client_id=ex_id, ref_price=ref(), bar_time=entry_bar_time
            )
            executed = True
            if closure is not None:
                self._record_closure(closure)
            else:
                logger.info("close: the %s position was already flat", self.symbol)
            if action is Action.CLOSE:
                return executed, None
            # FLIP: verify flat before opening the reverse position. (self.active is None after a recorded close;
            # passing it lets the broker report a concurrent closure if the close found nothing to close.)
            post = self.broker.sync(self.symbol, self.active, [])
            self._handle_sync(post, atr_last)
            if post.account.position is not None:
                self._event(
                    "WARNING",
                    "FLIP_ABORTED",
                    "position still open after the close leg of the flip; the reverse position is not opened",
                )
                errors.append("flip_aborted")
                return executed, "flip_aborted"
            account = post.account  # the open leg is sized from the post-close account (like the engine)

        direction = _OPEN_DIRECTION[action]
        if self.active is not None or account.position is not None:
            self._event("WARNING", "ENTRY_SKIPPED", f"{direction.value} entry skipped: not flat")
            errors.append("not_flat")
            return executed, "not_flat"
        filters = self.filters
        if filters is None:  # startup() sets it; defensive
            raise BotError("symbol filters are not loaded")
        decision = plan_entry(
            direction=direction,
            ref_price=ref(),
            equity=account.equity,
            atr_value=atr_last,
            filters=filters,
            risk=self._effective_risk(),
            fees=self.cfg.execution.fees,
            slippage_bps=self.cfg.execution.slippage_bps,
        )
        if decision.plan is None:
            self._event("INFO", "ENTRY_REJECTED", f"{direction.value} entry rejected by risk: {decision.reason}")
            return executed, f"risk:{decision.reason}"
        plan = decision.plan
        en_id = make_client_id(self.bot_id, self.symbol, "EN", bar)
        sl_id = make_client_id(self.bot_id, self.symbol, "SL", entry_bar_time, 1)
        tp_id = (
            make_client_id(self.bot_id, self.symbol, "TP", entry_bar_time, 1)
            if plan.take_profit_price is not None
            else None
        )
        logger.info(
            "opening %s %s qty=%s ref=%.8g stop=%s tp=%s (risk %.2f USDT, cap %s, %s)",
            direction.value,
            self.symbol,
            plan.qty,
            plan.ref_price,
            plan.stop_price,
            plan.take_profit_price,
            plan.risk_amount,
            plan.sizing_cap,
            en_id,
        )
        try:
            outcome = self.broker.open_position(
                plan,
                entry_client_id=en_id,
                sl_client_id=sl_id,
                tp_client_id=tp_id,
                ref_price=ref(),
                bar_time=entry_bar_time,
            )
        except ProtectionFailedError as exc:
            if exc.closure is not None and exc.entry is not None and exc.entry.filled:
                self.active = self._active_from_entry(
                    plan, exc.entry, entry_bar_time=entry_bar_time, entry_client_id=en_id
                )
                self._save_active()
                self._record_closure(exc.closure)
            elif exc.closure is None:
                logger.warning("protection failed without a closure to record (the position is already flat)")
            else:
                logger.warning("protection failed; the closure cannot be booked without filled entry details")
            self._halt_after_protection_failure(exc)
            errors.append(f"protection_failed: {exc}")
            return True, "protection_failed"
        except EmergencyError as exc:
            if exc.entry is not None and exc.entry.filled:
                self.active = self._active_from_entry(
                    plan, exc.entry, entry_bar_time=entry_bar_time, entry_client_id=en_id
                )
                self._save_active()  # so the emergency flatten books a proper trade
            self.emergency = {"attempt": 0, "bar": int(entry_bar_time), "since": now_ms(self.clock)}
            self._save_emergency()
            self._event(
                "CRITICAL",
                "EMERGENCY",
                f"entry may be unprotected and could not be flattened: {exc}; retrying the flatten every "
                f"{EMERGENCY_RETRY_SEC:.0f} s",
            )
            self.state = BotState.ERROR
            raise

        executed = True
        if outcome.filled:
            self.active = self._active_from_entry(plan, outcome, entry_bar_time=entry_bar_time, entry_client_id=en_id)
            self._save_active()  # immediately
            if outcome.entry_order is not None:
                self.storage.upsert_order(self.mode.value, outcome.entry_order, now_ms(self.clock))
            self._event(
                "INFO",
                "ENTRY",
                f"opened {direction.value} {outcome.qty:g} {self.symbol} @ {outcome.avg_price:.8g} "
                f"(stop {self.active.stop_price:g}, tp {self.active.take_profit_price}, "
                f"risk {self.active.risk_amount:.2f} USDT, cap {plan.sizing_cap})",
            )
            return executed, None
        if outcome.entry_order is not None:
            self.storage.upsert_order(self.mode.value, outcome.entry_order, now_ms(self.clock))
        self._event("INFO", "ENTRY_NOT_FILLED", f"{direction.value} entry not filled: {outcome.message or '-'}")
        return executed, "entry_not_filled"

    # ------------------------------------------------------------------------------------------ sync handling

    def _resync(self, atr_value: float | None) -> AccountSnapshot:
        """Read-only re-sync (empty candle list); its result goes through ``_handle_sync`` like every other."""
        post = self.broker.sync(self.symbol, self.active, [])
        self._handle_sync(post, atr_value)
        return post.account

    def _handle_sync(self, result: SyncResult, atr_value: float | None) -> bool:
        """The ONE handler for every ``SyncResult`` (§10.1). Returns True if anything was recorded/placed/closed."""
        changed = False
        issues = set(result.issues)
        pos: Position | None = result.account.position

        # 1. closure of the tracked trade (SL/TP/liquidation/manual, or a simulated paper exit)
        if result.closure is not None:
            if self.active is not None:
                self._record_closure(result.closure)
                changed = True
            else:
                c = result.closure
                self._event(
                    "WARNING",
                    "CLOSURE_UNTRACKED",
                    f"broker reported a closure without a tracked trade ({c.reason.value} {c.qty:g} @ {c.exit_price:g})",
                )
        if ISSUE_CLOSURE_DETAILS_UNKNOWN in issues:
            self._event("WARNING", ISSUE_CLOSURE_DETAILS_UNKNOWN, "the position was closed but the details are unknown")
        if ISSUE_ORPHAN_PROTECTIVE_CANCELED in issues:
            self._event("INFO", ISSUE_ORPHAN_PROTECTIVE_CANCELED, "orphan bot protective orders were cancelled")
        if ISSUE_FOREIGN_OPEN_ORDERS in issues:
            if not self._foreign_orders_reported:
                self._event(
                    "WARNING",
                    ISSUE_FOREIGN_OPEN_ORDERS,
                    f"open orders on {self.symbol} that this bot does not own were found (never cancelled)",
                )
                self._foreign_orders_reported = True
        else:
            self._foreign_orders_reported = False

        force_protection = False
        # SPEC-GAP: the exchange position has the opposite direction of the tracked trade (manual intervention).
        # Conservative: book the tracked trade as closed (UNKNOWN), then adopt and protect the real position.
        if pos is not None and self.active is not None and pos.direction is not self.active.direction:
            self._event(
                "CRITICAL",
                "DIRECTION_MISMATCH",
                f"tracked {self.active.direction.value} trade but the exchange position is {pos.direction.value}; "
                "booking the tracked trade as closed (UNKNOWN) and adopting the position",
            )
            self._record_closure(
                PositionClosure(
                    exit_time=self._server_now_ms(),
                    exit_price=self.active.entry_price,
                    qty=self.active.qty,
                    reason=ExitReason.UNKNOWN,
                    exit_fee=0.0,
                    funding=0.0,
                    gross_pnl=None,
                )
            )
            force_protection = True
            changed = True

        # 2. untracked position -> adopt (whether or not the broker saw it with active=None)
        if pos is not None and self.active is None:
            self._adopt(pos, atr_value)
            changed = True

        # 3. quantity drift
        if ISSUE_QTY_MISMATCH in issues and pos is not None and self.active is not None:
            old = self.active.qty
            self.active.qty = abs(float(pos.qty))
            self._save_active()
            self._event("WARNING", ISSUE_QTY_MISMATCH, f"position qty {old:g} -> {self.active.qty:g} (exchange)")
            changed = True

        # 4. missing / too small protection -> place a new generation (place-before-cancel inside the broker)
        if (
            pos is not None
            and self.active is not None
            and (force_protection or issues & {ISSUE_SL_MISSING, ISSUE_PROTECTION_QTY_MISMATCH})
        ):
            if self._ensure_protection(result.account, issues):
                changed = True
        return changed

    def _adopt(self, pos: Position, atr_value: float | None) -> None:
        direction = pos.direction
        entry = float(pos.entry_price)
        sl_cfg = self.cfg.risk.stop_loss
        stop_raw = compute_stop_price(entry, direction, sl_cfg, atr_value)
        if stop_raw is None:  # e.g. ATR unavailable (no candles): fall back to the percent stop
            stop_raw = compute_stop_price(entry, direction, dataclasses.replace(sl_cfg, mode="percent"), None)
        if stop_raw is None:
            raise BotError(f"cannot compute a stop price for the adopted position (entry {entry})")
        stop = self._round_protective(stop_raw, entry)
        tp: float | None = None
        tp_raw = compute_take_profit(entry, stop, direction, self.cfg.risk.take_profit_r)
        if tp_raw is not None and math.isfinite(tp_raw) and tp_raw > 0:
            tp = self._round_protective(tp_raw, entry)
        qty = abs(float(pos.qty))
        updated_at = int(pos.updated_at)
        self.active = ActiveTrade(
            trade_id=f"{self.mode.value}-{self.symbol}-adopted-{self._server_now_ms()}",
            symbol=self.symbol,
            direction=direction,
            qty=qty,
            entry_price=entry,
            entry_time=updated_at,
            entry_bar_open_time=floor_time(updated_at, self.interval_ms),
            stop_price=stop,
            take_profit_price=tp,
            liquidation_price=pos.liquidation_price,
            leverage=pos.leverage or self.cfg.risk.leverage,
            risk_amount=qty * abs(entry - stop),
            entry_fee=0.0,
            entry_client_id="adopted",
            protect_seq=0,
            entry_order_id=None,
        )
        self._save_active()
        self._event(
            "WARNING",
            "ADOPTED_POSITION",
            f"untracked {direction.value} position {qty:g} {self.symbol} @ {entry:g} adopted "
            f"(stop {stop:g}, tp {tp}, leverage {self.active.leverage})",
        )

    def _ensure_protection(self, account: AccountSnapshot, issues: set[str]) -> bool:
        active = self.active
        if active is None:
            return False
        seq = active.protect_seq + 1
        sl_id = make_client_id(self.bot_id, self.symbol, "SL", active.entry_bar_open_time, seq)
        tp_id = (
            make_client_id(self.bot_id, self.symbol, "TP", active.entry_bar_open_time, seq)
            if active.take_profit_price is not None
            else None
        )
        logger.warning(
            "protection check: %s -> ensuring protective orders (generation %d)",
            ", ".join(sorted(issues & {ISSUE_SL_MISSING, ISSUE_PROTECTION_QTY_MISMATCH})) or "forced",
            seq,
        )
        try:
            placed = self.broker.ensure_protection(active, account, sl_client_id=sl_id, tp_client_id=tp_id)
        except ProtectionFailedError as exc:
            if exc.closure is not None:
                self._record_closure(exc.closure)  # reason PROTECTION_FAILED (set by the broker)
            else:
                logger.warning("protection failed without a closure to record (the position is already flat)")
            self._halt_after_protection_failure(exc)
            return True
        new_ids = {sl_id} if tp_id is None else {sl_id, tp_id}
        if any(o.client_id in new_ids for o in placed):
            active.protect_seq = seq
            self._save_active()
            self._event(
                "WARNING",
                "PROTECTION_PLACED",
                f"protective orders placed for {active.trade_id} (generation {seq}: "
                f"{', '.join(o.client_id for o in placed if o.client_id in new_ids)})",
            )
            return True
        return False

    def _halt_after_protection_failure(self, exc: ProtectionFailedError) -> None:
        self._set_halted("protection_failed")
        self._event(
            "CRITICAL",
            "PROTECTION_FAILED",
            f"stop-loss could not be placed (flattened={exc.flattened}): {exc}; new entries halted until restart",
        )
        self.state = BotState.HALTED

    def _record_closure(self, closure: PositionClosure) -> None:
        active = self.active
        if active is None:
            self._event(
                "WARNING",
                "CLOSURE_UNTRACKED",
                f"closure without a tracked trade ({closure.reason.value} {closure.qty:g} @ {closure.exit_price:g})",
            )
            return
        trade = Trade.from_closure(active, closure, source=self.mode.value)
        self.storage.insert_trade(trade)
        if closure.order is not None:
            self.storage.upsert_order(self.mode.value, closure.order, now_ms(self.clock))
        level = "WARNING" if closure.reason in (ExitReason.LIQUIDATION, ExitReason.UNKNOWN) else "INFO"
        r_text = "-" if trade.r_multiple is None else f"{trade.r_multiple:.2f}"
        self._event(
            level,
            "TRADE_CLOSED",
            f"{trade.direction.value} {trade.qty:g} {trade.symbol} closed by {trade.exit_reason.value}: "
            f"entry {trade.entry_price:.8g} exit {trade.exit_price:.8g} net {trade.net_pnl:.4f} USDT (R {r_text})",
        )
        self.active = None
        self.storage.delete_state(self._key_active)
        if closure.reason in (ExitReason.STOP_LOSS, ExitReason.LIQUIDATION):
            self.cooldown.trigger(floor_time(int(closure.exit_time), self.interval_ms))
            self._save_cooldown()
        self._iteration_closures.append(closure)

    def _active_from_entry(
        self, plan: TradePlan, outcome: OpenOutcome, *, entry_bar_time: int, entry_client_id: str
    ) -> ActiveTrade:
        plan_qty = float(plan.qty)
        return ActiveTrade(
            trade_id=f"{self.mode.value}-{self.symbol}-{int(entry_bar_time)}-"
            f"{'L' if plan.direction is Direction.LONG else 'S'}",
            symbol=self.symbol,
            direction=plan.direction,
            qty=outcome.qty,
            entry_price=outcome.avg_price,
            entry_time=outcome.entry_time,
            entry_bar_open_time=int(entry_bar_time),
            stop_price=float(plan.stop_price),
            take_profit_price=None if plan.take_profit_price is None else float(plan.take_profit_price),
            liquidation_price=plan.liquidation_price,
            leverage=plan.leverage,
            risk_amount=plan.risk_amount * outcome.qty / plan_qty if plan_qty > 0 else plan.risk_amount,
            entry_fee=outcome.entry_fee,
            entry_client_id=entry_client_id,
            protect_seq=1,
            entry_order_id=outcome.entry_order.exchange_id if outcome.entry_order is not None else None,
        )

    # ------------------------------------------------------------------------------------------ protection check

    def protection_check(self) -> None:
        """Between-bar check (testnet/live): re-protect a position without a live own SL, record closures."""
        if self.mode is Mode.PAPER:
            return  # paper stops are simulated per candle
        self._iteration_closures = []
        result = self.broker.sync(self.symbol, self.active, [])
        changed = self._handle_sync(result, atr_value=None)
        account = result.account
        if changed:
            account = self._resync(None)
        self.last_account = account
        if changed or result.closure is not None:
            self.entries_blocked_reason = self._entries_blocked_reason(self._iteration_bar or 0)
            self._write_status(None, "protection check acted")

    # ------------------------------------------------------------------------------------------ main loop

    def run_forever(self, *, max_iterations: int | None = None) -> None:
        """§10.4: startup, then one iteration per candle close (+ delay); ``max_iterations=1`` is ``--once``."""
        if max_iterations is not None and int(max_iterations) < 1:
            raise ValueError("max_iterations must be >= 1")
        previous_handlers = self._install_signal_handlers()
        outcome = "user"
        error_text: str | None = None
        try:
            try:
                self.startup()
            except EmergencyError as exc:  # raised by ensure_protection during the reconcile
                self._startup_complete = True
                self._on_loop_emergency(exc)
            if self.stopped_before_start:
                outcome = "aborted"
                return
            iterations = 0
            while not self.stop_requested:
                if self.emergency is not None:
                    self._emergency_step()  # waits EMERGENCY_RETRY_SEC (1 s chunks) after a failed attempt
                    continue
                if max_iterations is not None and iterations >= max_iterations:
                    outcome = "once"
                    break
                self._guarded_iteration()
                iterations += 1
                if self.emergency is not None or (max_iterations is not None and iterations >= max_iterations):
                    continue
                self._wait_for_next_bar()
        except KeyboardInterrupt:
            # handlers could not be installed (non-main thread): treat as a user stop
            logger.warning("interrupted (KeyboardInterrupt); stopping")
            if not self._startup_complete:
                self.stopped_before_start = True
                outcome = "aborted"
        except BaseException as exc:
            outcome = "error"
            error_text = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._restore_signal_handlers(previous_handlers)
            self._write_final_status(outcome, error_text)

    def _guarded_iteration(self) -> None:
        try:
            self.run_once()
        except Exception as exc:
            self._handle_loop_exception(exc)
        else:
            self.consecutive_errors = 0

    def _handle_loop_exception(self, exc: Exception) -> None:
        """Exception policy (§10.5). Fatal errors are re-raised; everything else keeps the loop alive."""
        name = type(exc).__name__
        if isinstance(exc, _FATAL_ERRORS):
            self._event("CRITICAL", "FATAL_ERROR", f"{name}: {exc}; stopping the trader")
            self._safe_status(BotState.ERROR, f"{name}: {exc}")
            raise exc
        if isinstance(exc, EmergencyError):
            self._on_loop_emergency(exc)
            return
        if isinstance(exc, RateLimitError):
            pause = float(exc.retry_after) if exc.retry_after else DEFAULT_RATE_LIMIT_PAUSE_SEC
            self._event("WARNING", "RATE_LIMIT", f"{exc}; pausing {pause:.0f} s")
            self._safe_status(None, f"rate limited; pausing {pause:.0f} s")
            self._sleep_chunks(pause)
            return
        if isinstance(exc, _SKIP_ERRORS):
            self._event("WARNING", "ITERATION_SKIPPED", f"{name}: {exc}")
            self._safe_status(None, f"iteration skipped ({name})")
            return
        if isinstance(exc, ProtectionFailedError):  # normally handled where it is raised
            if exc.closure is not None and self.active is not None:
                self._record_closure(exc.closure)
            self._halt_after_protection_failure(exc)
            self._safe_status(None, f"{name}: {exc}")
            return
        logger.error("unexpected error in the trading loop: %s", exc, exc_info=exc)
        self.consecutive_errors += 1
        self._event("ERROR", "LOOP_ERROR", f"{name}: {exc} ({self.consecutive_errors}/{MAX_CONSECUTIVE_ERRORS})")
        if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS and not (
            self.halted is not None and self.halted.get("reason") == "repeated_errors"
        ):
            self._set_halted("repeated_errors")
            self._event(
                "CRITICAL",
                "HALTED",
                f"{MAX_CONSECUTIVE_ERRORS} consecutive errors: new entries halted until restart "
                "(sync and protection continue)",
            )
        self._safe_status(None, f"{name}: {exc}")

    def _on_loop_emergency(self, exc: EmergencyError) -> None:
        if self.emergency is None:
            bar = self._iteration_bar
            if bar is None:
                bar = floor_time(self._server_now_ms(), self.interval_ms)
            self.emergency = {"attempt": 0, "bar": int(bar), "since": now_ms(self.clock)}
            self._save_emergency()
            self._event(
                "CRITICAL",
                "EMERGENCY",
                f"{exc}; the position may be unprotected: flattening every {EMERGENCY_RETRY_SEC:.0f} s until flat",
            )
        self._safe_status(BotState.ERROR, f"emergency: {exc}")

    def _emergency_step(self) -> None:
        """One emergency flatten attempt with a never-used id (attempt counter); 10 s pause after a failure."""
        em = self.emergency
        if em is None:
            return
        attempt = int(em.get("attempt", 0)) + 1
        em["attempt"] = attempt
        self._save_emergency()
        fl_id = make_client_id(self.bot_id, self.symbol, "FL", int(em.get("bar", 0)), attempt)
        self._iteration_closures = []
        try:
            if self.mode is Mode.PAPER:
                closed, forming, server_now = self.market.recent_klines(
                    self.symbol, self.interval, self.effective_limit
                )
                bar_time = (
                    int(forming.open_time) if forming is not None else floor_time(server_now, self.interval_ms)
                )
                entry_bar = int(closed["open_time"].iloc[-1]) + self.interval_ms if not closed.empty else bar_time
                ref_price: float | None = self._ref_price(forming, entry_bar)
            else:
                ref_price = None
                bar_time = floor_time(self._server_now_ms(), self.interval_ms)
            logger.critical("emergency flatten attempt %d (%s)", attempt, fl_id)
            closure = self.broker.close_position(
                self.symbol,
                self.active,
                reason=ExitReason.PROTECTION_FAILED,
                client_id=fl_id,
                ref_price=ref_price,
                bar_time=bar_time,
            )
        except _FATAL_ERRORS:
            raise
        except Exception as exc:
            self._event(
                "CRITICAL",
                "EMERGENCY_FLATTEN_FAILED",
                f"attempt {attempt} ({fl_id}) failed: {type(exc).__name__}: {exc}; retrying in "
                f"{EMERGENCY_RETRY_SEC:.0f} s",
            )
            self._safe_status(BotState.ERROR, f"emergency flatten attempt {attempt} failed")
            self._sleep_chunks(EMERGENCY_RETRY_SEC)
            return
        if closure is not None:
            self._record_closure(closure)
        self.emergency = None
        self.storage.delete_state(self._key_emergency)
        self._set_halted("emergency_flatten")
        self._event(
            "CRITICAL",
            "EMERGENCY_FLATTENED",
            f"emergency flatten succeeded on attempt {attempt} ({fl_id}); new entries halted until restart",
        )
        self._safe_status(None, "emergency flatten done; entries halted")

    def _wait_for_next_bar(self) -> None:
        """Sleep in <= 1 s chunks until the next candle close + delay; heartbeat and protection checks meanwhile."""
        delay_ms = int(round(float(self.cfg.execution.candle_close_delay_sec) * 1000))
        now = self._server_now_ms()
        # SPEC-GAP: next_close_ms(now - delay) + delay == next_close_ms(now) + delay except when the iteration
        # finished within `delay` after a close; then this wakes for THAT close instead of skipping a whole bar.
        wake = next_close_ms(now - delay_ms, self.interval_ms) + delay_ms
        heartbeat_ms = int(self.cfg.execution.heartbeat_sec) * 1000
        protection_ms = min(int(self.cfg.execution.heartbeat_sec), PROTECTION_CHECK_MAX_SEC) * 1000
        local = now_ms(self.clock)
        last_heartbeat = local
        last_protection = local
        logger.debug("waiting until %s for the next candle", ms_to_iso(wake))
        while now < wake and not self.stop_requested:
            self.sleep(min(1.0, (wake - now) / 1000))
            local = now_ms(self.clock)
            if local - last_heartbeat >= heartbeat_ms:
                last_heartbeat = local
                try:
                    self.storage.touch_heartbeat(local)
                except Exception:
                    logger.exception("heartbeat write failed")
            if self.mode is not Mode.PAPER and local - last_protection >= protection_ms:
                last_protection = local
                try:
                    self.protection_check()
                except Exception as exc:
                    self._handle_loop_exception(exc)  # fatal rows re-raise
                if self.emergency is not None:
                    return
            now = self._server_now_ms()

    def _sleep_chunks(self, seconds: float) -> None:
        remaining = float(seconds)
        while remaining > 0 and not self.stop_requested:
            step = min(1.0, remaining)
            self.sleep(step)
            remaining -= step

    def _install_signal_handlers(self) -> dict[int, Any]:
        if threading.current_thread() is not threading.main_thread():
            return {}

        def handler(signum: int, frame: Any) -> None:
            self.request_stop()  # only sets a flag: never interrupts an order sequence

        previous: dict[int, Any] = {}
        for name in ("SIGINT", "SIGBREAK"):
            signum = getattr(signal_module, name, None)
            if signum is None:
                continue
            try:
                previous[signum] = signal_module.signal(signum, handler)
            except (ValueError, OSError):
                logger.debug("could not install a %s handler", name)
        return previous

    @staticmethod
    def _restore_signal_handlers(previous: Mapping[int, Any]) -> None:
        for signum, old in previous.items():
            try:
                signal_module.signal(signum, old if old is not None else signal_module.SIG_DFL)
            except (ValueError, OSError, TypeError):
                logger.debug("could not restore the handler of signal %s", signum)

    def _write_final_status(self, outcome: str, error_text: str | None) -> None:
        if outcome == "error":
            state, message = BotState.ERROR, error_text or "error"
        elif outcome == "once":
            state, message = BotState.STOPPED, ONCE_MESSAGE
        elif outcome == "aborted":
            state, message = BotState.STOPPED, ABORTED_MESSAGE
        else:
            state, message = BotState.STOPPED, USER_STOP_MESSAGE
        self._safe_status(state, message)
        logger.info("trader stopped: %s", message)

    # ------------------------------------------------------------------------------------------ helpers

    def _behind(self, closed: pd.DataFrame, expected: int) -> bool:
        return closed.empty or int(closed["open_time"].iloc[-1]) < int(expected)

    def _server_now_ms(self) -> int:
        return int(self.market.client.server_time_ms())

    def _ref_price(self, forming: Candle | None, entry_bar_time: int) -> float:
        """The forming candle's open when it is the entry bar, else the current mark price."""
        if forming is not None and int(forming.open_time) == int(entry_bar_time):
            return float(forming.open)
        return float(self.market.mark_price(self.symbol))

    def _atr_last(self, closed: pd.DataFrame) -> float | None:
        sl = self.cfg.risk.stop_loss
        if sl.mode != "atr" or closed.empty:
            return None
        try:
            value = float(indicators.atr(closed, int(sl.atr_period)).iloc[-1])
        except (BotError, ValueError, KeyError, TypeError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    def _round_protective(self, price: float, entry: float) -> float:
        if self.filters is None:
            return float(price)
        return float(round_protective_price(price, self.filters.tick_size, entry=entry))

    def _effective_risk(self) -> RiskConfig:
        """``cfg.risk`` with ``max_position_notional`` capped by the exchange leverage bracket (§10.1)."""
        risk = self.cfg.risk
        mn = self.broker.max_notional(self.symbol)
        if mn is not None and math.isfinite(float(mn)) and 0 < float(mn) < risk.max_position_notional:
            cap = float(mn)
            if cap not in self._warned_max_notional:
                self._warned_max_notional.add(cap)
                logger.warning(
                    "exchange leverage bracket caps the position notional at %.2f USDT (config max_position_notional "
                    "%.2f); sizing uses the lower value",
                    cap,
                    risk.max_position_notional,
                )
            return dataclasses.replace(risk, max_position_notional=cap)
        return risk

    def _entries_blocked_reason(self, bar: int) -> str | None:
        if not self.kill.entries_allowed:
            return "kill_switch"
        if self.cooldown.active(bar):
            return "cooldown"
        if self.cfg.halt_path.exists():
            return "halt_file"
        if self.halted:
            return f"halted:{self.halted.get('reason', 'unknown')}"
        if self.emergency:
            return "emergency"
        return None

    def _current_state(self) -> BotState:
        if self.emergency:
            return BotState.ERROR
        if self.halted:
            return BotState.ERROR if self.halted.get("reason") == "repeated_errors" else BotState.HALTED
        if self.kill.tripped:
            return BotState.KILL_SWITCH
        return BotState.RUNNING

    def _strategy_label(self) -> str:
        try:
            return str(self.strategy.describe())
        except Exception:
            return str(getattr(self.strategy, "name", type(self.strategy).__name__))

    def _write_status(self, state: BotState | None, message: str | None = None) -> None:
        self.state = self._current_state() if state is None else state
        if message is not None:
            self.message = message
        self.storage.upsert_status(
            BotStatus(
                updated_at=now_ms(self.clock),  # LOCAL time (the dashboard computes the heartbeat age locally)
                started_at=self.started_at,
                mode=self.mode,
                symbol=self.symbol,
                interval=self.interval,
                strategy=self._strategy_label(),
                state=self.state,
                message=self.message,
                account=self.last_account,
                last_signal=self.last_signal,
                last_bar_open_time=self.last_bar,
                entries_blocked_reason=self.entries_blocked_reason,
                pid=os.getpid(),
            )
        )

    def _safe_status(self, state: BotState | None, message: str | None = None) -> None:
        try:
            self._write_status(state, message)
        except Exception:
            logger.exception("could not write the bot status")

    def _event(self, level: str, kind: str, message: str) -> None:
        logger.log(logging.getLevelNamesMapping().get(level, logging.INFO), "[%s] %s", kind, message)
        self.storage.log_event(level, self.mode.value, kind, message, ts_ms=now_ms(self.clock))

    def _set_halted(self, reason: str) -> None:
        self.halted = {"reason": reason, "ts": now_ms(self.clock)}
        self.storage.set_state(self._key_halted, self.halted)

    def _save_active(self) -> None:
        if self.active is None:
            self.storage.delete_state(self._key_active)
        else:
            self.storage.set_state(self._key_active, self.active.to_dict())

    def _save_last_bar(self) -> None:
        if self.last_bar is not None:
            self.storage.set_state(self._key_last_bar, int(self.last_bar))

    def _save_kill(self) -> None:
        self.storage.set_state(self._key_kill, self.kill.to_dict())

    def _save_cooldown(self) -> None:
        self.storage.set_state(self._key_cooldown, self.cooldown.to_dict())

    def _save_emergency(self) -> None:
        if self.emergency is None:
            self.storage.delete_state(self._key_emergency)
        else:
            self.storage.set_state(self._key_emergency, dict(self.emergency))


def _as_int(value: Any, default: int) -> int:
    try:
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------------------------


def build_trader(
    cfg: AppConfig,
    storage: Storage,
    *,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> Trader:
    """Wire the mode-specific components. The caller owns ``storage``; call ``Trader.close()`` when done.

    paper:   mainnet public client WITHOUT credentials + PaperBroker (a signed call is impossible by construction)
    testnet: credentials from .env/environment, Demo Trading host, ExchangeBroker(TESTNET)
    live:    ``assert_live_allowed`` first (process env CONFIRM_LIVE_TRADING=YES), mainnet, ExchangeBroker(LIVE)
    """
    mode = Mode(cfg.mode)
    if mode is Mode.LIVE:
        assert_live_allowed(cfg, environ=environ)  # before anything else: no client, no network
    creds = load_credentials(cfg, environ=environ)  # None in paper mode (keys are never read)

    load_strategy_modules(cfg.strategy.extra_modules)
    strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)

    client: BinanceRestClient
    if mode is Mode.PAPER:
        client = BinanceRestClient(
            MAINNET_REST_URL, recv_window_ms=cfg.execution.recv_window_ms, clock=clock, sleep=sleep
        )
    else:
        if creds is None:  # load_credentials raises for testnet/live; defensive
            raise ConfigError(f"{mode.value} mode requires API credentials")
        base_url = TESTNET_REST_URL if mode is Mode.TESTNET else MAINNET_REST_URL
        client = BinanceRestClient(
            base_url,
            creds.api_key,
            creds.api_secret,
            recv_window_ms=cfg.execution.recv_window_ms,
            clock=clock,
            sleep=sleep,
        )
    try:
        market = MarketData(client)
        broker: Broker
        if mode is Mode.PAPER:
            broker = PaperBroker(
                market=market,
                fill_model=FillModel.from_config(cfg.execution),
                storage=storage,
                initial_balance=cfg.paper.initial_balance,
                include_funding=cfg.paper.include_funding,
                clock=clock,
                sleep=sleep,
            )
        else:
            broker = ExchangeBroker(
                mode=mode, client=client, market=market, execution=cfg.execution, clock=clock, sleep=sleep
            )
        trader = Trader(cfg, broker=broker, market=market, strategy=strategy, storage=storage, clock=clock, sleep=sleep)
    except BaseException:
        client.close()
        raise
    trader._owned_resources.append(client)
    logger.info(
        "trader wired: mode=%s host=%s credentials=%s broker=%s",
        mode.value,
        client.base_url,
        "yes" if client.has_credentials else "no",
        type(broker).__name__,
    )
    return trader
