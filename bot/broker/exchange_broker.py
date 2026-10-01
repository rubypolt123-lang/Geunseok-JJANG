"""Exchange broker: Binance USDT-M futures, testnet (Demo Trading) and live (SPEC §9.4, §9.5).

Safety model:
- Entries are MARKET orders with our own ``newClientOrderId``. An outcome that is not definitive (timeout,
  "Unknown error", -1001, 429, duplicate id) is resolved by LOOKUP, never by a blind resend, and every entry ends
  with a position truth check (``GET /fapi/v3/positionRisk``): ``OpenOutcome(filled=False)`` is returned only when
  the exchange confirms that no position resulted.
- Protective stop-loss / take-profit orders go ONLY to the Algo Order service (``POST /fapi/v1/algoOrder`` with
  ``triggerPrice``); ``/fapi/v1/order`` never receives a conditional type and ``stopPrice`` is never sent.
- A position without a stop is flattened immediately (reduce-only market close).
- The broker only ever cancels its OWN orders (client id starts with ``client_id_prefix(bot_id, symbol)``), one by
  one. ``DELETE /fapi/v1/allOpenOrders`` and ``DELETE /fapi/v1/algoOpenOrders`` are never used.
- On mainnet (live) account-wide settings (Hedge mode, multi-assets mode) are never changed.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from bot.broker.base import (
    ISSUE_CLOSURE_DETAILS_UNKNOWN,
    ISSUE_FOREIGN_OPEN_ORDERS,
    ISSUE_ORPHAN_PROTECTIVE_CANCELED,
    ISSUE_PROTECTION_QTY_MISMATCH,
    ISSUE_QTY_MISMATCH,
    ISSUE_SL_MISSING,
    ISSUE_UNTRACKED_POSITION,
    LIVE_PROTECTIVE_STATUSES,
    QTY_EPS,
    Broker,
    covers_position,
    exit_purpose,
    is_own,
    live_own_orders,
    tp_matches_position,
)
from bot.config import MAINNET_REST_URL, TESTNET_REST_URL
from bot.errors import (
    AlgoLimitError,
    AuthError,
    BotError,
    ConfigError,
    DuplicateClientIdError,
    EmergencyError,
    ExchangeError,
    ImmediateTriggerError,
    IpBannedError,
    NoChangeNeededError,
    NoSuchOrderError,
    OrderRejectedError,
    ProtectionFailedError,
    RateLimitError,
    ReduceOnlyRejectedError,
    TimestampError,
    TransientError,
    UnknownOrderStatusError,
)
from bot.exchange.filters import format_decimal, round_protective_price, to_decimal
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
    Side,
    SymbolFilters,
    SyncResult,
    TradePlan,
    client_id_prefix,
    make_client_id,
    next_client_id,
)
from bot.timeutil import DAY_MS, now_ms

if TYPE_CHECKING:
    from bot.config import ExecutionConfig
    from bot.exchange.market import MarketData
    from bot.exchange.rest import BinanceRestClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------------------------

ORDER_PATH: Final = "/fapi/v1/order"
ALGO_ORDER_PATH: Final = "/fapi/v1/algoOrder"
OPEN_ORDERS_PATH: Final = "/fapi/v1/openOrders"
OPEN_ALGO_ORDERS_PATH: Final = "/fapi/v1/openAlgoOrders"
ALL_ALGO_ORDERS_PATH: Final = "/fapi/v1/allAlgoOrders"
USER_TRADES_PATH: Final = "/fapi/v1/userTrades"
INCOME_PATH: Final = "/fapi/v1/income"
POSITION_RISK_PATH: Final = "/fapi/v3/positionRisk"
ACCOUNT_PATH: Final = "/fapi/v3/account"
ACCOUNT_CONFIG_PATH: Final = "/fapi/v1/accountConfig"
SYMBOL_CONFIG_PATH: Final = "/fapi/v1/symbolConfig"
POSITION_MODE_PATH: Final = "/fapi/v1/positionSide/dual"
MULTI_ASSETS_PATH: Final = "/fapi/v1/multiAssetsMargin"
MARGIN_TYPE_PATH: Final = "/fapi/v1/marginType"
LEVERAGE_PATH: Final = "/fapi/v1/leverage"

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

TERMINAL_ORDER_STATUSES: Final[frozenset[str]] = frozenset(
    {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
)
# §9.5: statuses that confirm a placed algo order (TRIGGERED/FINISHED = it already fired).
ALGO_OK_STATUSES: Final[frozenset[str]] = frozenset({"NEW", "TRIGGERING", "TRIGGERED", "FINISHED"})

AWAIT_TERMINAL_POLLS: Final = 5
AWAIT_TERMINAL_SLEEP_SEC: Final = 1.0
ORDER_LOOKUP_ATTEMPTS: Final = 3
ORDER_LOOKUP_SLEEP_SEC: Final = 1.0
ALGO_LOOKUP_ATTEMPTS: Final = 3
ALGO_LOOKUP_SLEEP_SEC: Final = 0.5
MAX_FRESH_ID_STEPS: Final = 5
MAX_CLOSE_ORDERS: Final = 3
RATE_LIMIT_MAX_WAIT_SEC: Final = 10.0
USER_TRADES_LIMIT: Final = 1000
INCOME_PAGE_LIMIT: Final = 1000
HISTORY_WINDOW_MS: Final = 7 * DAY_MS - 60_000  # userTrades / allAlgoOrders windows are at most 7 days
CLOSE_FALLBACK_LOOKBACK_MS: Final = 60_000

HEDGE_MODE_BLOCK_CODES: Final[frozenset[int]] = frozenset({-4067, -4068, -4531})
MARGIN_TYPE_BLOCK_CODES: Final[frozenset[int]] = frozenset({-4047, -4048})
LEVERAGE_INVALID_CODE: Final = -4028
LEVERAGE_REDUCTION_BLOCKED_CODE: Final = -4161
LIQUIDATION_CLIENT_ID_PREFIX: Final = "autoclose-"
QUOTE_ASSET: Final = "USDT"

_ALGO_TYPE_TO_KIND: Final[dict[str, OrderPurpose]] = {
    "STOP_MARKET": OrderPurpose.STOP_LOSS,
    "TAKE_PROFIT_MARKET": OrderPurpose.TAKE_PROFIT,
}
_KIND_TO_ALGO_TYPE: Final[dict[OrderPurpose, str]] = {
    OrderPurpose.STOP_LOSS: "STOP_MARKET",
    OrderPurpose.TAKE_PROFIT: "TAKE_PROFIT_MARKET",
}
_ALGO_KIND_TO_EXIT: Final[dict[OrderPurpose, ExitReason]] = {
    OrderPurpose.STOP_LOSS: ExitReason.STOP_LOSS,
    OrderPurpose.TAKE_PROFIT: ExitReason.TAKE_PROFIT,
}
# Exit reason implied by the kind tag of one of OUR client ids ("<bot>-<tag>-<KIND>-<bar>-<seq>").
_OWN_KIND_TO_EXIT: Final[dict[str, ExitReason]] = {
    "SL": ExitReason.STOP_LOSS,
    "TP": ExitReason.TAKE_PROFIT,
    "EX": ExitReason.SIGNAL,
    "FL": ExitReason.PROTECTION_FAILED,
    "KS": ExitReason.KILL_SWITCH,
}


# ---------------------------------------------------------------------------------------------
# Small parsing helpers (exchange JSON -> native values)
# ---------------------------------------------------------------------------------------------


def _f(value: Any, default: float = 0.0) -> float:
    """Exchange number (str/int/float) -> finite float, ``default`` when missing/invalid."""
    if value is None or isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _opt_f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    result = _f(value, math.nan)
    return None if math.isnan(result) else result


def _int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool) or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _dec(value: Any) -> Decimal:
    """Exchange quantity string -> Decimal (0 when missing/invalid)."""
    if value is None or value == "" or isinstance(value, bool):
        return Decimal("0")
    try:
        return to_decimal(value if isinstance(value, (Decimal, int, float)) else str(value))
    except (TypeError, ValueError):
        return Decimal("0")


def _truthy(value: Any) -> bool:
    """Binance booleans arrive as JSON bools, sometimes as "true"/"false" strings."""
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _rows(payload: Any) -> list[dict[str, Any]]:
    """A JSON list of objects (or one object, or ``{"orders": [...]}``) -> list of dicts."""
    if isinstance(payload, Mapping):
        for key in ("orders", "rows", "data"):
            inner = payload.get(key)
            if isinstance(inner, list):
                payload = inner
                break
        else:
            return [dict(payload)] if payload else []
    if isinstance(payload, list):
        return [dict(x) for x in payload if isinstance(x, Mapping)]
    return []


def _status(order: Mapping[str, Any] | None) -> str:
    return "" if order is None else str(order.get("status") or "")


def _is_terminal(order: Mapping[str, Any] | None) -> bool:
    return order is not None and _status(order) in TERMINAL_ORDER_STATUSES


def _executed(order: Mapping[str, Any] | None) -> float:
    return 0.0 if order is None else _f(order.get("executedQty"))


def _order_status(value: Any) -> OrderStatus:
    try:
        return OrderStatus(str(value))
    except ValueError:
        return OrderStatus.UNKNOWN


def _err_text(exc: BaseException) -> str:
    if isinstance(exc, ExchangeError):
        return f"{exc.code} {exc.msg}"
    return f"{type(exc).__name__}: {exc}"


def _is_fatal(exc: BaseException) -> bool:
    """Errors that make every further request pointless (keys/IP)."""
    return isinstance(exc, (AuthError, IpBannedError))


def _non_definitive(exc: BaseException) -> bool:
    """§9.4: an order POST whose outcome is unknown -> resolve by lookup, never blind-resend."""
    if isinstance(exc, IpBannedError):
        return False
    return isinstance(exc, (UnknownOrderStatusError, TransientError, RateLimitError, DuplicateClientIdError))


def _kind_of_own_id(client_id: str, prefix: str) -> str | None:
    """"mab1-4314-SL-1790769600-2" -> "SL" (None if the id is not ours / has no kind tag)."""
    if not is_own(client_id, prefix):
        return None
    rest = client_id[len(prefix):]
    kind = rest.split("-", 1)[0]
    return kind or None


@dataclass(frozen=True, slots=True)
class _OpenOrderBook:
    """Raw open orders of one symbol as last read (own/foreign split is derived from the client ids)."""

    algo: tuple[dict[str, Any], ...]
    regular: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _Fills:
    qty: float
    price: float
    fee: float
    realized_pnl: float
    last_time: int


# ---------------------------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------------------------


class ExchangeBroker(Broker):
    """Binance USDT-M futures broker (testnet = Demo Trading, live = mainnet)."""

    def __init__(
        self,
        *,
        mode: Mode,
        client: BinanceRestClient,
        market: MarketData,
        execution: ExecutionConfig,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            mode = Mode(mode)
        except ValueError:
            raise ConfigError(f"ExchangeBroker: unknown mode {mode!r}") from None
        if mode not in (Mode.TESTNET, Mode.LIVE):
            raise ConfigError(f"ExchangeBroker supports only testnet/live modes, not {mode.value}")
        if not bool(getattr(client, "has_credentials", False)):
            raise ConfigError(f"{mode.value} mode requires API credentials (none configured)")
        expected = TESTNET_REST_URL if mode is Mode.TESTNET else MAINNET_REST_URL
        actual = str(getattr(client, "base_url", "")).rstrip("/")
        if actual != expected:
            raise ConfigError(f"host/mode mismatch: {mode.value} mode must use {expected}, client uses {actual}")
        market_client = getattr(market, "client", None)
        market_host = getattr(market_client, "base_url", None)
        if market_host is not None and str(market_host).rstrip("/") != expected:
            raise ConfigError(
                f"host/mode mismatch: market data must come from {expected} in {mode.value} mode, not {market_host}"
            )
        self.mode = mode
        self.client = client
        self.market = market
        self.execution = execution
        self.bot_id: str = str(execution.bot_id)
        self.clock = clock
        self.sleep = sleep
        self._leverage: dict[str, int] = {}
        self._desired_leverage: dict[str, int] = {}
        self._max_notional: dict[str, float] = {}
        self._filters: dict[str, SymbolFilters] = {}
        self._open_orders: dict[str, _OpenOrderBook] = {}

    # ------------------------------------------------------------------------------------------ basics

    def _signed(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.client.signed_request(method, path, params)  # type: ignore[arg-type]

    def _prefix(self, symbol: str) -> str:
        return client_id_prefix(self.bot_id, symbol)

    def _server_now(self) -> int:
        return int(self.client.server_time_ms())

    @property
    def _reduce_only_mode(self) -> bool:
        return str(self.execution.protective_mode) == "reduce_only"

    def _symbol_filters(self, symbol: str) -> SymbolFilters:
        cached = self._filters.get(symbol)
        if cached is not None:
            return cached
        filters = self.market.symbol_filters(symbol)
        self._filters[symbol] = filters
        return filters

    def _rate_limit_pause(self, exc: BaseException) -> None:
        """§9.4: after a 429 on an order POST wait ``min(retry_after or 1, 10)`` s before resolving."""
        if isinstance(exc, RateLimitError) and not isinstance(exc, IpBannedError):
            wait = exc.retry_after if exc.retry_after else 1.0
            self.sleep(min(float(wait), RATE_LIMIT_MAX_WAIT_SEC))

    def max_notional(self, symbol: str) -> float | None:
        """Cached ``maxNotionalValue`` of the current leverage bracket (None before prepare_symbol)."""
        return self._max_notional.get(symbol)

    # ------------------------------------------------------------------------------------------ prepare_symbol

    def prepare_symbol(self, symbol: str, leverage: int) -> SymbolFilters:
        """Validate the symbol and put the account into One-way / single-asset / ISOLATED / ``leverage``."""
        lev = int(leverage)
        if lev < 1:
            raise ConfigError(f"leverage must be >= 1, got {leverage!r}")
        live = self.mode is Mode.LIVE

        # 1. clock + filters of the SAME host
        self.client.sync_time()
        filters = self.market.symbol_filters(symbol)
        if filters.status != "TRADING" or filters.contract_type != "PERPETUAL":
            raise ConfigError(
                f"{symbol} is not a tradable perpetual contract (status {filters.status}, "
                f"contract {filters.contract_type})"
            )
        self._filters[symbol] = filters

        # 2. account-wide modes
        cfg = self._signed("GET", ACCOUNT_CONFIG_PATH)
        cfg = cfg if isinstance(cfg, Mapping) else {}
        dual = _truthy(cfg.get("dualSidePosition"))
        multi = _truthy(cfg.get("multiAssetsMargin"))

        # 3. position mode: One-way required
        if dual:
            if live:
                raise ConfigError(
                    "account is in Hedge mode. 바이낸스 선물 설정에서 포지션 모드를 단방향(One-way)으로 직접 바꾼 뒤 "
                    "다시 실행하세요 (UM/CM 모두 적용됨)"
                )
            try:
                self._signed("POST", POSITION_MODE_PATH, {"dualSidePosition": False})
                logger.info("switched the testnet account to One-way position mode")
            except NoChangeNeededError:
                pass
            except ExchangeError as exc:
                if exc.code in HEDGE_MODE_BLOCK_CODES:
                    raise ConfigError(
                        "account is in Hedge mode; close all UM/CM positions/orders and switch to One-way manually "
                        f"({exc.code} {exc.msg})"
                    ) from None
                raise

        # 4. single-asset mode (isolated margin requires it)
        if multi:
            if live:
                raise ConfigError(
                    "multi-assets mode is on. 바이낸스 선물 설정에서 멀티에셋 모드를 끄고(단일 자산 모드) 다시 실행하세요"
                )
            try:
                self._signed("POST", MULTI_ASSETS_PATH, {"multiAssetsMargin": False})
                logger.info("switched the testnet account to single-asset margin mode")
            except NoChangeNeededError:
                pass
            except (AuthError, IpBannedError, TransientError, RateLimitError, TimestampError):
                raise
            except ExchangeError as exc:
                raise ConfigError(
                    "cannot switch off multi-assets mode; isolated margin requires single-asset mode "
                    f"({exc.code} {exc.msg})"
                ) from None

        # 5. symbol configuration + current position
        sc = self._symbol_config(symbol)
        pos = self._position(symbol)

        # 6. ISOLATED margin
        if str(sc.get("marginType") or "").upper() != "ISOLATED":
            if pos is not None:
                raise ConfigError(
                    f"{symbol}: cannot switch to ISOLATED while a position exists; close it manually"
                )
            try:
                self._signed("POST", MARGIN_TYPE_PATH, {"symbol": symbol, "marginType": "ISOLATED"})
                logger.info("%s margin type set to ISOLATED", symbol)
            except NoChangeNeededError:
                pass
            except ExchangeError as exc:
                if exc.code in MARGIN_TYPE_BLOCK_CODES:
                    raise ConfigError(
                        f"{symbol}: cannot switch to ISOLATED while orders/positions exist ({exc.code} {exc.msg})"
                    ) from None
                raise

        # 7. leverage (never reduced under an open position)
        sc_leverage = _int(sc.get("leverage"))
        if pos is not None and sc_leverage != lev:
            self._keep_exchange_leverage(symbol, sc, lev)
        else:
            try:
                self._apply_leverage(symbol, lev)
            except ExchangeError as exc:
                if exc.code == LEVERAGE_INVALID_CODE:
                    raise ConfigError(f"{symbol}: invalid leverage {lev} ({exc.code} {exc.msg})") from None
                if exc.code == LEVERAGE_REDUCTION_BLOCKED_CODE:
                    # race: a position appeared between the reads -> keep the exchange leverage
                    self._keep_exchange_leverage(symbol, sc, lev)
                else:
                    raise
        self._desired_leverage[symbol] = lev

        # 8. verify
        check = self._symbol_config(symbol)
        margin_type = str(check.get("marginType") or "").upper()
        check_leverage = _int(check.get("leverage"))
        if margin_type != "ISOLATED" or check_leverage != self._leverage.get(symbol):
            raise ConfigError(
                f"{symbol}: symbol configuration not applied (marginType {margin_type or '?'}, leverage "
                f"{check_leverage}, expected ISOLATED / {self._leverage.get(symbol)})"
            )
        logger.info(
            "%s prepared on %s: ISOLATED, leverage %d (configured %d), max notional %s",
            symbol,
            self.client.base_url,
            self._leverage[symbol],
            lev,
            self._max_notional.get(symbol),
        )
        return filters

    def _symbol_config(self, symbol: str) -> dict[str, Any]:
        rows = _rows(self._signed("GET", SYMBOL_CONFIG_PATH, {"symbol": symbol}))
        for row in rows:
            if str(row.get("symbol")) == symbol:
                return row
        raise ConfigError(f"{symbol}: symbolConfig returned no row for the symbol")

    def _keep_exchange_leverage(self, symbol: str, sc: Mapping[str, Any], configured: int) -> None:
        kept = _int(sc.get("leverage"))
        logger.warning(
            "keeping exchange leverage %d for the open position; configured %d applies when flat", kept, configured
        )
        self._leverage[symbol] = kept
        mn = _opt_f(sc.get("maxNotionalValue"))
        if mn is not None and mn > 0:
            self._max_notional[symbol] = mn

    def _apply_leverage(self, symbol: str, leverage: int) -> None:
        """``POST /fapi/v1/leverage``; the response must echo the requested leverage. Caches it + maxNotional."""
        resp = self._signed("POST", LEVERAGE_PATH, {"symbol": symbol, "leverage": int(leverage)})
        resp = resp if isinstance(resp, Mapping) else {}
        got = _int(resp.get("leverage"), -1)
        if got != int(leverage):
            raise ConfigError(f"{symbol}: exchange set leverage {got} instead of {leverage}")
        self._leverage[symbol] = got
        mn = _opt_f(resp.get("maxNotionalValue"))
        if mn is not None and mn > 0:
            self._max_notional[symbol] = mn
        logger.info("%s leverage set to %d (max notional %s)", symbol, got, self._max_notional.get(symbol))

    # ------------------------------------------------------------------------------------------ lookups

    def _lookup_order(self, symbol: str, client_id: str) -> dict[str, Any] | None:
        """``GET /fapi/v1/order symbol, origClientOrderId``; None when the order does not exist."""
        try:
            order = self._signed("GET", ORDER_PATH, {"symbol": symbol, "origClientOrderId": client_id})
        except NoSuchOrderError:
            return None
        return dict(order) if isinstance(order, Mapping) and order else None

    def _lookup_order_retrying(self, symbol: str, client_id: str) -> dict[str, Any] | None:
        """Up to 3 ``_lookup_order`` calls spaced ``sleep(1.0)`` (resolution after a non-definitive POST).

        Lookup failures count as "not found" for that attempt: the caller always follows with a position read.
        """
        for attempt in range(ORDER_LOOKUP_ATTEMPTS):
            if attempt:
                self.sleep(ORDER_LOOKUP_SLEEP_SEC)
            try:
                found = self._lookup_order(symbol, client_id)
            except ExchangeError as exc:
                logger.warning("lookup of order %s failed: %s", client_id, _err_text(exc))
                if _is_fatal(exc):
                    return None
                continue
            if found is not None:
                return found
        return None

    def _await_terminal(self, symbol: str, order: Mapping[str, Any]) -> dict[str, Any]:
        """Poll ``GET /fapi/v1/order symbol, orderId`` (5 x, 1 s apart) until a terminal status; last seen order."""
        current = dict(order)
        polls = 0
        while not _is_terminal(current) and polls < AWAIT_TERMINAL_POLLS:
            polls += 1
            self.sleep(AWAIT_TERMINAL_SLEEP_SEC)
            if current.get("orderId") is not None:
                params: dict[str, Any] = {"symbol": symbol, "orderId": current["orderId"]}
            else:
                params = {"symbol": symbol, "origClientOrderId": current.get("clientOrderId")}
            try:
                nxt = self._signed("GET", ORDER_PATH, params)
            except ExchangeError as exc:
                logger.warning("polling order %s failed: %s", current.get("clientOrderId"), _err_text(exc))
                if _is_fatal(exc):
                    break
                continue
            if isinstance(nxt, Mapping) and nxt:
                current = dict(nxt)
        if not _is_terminal(current):
            logger.warning(
                "order %s still %s after %d polls", current.get("clientOrderId"), _status(current) or "?", polls
            )
        return current

    def _position(self, symbol: str) -> dict[str, Any] | None:
        """One-way position row (``positionSide == "BOTH"``, non-zero ``positionAmt``) or None when flat."""
        for row in _rows(self._signed("GET", POSITION_RISK_PATH, {"symbol": symbol})):
            if str(row.get("symbol", symbol)) != symbol:
                continue
            if str(row.get("positionSide", "BOTH")) != "BOTH":
                continue
            if _f(row.get("positionAmt")) != 0.0:
                return row
        return None

    def _lookup_algo(self, symbol: str, client_algo_id: str) -> dict[str, Any] | None:
        """``GET /fapi/v1/algoOrder clientAlgoId`` (3 attempts, 0.5 s apart while not found)."""
        for attempt in range(ALGO_LOOKUP_ATTEMPTS):
            if attempt:
                self.sleep(ALGO_LOOKUP_SLEEP_SEC)
            try:
                found = self._signed("GET", ALGO_ORDER_PATH, {"clientAlgoId": client_algo_id})
            except NoSuchOrderError:
                continue
            except ExchangeError as exc:
                if _is_fatal(exc):
                    raise
                # any other 4xx (and a transient failure that outlived the client's retries): "not found" this time
                logger.warning("lookup of algo order %s failed: %s", client_algo_id, _err_text(exc))
                continue
            if not isinstance(found, Mapping) or not (found.get("algoId") or found.get("clientAlgoId")):
                continue
            if found.get("symbol") is not None and str(found["symbol"]) != symbol:
                logger.error(
                    "algo order %s belongs to %s, not %s; treating it as not found",
                    client_algo_id,
                    found.get("symbol"),
                    symbol,
                )
                return None
            return dict(found)
        return None

    def _fresh_client_id(self, symbol: str, client_id: str) -> str:
        """A client id that was never used for an order on ``symbol`` (lookups by it are then unambiguous)."""
        cid = client_id
        for _ in range(MAX_FRESH_ID_STEPS):
            if self._lookup_order(symbol, cid) is None:
                return cid
            cid = next_client_id(cid)
        if self._lookup_order(symbol, cid) is None:
            return cid
        raise BotError(f"no unused client id near {client_id} after {MAX_FRESH_ID_STEPS} steps")

    # ------------------------------------------------------------------------------------------ history / income

    def _user_trades(self, symbol: str, **params: Any) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"symbol": symbol}
        query.update({k: v for k, v in params.items() if v is not None})
        return [r for r in _rows(self._signed("GET", USER_TRADES_PATH, query)) if str(r.get("symbol", symbol)) == symbol]

    def _income_rows(self, symbol: str, income_type: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """``GET /fapi/v1/income`` paginated: next ``startTime`` = last row time + 1 until < 1000 rows."""
        out: list[dict[str, Any]] = []
        cur = int(start_ms)
        end = int(end_ms)
        while cur <= end:
            page = _rows(
                self._signed(
                    "GET",
                    INCOME_PATH,
                    {
                        "symbol": symbol,
                        "incomeType": income_type,
                        "startTime": cur,
                        "endTime": end,
                        "limit": INCOME_PAGE_LIMIT,
                    },
                )
            )
            out.extend(page)
            if len(page) < INCOME_PAGE_LIMIT:
                break
            last = max(_int(r.get("time")) for r in page)
            if last + 1 <= cur:  # no progress -> never loop forever
                break
            cur = last + 1
        return out

    def _income_sum(self, symbol: str, income_type: str, start_ms: int, end_ms: int) -> float:
        """Sum of ``income`` (signed as Binance reports it) for ``income_type`` in [start, end]."""
        rows = self._income_rows(symbol, income_type, start_ms, end_ms)
        return float(sum(_f(r.get("income")) for r in rows if str(r.get("symbol", symbol)) == symbol))

    def _funding_income(self, symbol: str, start_ms: int, end_ms: int) -> float:
        """Funding over [start, end]; + = paid (Binance reports paid funding as negative income)."""
        return -self._income_sum(symbol, "FUNDING_FEE", start_ms, end_ms)

    def _aggregate_fills(self, fills: Sequence[Mapping[str, Any]]) -> _Fills:
        qty = 0.0
        notional = 0.0
        fee = 0.0
        pnl = 0.0
        last_time = 0
        taker = float(self.execution.fees.taker)
        for t in fills:
            q = abs(_f(t.get("qty")))
            p = _f(t.get("price"))
            qty += q
            notional += q * p
            pnl += _f(t.get("realizedPnl"))
            last_time = max(last_time, _int(t.get("time")))
            commission = abs(_f(t.get("commission")))
            if str(t.get("commissionAsset") or QUOTE_ASSET) == QUOTE_ASSET:
                fee += commission
            elif commission > 0:
                # SPEC-GAP: fee paid in another asset (BNB discount) -> estimate its USDT value as in the entry rule
                fee += q * p * taker
        price = notional / qty if qty > 0 else 0.0
        return _Fills(qty=qty, price=price, fee=fee, realized_pnl=pnl, last_time=last_time)

    # ------------------------------------------------------------------------------------------ account

    def _open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        rows = _rows(self._signed("GET", OPEN_ALGO_ORDERS_PATH, {"symbol": symbol}))
        return [r for r in rows if str(r.get("symbol", symbol)) == symbol]

    def _open_regular_orders(self, symbol: str) -> list[dict[str, Any]]:
        rows = _rows(self._signed("GET", OPEN_ORDERS_PATH, {"symbol": symbol}))
        return [r for r in rows if str(r.get("symbol", symbol)) == symbol]

    def _account(self, symbol: str) -> AccountSnapshot:
        """Account snapshot of ``symbol`` (own/foreign open orders are kept privately for ``sync``)."""
        return self._read_account(symbol)[0]

    def _read_account(self, symbol: str) -> tuple[AccountSnapshot, _OpenOrderBook]:
        acct = self._signed("GET", ACCOUNT_PATH)
        acct = acct if isinstance(acct, Mapping) else {}
        usdt = next(
            (a for a in _rows(acct.get("assets") or []) if str(a.get("asset")) == QUOTE_ASSET),
            None,
        )
        if usdt is not None:
            wallet = _f(usdt.get("walletBalance"))
            equity = _f(usdt.get("marginBalance"))
            available = _f(usdt.get("availableBalance"))
            upnl = _f(usdt.get("unrealizedProfit"))
        else:
            logger.warning("account response has no USDT asset; using account totals")
            wallet = _f(acct.get("totalWalletBalance"))
            equity = _f(acct.get("totalMarginBalance"))
            available = _f(acct.get("availableBalance"))
            upnl = _f(acct.get("totalUnrealizedProfit"))

        row = self._position(symbol)
        position = self._position_model(symbol, row) if row is not None else None
        algo = self._open_algo_orders(symbol)
        regular = self._open_regular_orders(symbol)
        protective = tuple(p for p in (self._protective_from_open_algo(a) for a in algo) if p is not None)
        snapshot = AccountSnapshot(
            ts=now_ms(self.clock),
            wallet_balance=wallet,
            equity=equity,
            available_balance=available,
            unrealized_pnl=upnl,
            position=position,
            protective_orders=protective,
            open_orders_count=len(regular),
        )
        book = _OpenOrderBook(algo=tuple(algo), regular=tuple(regular))
        self._open_orders[symbol] = book
        return snapshot, book

    def _position_model(self, symbol: str, row: Mapping[str, Any]) -> Position:
        liq = _opt_f(row.get("liquidationPrice"))
        updated = _int(row.get("updateTime"))
        return Position(
            symbol=symbol,
            qty=_f(row.get("positionAmt")),
            entry_price=_f(row.get("entryPrice")),
            mark_price=_opt_f(row.get("markPrice")),
            unrealized_pnl=_f(row.get("unRealizedProfit")),
            liquidation_price=liq if liq is not None and liq > 0 else None,
            isolated_margin=_opt_f(row.get("isolatedMargin")),
            leverage=self._leverage.get(symbol),
            updated_at=updated if updated > 0 else self._server_now(),
        )

    @staticmethod
    def _protective_from_open_algo(a: Mapping[str, Any]) -> ProtectiveOrder | None:
        kind = _ALGO_TYPE_TO_KIND.get(str(a.get("orderType") or a.get("type") or ""))
        if kind is None:
            return None
        try:
            side = Side(str(a.get("side")))
        except ValueError:
            logger.warning("algo order %s has an unknown side %r", a.get("clientAlgoId"), a.get("side"))
            return None
        qty_raw = a.get("quantity")
        quantity = None if qty_raw in (None, "", "0") or _f(qty_raw) == 0.0 else _f(qty_raw)
        return ProtectiveOrder(
            kind=kind,
            client_id=str(a.get("clientAlgoId") or ""),
            exchange_id=None if a.get("algoId") is None else str(a.get("algoId")),
            side=side,
            trigger_price=_f(a.get("triggerPrice")),
            status=str(a.get("algoStatus") or ""),
            close_position=a.get("closePosition") in (True, "true"),
            quantity=quantity,
        )

    # ------------------------------------------------------------------------------------------ cancellation

    def _cancel_algo(self, client_algo_id: str) -> bool:
        """``DELETE /fapi/v1/algoOrder clientAlgoId``; False if the order no longer exists."""
        try:
            self._signed("DELETE", ALGO_ORDER_PATH, {"clientAlgoId": client_algo_id})
        except NoSuchOrderError:
            return False
        logger.info("cancelled algo order %s", client_algo_id)
        return True

    def _cancel_own_orders(self, symbol: str) -> int:
        """Cancel this bot's open algo + regular orders on ``symbol`` one by one. Returns the number cancelled."""
        prefix = self._prefix(symbol)
        cancelled = 0
        for a in self._open_algo_orders(symbol):
            cid = str(a.get("clientAlgoId") or "")
            if is_own(cid, prefix) and self._cancel_algo(cid):
                cancelled += 1
        for o in self._open_regular_orders(symbol):
            cid = str(o.get("clientOrderId") or "")
            if not is_own(cid, prefix):
                continue
            try:
                self._signed("DELETE", ORDER_PATH, {"symbol": symbol, "origClientOrderId": cid})
            except NoSuchOrderError:
                continue
            logger.info("cancelled order %s", cid)
            cancelled += 1
        return cancelled

    def _cancel_own_orders_tolerant(self, symbol: str) -> int:
        """``_cancel_own_orders`` for cleanup paths: a failure is logged (the next sync retries), never raised."""
        try:
            return self._cancel_own_orders(symbol)
        except ExchangeError as exc:
            if _is_fatal(exc):
                raise
            logger.warning("cancelling own orders on %s failed: %s (retried by the next sync)", symbol, _err_text(exc))
            return 0

    def _cleanup_algo_orders(self, symbol: str, keep_side: Side) -> int:
        """-4045 handling: cancel own algo orders that do not belong to the live position.

        For the current position (closing side ``keep_side``) the newest own SL and the newest own TP are kept.
        """
        prefix = self._prefix(symbol)
        own = [a for a in self._open_algo_orders(symbol) if is_own(str(a.get("clientAlgoId") or ""), prefix)]
        keep: set[str] = set()
        for algo_type in _ALGO_TYPE_TO_KIND:
            candidates = [
                a
                for a in own
                if str(a.get("orderType") or a.get("type") or "") == algo_type
                and str(a.get("side")) == keep_side.value
                and str(a.get("algoStatus") or "") in LIVE_PROTECTIVE_STATUSES
            ]
            if candidates:
                newest = max(candidates, key=lambda a: (_int(a.get("createTime")), _int(a.get("algoId"))))
                keep.add(str(newest.get("clientAlgoId")))
        cancelled = 0
        for a in own:
            cid = str(a.get("clientAlgoId"))
            if cid in keep:
                continue
            try:
                if self._cancel_algo(cid):
                    cancelled += 1
            except ExchangeError as exc:
                if _is_fatal(exc):
                    raise
                logger.warning("cancelling algo order %s failed: %s", cid, _err_text(exc))
        logger.warning("algo order limit reached: cancelled %d stale own algo orders on %s", cancelled, symbol)
        return cancelled

    # ------------------------------------------------------------------------------------------ protective orders

    def _protective_params(
        self,
        symbol: str,
        kind: OrderPurpose,
        side: Side,
        trigger: Decimal,
        quantity: Decimal | None,
        client_algo_id: str,
    ) -> dict[str, Any]:
        """``POST /fapi/v1/algoOrder`` params in the exact §9.5 order."""
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side.value,
            "type": _KIND_TO_ALGO_TYPE[kind],
            "triggerPrice": format_decimal(trigger),
            "workingType": str(self.execution.working_type),
            "priceProtect": bool(self.execution.price_protect),
        }
        if self._reduce_only_mode:
            assert quantity is not None
            params["quantity"] = format_decimal(quantity)
            params["reduceOnly"] = True
        else:
            params["closePosition"] = True  # never together with quantity/reduceOnly (-4137)
        params["clientAlgoId"] = client_algo_id
        return params

    def _protective_from_response(
        self,
        a: Mapping[str, Any],
        *,
        kind: OrderPurpose,
        side: Side,
        trigger: Decimal,
        quantity: Decimal | None,
        client_algo_id: str,
    ) -> ProtectiveOrder:
        try:
            resp_side = Side(str(a.get("side")))
        except ValueError:
            resp_side = side
        close_position = (
            a.get("closePosition") in (True, "true") if "closePosition" in a else not self._reduce_only_mode
        )
        qty = _opt_f(a.get("quantity"))
        if qty is None or qty == 0.0:
            qty = None if close_position or quantity is None else float(quantity)
        return ProtectiveOrder(
            kind=kind,
            client_id=str(a.get("clientAlgoId") or client_algo_id),
            exchange_id=None if a.get("algoId") is None else str(a.get("algoId")),
            side=resp_side,
            trigger_price=_f(a.get("triggerPrice"), float(trigger)) or float(trigger),
            status=str(a.get("algoStatus") or ""),
            close_position=close_position,
            quantity=qty,
        )

    def _place_protective(
        self,
        symbol: str,
        kind: OrderPurpose,
        trigger: float | Decimal,
        quantity: float | Decimal | None,
        client_algo_id: str,
        *,
        side: Side,
        entry_price: float,
    ) -> ProtectiveOrder | None:
        """§9.5 placement procedure. Returns the placed order, or None = "failed" (every failure logged at ERROR)."""
        label = "SL" if kind is OrderPurpose.STOP_LOSS else "TP"
        try:
            tick = self._symbol_filters(symbol).tick_size
            trig = round_protective_price(trigger, tick, entry=float(entry_price))
        except (BotError, ValueError, TypeError) as exc:
            logger.error("%s %s for %s: invalid trigger %r (%s)", label, client_algo_id, symbol, trigger, exc)
            return None
        if trig <= 0:
            logger.error("%s %s for %s: trigger %s is not positive", label, client_algo_id, symbol, trig)
            return None
        qty_dec: Decimal | None = None
        if self._reduce_only_mode:
            qty_dec = abs(to_decimal(quantity)) if quantity is not None else Decimal("0")
            if qty_dec <= 0:
                logger.error("%s %s for %s: reduce_only mode needs the position quantity", label, client_algo_id, symbol)
                return None
        params = self._protective_params(symbol, kind, side, trig, qty_dec, client_algo_id)

        def confirmed(a: Mapping[str, Any]) -> ProtectiveOrder:
            po = self._protective_from_response(
                a, kind=kind, side=side, trigger=trig, quantity=qty_dec, client_algo_id=client_algo_id
            )
            logger.info(
                "%s %s placed on %s: %s trigger %s (%s, algoId %s)",
                label,
                client_algo_id,
                symbol,
                side.value,
                format_decimal(trig),
                po.status,
                po.exchange_id,
            )
            return po

        posts = 0
        while posts < 2:  # the first POST plus at most one retry (§9.5)
            posts += 1
            try:
                resp = self._signed("POST", ALGO_ORDER_PATH, params)
            except ImmediateTriggerError as exc:
                logger.error(
                    "%s %s for %s would trigger immediately (%s); not retried", label, client_algo_id, symbol,
                    _err_text(exc),
                )
                return None
            except AlgoLimitError as exc:
                logger.error("%s %s for %s: algo order limit (%s)", label, client_algo_id, symbol, _err_text(exc))
                if posts >= 2:
                    return None
                try:
                    self._cleanup_algo_orders(symbol, keep_side=side)
                except ExchangeError as cleanup_exc:
                    logger.error("algo order cleanup on %s failed: %s", symbol, _err_text(cleanup_exc))
                    return None
                continue
            except BotError as exc:
                if not _non_definitive(exc):
                    logger.error("%s %s for %s rejected: %s", label, client_algo_id, symbol, _err_text(exc))
                    return None
                logger.warning(
                    "%s %s for %s: outcome not definitive (%s); looking it up", label, client_algo_id, symbol,
                    _err_text(exc),
                )
                self._rate_limit_pause(exc)
            else:
                if (
                    isinstance(resp, Mapping)
                    and str(resp.get("clientAlgoId")) == client_algo_id
                    and str(resp.get("algoStatus") or "") in ALGO_OK_STATUSES
                ):
                    return confirmed(resp)
                logger.warning(
                    "%s %s for %s: response without a confirming algoStatus; looking it up",
                    label,
                    client_algo_id,
                    symbol,
                )
            # resolve by lookup (§9.5 step 2); a GET openAlgoOrders right after placement may lag -> never used here
            found = self._lookup_algo(symbol, client_algo_id)
            if found is not None and str(found.get("algoStatus") or "") in ALGO_OK_STATUSES:
                return confirmed(found)
            logger.warning(
                "%s %s for %s not confirmed (status %s)",
                label,
                client_algo_id,
                symbol,
                None if found is None else found.get("algoStatus"),
            )
        logger.error("%s %s for %s could not be placed", label, client_algo_id, symbol)
        return None

    def _protect(
        self,
        symbol: str,
        kind: OrderPurpose,
        trigger: float | Decimal,
        quantity: float | Decimal | None,
        client_algo_id: str,
        *,
        side: Side,
        entry_price: float,
    ) -> ProtectiveOrder | None:
        """``_place_protective`` that never raises: any unexpected error counts as "failed"."""
        try:
            return self._place_protective(
                symbol, kind, trigger, quantity, client_algo_id, side=side, entry_price=entry_price
            )
        except Exception:
            logger.exception("placing %s %s on %s failed unexpectedly", kind.value, client_algo_id, symbol)
            return None

    # ------------------------------------------------------------------------------------------ open_position

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
        qty_plan = to_decimal(plan.qty)
        if qty_plan <= 0:
            raise ValueError(f"plan quantity must be > 0, got {plan.qty!r}")
        side = direction.opening_side
        bar_ms = int(bar_time)

        # 0. leverage catch-up (kept for an earlier position, §prepare_symbol 7); the trader only opens when flat
        desired = self._desired_leverage.get(symbol)
        if desired is not None and self._leverage.get(symbol) != desired:
            try:
                self._apply_leverage(symbol, desired)
            except (AuthError, IpBannedError):
                raise
            except (ExchangeError, ConfigError) as exc:
                message = f"leverage update failed: {_err_text(exc) if isinstance(exc, ExchangeError) else exc}"
                logger.warning("%s: %s", symbol, message)
                return OpenOutcome(filled=False, message=message)

        # 1. pre-flight idempotency: an earlier attempt with this id may already have filled
        order: dict[str, Any] | None = self._lookup_order(symbol, entry_client_id)
        reused = False
        if order is not None:
            if not _is_terminal(order):
                order = self._await_terminal(symbol, order)
            if _executed(order) <= 0 and not _is_terminal(order):
                # SPEC-GAP: still pending after polling -> cancel our own stale order before sending a new one
                order = self._cancel_stale_entry(symbol, order)
            if _executed(order) > 0:
                reused = True
                logger.warning(
                    "entry %s already executed %s (status %s); reusing it instead of sending a new order",
                    entry_client_id,
                    order.get("executedQty"),
                    _status(order),
                )
            else:
                entry_client_id = self._fresh_client_id(symbol, entry_client_id)
                order = None

        # 2. entry
        message = ""
        if not reused:
            params = {
                "symbol": symbol,
                "side": side.value,
                "type": "MARKET",
                "quantity": format_decimal(qty_plan),
                "newClientOrderId": entry_client_id,
                "newOrderRespType": "RESULT",
            }
            try:
                resp = self._signed("POST", ORDER_PATH, params)
            except (AuthError, IpBannedError):
                raise
            except BotError as exc:
                if _non_definitive(exc) or (
                    isinstance(exc, ExchangeError) and not isinstance(exc, (OrderRejectedError, TimestampError))
                ):
                    logger.warning(
                        "entry %s outcome not definitive (%s); looking it up", entry_client_id, _err_text(exc)
                    )
                    self._rate_limit_pause(exc)
                    order = self._lookup_order_retrying(symbol, entry_client_id)
                    if order is None:
                        message = f"entry outcome unknown ({_err_text(exc)})"
                else:
                    order = None
                    message = _err_text(exc)
                    logger.warning("entry %s rejected: %s", entry_client_id, message)
            else:
                order = dict(resp) if isinstance(resp, Mapping) and resp else None

        # 3. a MARKET RESULT can come back NEW / PARTIALLY_FILLED
        if order is not None and not _is_terminal(order):
            order = self._await_terminal(symbol, order)

        # 4. position truth check (mandatory before any filled=False)
        executed = _executed(order)
        try:
            pos = self._position(symbol)
        except ExchangeError as exc:
            if executed <= 0:
                raise  # cannot confirm flat -> never report filled=False
            # SPEC-GAP: the order reports a fill but the position read failed: protect it anyway (the SL does not
            # need the quantity in close_position mode; reduce_only uses the executed quantity).
            logger.error("position read after entry %s failed (%s); protecting the fill", entry_client_id, _err_text(exc))
            pos = {"positionAmt": str(direction.sign * executed), "_assumed": True}
        if pos is None and executed <= 0:
            msg = message or f"entry not filled (status {_status(order) or 'unknown'})"
            logger.info("%s entry %s: %s", symbol, entry_client_id, msg)
            return OpenOutcome(filled=False, message=msg)
        if pos is not None and Direction.from_qty(_f(pos.get("positionAmt"))) is not direction:
            logger.error(
                "%s: unexpected opposite position %s after entry %s; leaving it untouched",
                symbol,
                pos.get("positionAmt"),
                entry_client_id,
            )
            return OpenOutcome(filled=False, message="unexpected opposite position")

        recovered = executed <= 0  # position exists although no fill of ours was identified
        pos_qty = abs(_dec(pos.get("positionAmt"))) if pos is not None else Decimal("0")

        # 5. average price / time
        if not recovered:
            assert order is not None
            qty = executed
            avg = self._entry_avg_price(symbol, order, pos, ref_price)
            entry_time = _int(order.get("updateTime")) or _int(order.get("time")) or self._server_now()
            order_id = order.get("orderId")
        else:
            assert pos is not None
            qty = float(pos_qty)
            avg = _f(pos.get("entryPrice")) or float(ref_price)
            entry_time = _int(pos.get("updateTime")) or self._server_now()
            order_id = None
            logger.warning(
                "%s entry %s: no fill identified but a %s position of %s exists; treating it as filled (recovered)",
                symbol,
                entry_client_id,
                direction.value,
                format_decimal(pos_qty),
            )

        # 6. entry fee
        fee = self._entry_fee(symbol, order_id, qty, avg)
        entry_order = None
        if not recovered and order is not None:
            entry_order = self._order_result(
                order,
                symbol=symbol,
                side=side,
                purpose=OrderPurpose.ENTRY,
                fallback_client_id=entry_client_id,
                avg_price=avg,
                fee=fee,
                ts=entry_time,
            )
        outcome = OpenOutcome(
            filled=True,
            qty=float(qty),
            avg_price=float(avg),
            entry_fee=float(fee),
            entry_time=int(entry_time),
            entry_order=entry_order,
            protective=(),
            message="recovered" if recovered else ("reused" if reused else "filled"),
        )
        logger.info(
            "%s entry %s %s qty=%s avg=%.8g fee=%.8g (%s)",
            symbol,
            entry_client_id,
            direction.value,
            qty,
            avg,
            fee,
            outcome.message,
        )
        if pos is None:
            logger.warning(
                "%s entry %s filled but the position is already closed; the next sync records the closure",
                symbol,
                entry_client_id,
            )
            return dataclasses.replace(outcome, message="filled; position already closed")

        # 7. mandatory stop-loss
        closing = direction.closing_side
        protect_qty = pos_qty if pos_qty > 0 else to_decimal(qty)
        sl = self._protect(
            symbol, OrderPurpose.STOP_LOSS, plan.stop_price, protect_qty, sl_client_id, side=closing, entry_price=avg
        )
        # 8. no SL -> flatten immediately
        if sl is None:
            fl_id = make_client_id(self.bot_id, symbol, "FL", bar_ms, 0)
            logger.critical("%s: stop-loss could not be placed after entry %s; flattening", symbol, entry_client_id)
            try:
                closure = self.close_position(
                    symbol, None, reason=ExitReason.PROTECTION_FAILED, client_id=fl_id, ref_price=None, bar_time=bar_ms
                )
            except Exception as exc:
                raise EmergencyError("entry filled but unprotected and flatten failed", entry=outcome) from exc
            raise ProtectionFailedError(
                "stop-loss could not be placed; position flattened", flattened=True, closure=closure, entry=outcome
            )
        protective: list[ProtectiveOrder] = [sl]

        # 9. optional take-profit (failure keeps the position: the SL exists)
        if plan.take_profit_price is not None:
            if tp_client_id is None:
                logger.warning("%s: take-profit planned but no client id given; skipped", symbol)
            else:
                tp = self._protect(
                    symbol,
                    OrderPurpose.TAKE_PROFIT,
                    plan.take_profit_price,
                    protect_qty,
                    tp_client_id,
                    side=closing,
                    entry_price=avg,
                )
                if tp is None:
                    logger.warning("%s: take-profit %s could not be placed; keeping the position (SL exists)",
                                   symbol, tp_client_id)
                else:
                    protective.append(tp)

        # 10.
        return dataclasses.replace(outcome, protective=tuple(protective))

    def _cancel_stale_entry(self, symbol: str, order: Mapping[str, Any]) -> dict[str, Any]:
        """Cancel our own still-pending entry order and return its final state (best effort)."""
        cid = str(order.get("clientOrderId") or "")
        try:
            self._signed("DELETE", ORDER_PATH, {"symbol": symbol, "origClientOrderId": cid})
            logger.warning("cancelled stale pending entry order %s", cid)
        except NoSuchOrderError:
            pass
        except ExchangeError as exc:
            if _is_fatal(exc):
                raise
            logger.warning("cancelling stale entry order %s failed: %s", cid, _err_text(exc))
        found = self._lookup_order(symbol, cid)
        return dict(found) if found is not None else dict(order)

    def _entry_avg_price(
        self, symbol: str, order: Mapping[str, Any], pos: Mapping[str, Any] | None, ref_price: float
    ) -> float:
        avg = _f(order.get("avgPrice"))
        if avg > 0:
            return avg
        if order.get("orderId") is not None:
            try:
                full = self._signed("GET", ORDER_PATH, {"symbol": symbol, "orderId": order["orderId"]})
                if isinstance(full, Mapping):
                    avg = _f(full.get("avgPrice"))
                    if avg <= 0 and _f(full.get("executedQty")) > 0:
                        avg = _f(full.get("cumQuote")) / _f(full.get("executedQty"))
            except Exception as exc:  # never raise between the fill and the stop-loss
                logger.warning("average price lookup of order %s failed: %s", order.get("orderId"), exc)
        if avg <= 0 and _executed(order) > 0 and _f(order.get("cumQuote")) > 0:
            avg = _f(order.get("cumQuote")) / _executed(order)
        if avg <= 0 and pos is not None:
            avg = _f(pos.get("entryPrice"))
        if avg <= 0:
            logger.warning("average fill price of %s unknown; using the reference price", order.get("clientOrderId"))
            avg = float(ref_price)
        return avg

    def _entry_fee(self, symbol: str, order_id: Any, qty: float, avg: float) -> float:
        """USDT commission of the entry from userTrades; estimate ``qty*avg*taker`` when unavailable."""
        estimate = abs(float(qty)) * float(avg) * float(self.execution.fees.taker)
        if order_id is None:
            return estimate
        try:
            fills = [t for t in self._user_trades(symbol, orderId=order_id) if str(t.get("orderId")) == str(order_id)]
        except Exception as exc:  # never raise between the fill and the stop-loss
            logger.warning("userTrades of entry order %s unavailable: %s; fee estimated", order_id, exc)
            return estimate
        if not fills:
            return estimate
        if any(
            str(t.get("commissionAsset") or QUOTE_ASSET) != QUOTE_ASSET and _f(t.get("commission")) != 0.0
            for t in fills
        ):
            return estimate
        return float(sum(abs(_f(t.get("commission"))) for t in fills))

    def _order_result(
        self,
        order: Mapping[str, Any],
        *,
        symbol: str,
        side: Side,
        purpose: OrderPurpose,
        fallback_client_id: str,
        avg_price: float | None,
        fee: float,
        ts: int,
    ) -> OrderResult:
        requested = _opt_f(order.get("origQty"))
        return OrderResult(
            client_id=str(order.get("clientOrderId") or fallback_client_id),
            exchange_id=None if order.get("orderId") is None else str(order.get("orderId")),
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            purpose=purpose,
            status=_order_status(order.get("status")),
            requested_qty=requested if requested and requested > 0 else None,
            executed_qty=_executed(order),
            avg_price=avg_price if avg_price and avg_price > 0 else None,
            trigger_price=None,
            fee=float(fee),
            ts=int(ts),
            raw=dict(order),
        )

    # ------------------------------------------------------------------------------------------ close_position

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
        reason = ExitReason(reason)
        pos = self._position(symbol)
        if pos is None:
            cancelled = self._cancel_own_orders_tolerant(symbol)
            if cancelled:
                logger.info("%s already flat; cancelled %d own orphan orders", symbol, cancelled)
            return None

        initial_direction = Direction.from_qty(_f(pos.get("positionAmt")))
        initial_qty = abs(_dec(pos.get("positionAmt")))
        client_id = self._fresh_client_id(symbol, client_id)
        close_orders: list[dict[str, Any]] = []
        flat = False
        for n_order in range(MAX_CLOSE_ORDERS):
            if n_order:
                client_id = self._fresh_client_id(symbol, next_client_id(client_id))
            amt = _dec(pos.get("positionAmt"))
            closing = Direction.from_qty(float(amt)).closing_side
            params = {
                "symbol": symbol,
                "side": closing.value,
                "type": "MARKET",
                "quantity": format_decimal(abs(amt)),
                "reduceOnly": True,
                "newClientOrderId": client_id,
                "newOrderRespType": "RESULT",
            }
            try:
                resp = self._signed("POST", ORDER_PATH, params)
            except DuplicateClientIdError as exc:
                logger.warning("close %s: duplicate client id (%s); re-reading the position", client_id, _err_text(exc))
            except ReduceOnlyRejectedError as exc:
                logger.warning("close %s rejected as reduce-only (%s); re-reading the position", client_id, _err_text(exc))
            except (AuthError, IpBannedError):
                raise
            except BotError as exc:
                if _non_definitive(exc) or (
                    isinstance(exc, ExchangeError) and not isinstance(exc, (OrderRejectedError, TimestampError))
                ):
                    logger.warning("close %s outcome not definitive (%s); looking it up", client_id, _err_text(exc))
                    self._rate_limit_pause(exc)
                    found = self._lookup_order_retrying(symbol, client_id)
                    if found is not None:
                        close_orders.append(self._await_terminal(symbol, found))
                else:
                    logger.error("close %s rejected: %s", client_id, _err_text(exc))
            else:
                if isinstance(resp, Mapping) and resp:
                    close_orders.append(self._await_terminal(symbol, resp))
            pos = self._position(symbol)
            if pos is None:
                flat = True
                break
            logger.warning(
                "%s still has a position of %s after close order %s", symbol, pos.get("positionAmt"), client_id
            )
        if not flat:
            raise EmergencyError(f"position not flat after close ({symbol}, {MAX_CLOSE_ORDERS} orders sent)")

        self._cancel_own_orders_tolerant(symbol)
        if self._position(symbol) is not None:
            raise EmergencyError(f"position not flat after close ({symbol})")

        closure = self._closure_from_close_orders(
            symbol,
            active,
            reason=reason,
            orders=close_orders,
            closing_side=initial_direction.closing_side,
            initial_qty=initial_qty,
            ref_price=ref_price,
            bar_time=int(bar_time),
            last_client_id=client_id,
        )
        logger.info(
            "%s closed (%s): qty=%s exit=%.8g fee=%.8g gross=%s funding=%.8g",
            symbol,
            reason.value,
            closure.qty,
            closure.exit_price,
            closure.exit_fee,
            closure.gross_pnl,
            closure.funding,
        )
        return closure

    def _closure_from_close_orders(
        self,
        symbol: str,
        active: ActiveTrade | None,
        *,
        reason: ExitReason,
        orders: Sequence[Mapping[str, Any]],
        closing_side: Side,
        initial_qty: Decimal,
        ref_price: float | None,
        bar_time: int,
        last_client_id: str,
    ) -> PositionClosure:
        now = self._server_now()
        fills: dict[str, dict[str, Any]] = {}
        order_ids = [str(o["orderId"]) for o in orders if o.get("orderId") is not None and _executed(o) > 0]
        try:
            for oid in order_ids:
                for t in self._user_trades(symbol, orderId=oid):
                    if str(t.get("orderId")) == oid:
                        fills[str(t.get("id", f"{oid}:{len(fills)}"))] = t
            if not fills:
                # fallback: no close order identified (or its fills are not visible yet)
                start = max(int(bar_time) - CLOSE_FALLBACK_LOOKBACK_MS, now - HISTORY_WINDOW_MS)
                for t in self._user_trades(symbol, startTime=start, limit=USER_TRADES_LIMIT):
                    if str(t.get("side")) == closing_side.value:
                        fills[str(t.get("id", f"t:{len(fills)}"))] = t
        except ExchangeError as exc:
            if _is_fatal(exc):
                raise
            logger.warning("close fills of %s unavailable (%s); closure details estimated", symbol, _err_text(exc))

        last_order = orders[-1] if orders else None
        if fills:
            agg = self._aggregate_fills(list(fills.values()))
            qty = agg.qty
            exit_price = agg.price
            exit_fee = agg.fee
            gross: float | None = agg.realized_pnl
            exit_time = agg.last_time or now
        else:
            qty = float(initial_qty)
            exit_price = _f(last_order.get("avgPrice")) if last_order is not None else 0.0
            if exit_price <= 0:
                exit_price = float(ref_price) if ref_price else (float(active.entry_price) if active else 0.0)
            exit_fee = qty * exit_price * float(self.execution.fees.taker)
            gross = None
            exit_time = (_int(last_order.get("updateTime")) if last_order is not None else 0) or now
            logger.warning("%s: close fills not found; exit price/fee estimated", symbol)

        funding = 0.0
        if active is not None:
            try:
                funding = self._funding_income(symbol, int(active.entry_time), now)
            except ExchangeError as exc:
                if _is_fatal(exc):
                    raise
                logger.warning("funding income of %s unavailable (%s); booked as 0", symbol, _err_text(exc))

        order_result = None
        if last_order is not None:
            oid = str(last_order.get("orderId")) if last_order.get("orderId") is not None else None
            order_fee = sum(
                abs(_f(t.get("commission")))
                for t in fills.values()
                if oid is not None and str(t.get("orderId")) == oid
                and str(t.get("commissionAsset") or QUOTE_ASSET) == QUOTE_ASSET
            )
            order_result = self._order_result(
                last_order,
                symbol=symbol,
                side=closing_side,
                purpose=exit_purpose(reason),
                fallback_client_id=last_client_id,
                avg_price=_f(last_order.get("avgPrice")) or None,
                fee=float(order_fee),
                ts=_int(last_order.get("updateTime")) or exit_time,
            )
        return PositionClosure(
            exit_time=int(exit_time),
            exit_price=float(exit_price),
            qty=float(qty),
            reason=reason,
            exit_fee=float(exit_fee),
            funding=float(funding),
            gross_pnl=None if gross is None else float(gross),
            order=order_result,
        )

    # ------------------------------------------------------------------------------------------ sync

    def sync(self, symbol: str, active: ActiveTrade | None, closed_candles: Sequence[Candle]) -> SyncResult:
        """Read the account, detect a closure of ``active``, clean own orphans, report issues (candles ignored)."""
        account, book = self._read_account(symbol)
        prefix = self._prefix(symbol)
        issues: list[str] = []
        closure: PositionClosure | None = None
        cancelled = 0
        pos = account.position

        # 2. the tracked position is gone: closed by SL/TP/liquidation/manual
        if active is not None and pos is None:
            closure, unknown = self._detect_closure(symbol, active)
            if unknown:
                issues.append(ISSUE_CLOSURE_DETAILS_UNKNOWN)
            n = self._cancel_own_orders_tolerant(symbol)
            if n:
                cancelled += n
                issues.append(ISSUE_ORPHAN_PROTECTIVE_CANCELED)

        # 3. a position the trader does not know about
        if active is None and pos is not None:
            issues.append(ISSUE_UNTRACKED_POSITION)

        # 4. size differs from the tracked trade
        if active is not None and pos is not None and abs(abs(pos.qty) - float(active.qty)) > QTY_EPS:
            issues.append(ISSUE_QTY_MISMATCH)

        if pos is not None:
            closing = pos.direction.closing_side
            own_sl = live_own_orders(account.protective_orders, prefix, OrderPurpose.STOP_LOSS, closing)
            # 5. no own live stop (foreign stops do not count)
            if not own_sl:
                issues.append(ISSUE_SL_MISSING)
            # 6. reduce_only quantities must cover the position
            elif self._reduce_only_mode:
                own_tp = live_own_orders(account.protective_orders, prefix, OrderPurpose.TAKE_PROFIT, closing)
                sl_ok = any(covers_position(o, pos.qty) for o in own_sl)
                tp_ok = not own_tp or any(tp_matches_position(o, pos.qty) for o in own_tp)
                if not (sl_ok and tp_ok):
                    issues.append(ISSUE_PROTECTION_QTY_MISMATCH)

        # 7. flat with own (orphan) protective orders left over
        if pos is None and active is None:
            if any(is_own(str(a.get("clientAlgoId") or ""), prefix) for a in book.algo):
                n = self._cancel_own_orders_tolerant(symbol)
                if n:
                    cancelled += n
                    issues.append(ISSUE_ORPHAN_PROTECTIVE_CANCELED)

        # 8. foreign orders are reported, never cancelled
        foreign_algo = [a for a in book.algo if not is_own(str(a.get("clientAlgoId") or ""), prefix)]
        foreign_regular = [o for o in book.regular if not is_own(str(o.get("clientOrderId") or ""), prefix)]
        if foreign_algo or foreign_regular:
            issues.append(ISSUE_FOREIGN_OPEN_ORDERS)

        # 9.
        if cancelled:
            account = self._account(symbol)
        return SyncResult(account=account, closure=closure, issues=tuple(dict.fromkeys(issues)))

    def _detect_closure(self, symbol: str, active: ActiveTrade) -> tuple[PositionClosure, bool]:
        """Closure of ``active`` from the exchange history. Returns (closure, details_unknown)."""
        now = self._server_now()
        entry_time = int(active.entry_time)
        start = max(entry_time, now - HISTORY_WINDOW_MS)
        closing = active.direction.closing_side
        trades = self._user_trades(symbol, startTime=start, limit=USER_TRADES_LIMIT)
        fills = [
            t
            for t in trades
            if str(t.get("side")) == closing.value
            and _int(t.get("time")) >= entry_time
            and (active.entry_order_id is None or str(t.get("orderId")) != str(active.entry_order_id))
        ]
        funding = self._funding_income(symbol, entry_time, now)
        if not fills:
            logger.warning("%s: position closed but no closing fills were found; details unknown", symbol)
            return (
                PositionClosure(
                    exit_time=now,
                    exit_price=float(active.entry_price),
                    qty=float(active.qty),
                    reason=ExitReason.UNKNOWN,
                    exit_fee=0.0,
                    funding=float(funding),
                    gross_pnl=None,
                ),
                True,
            )
        reason = self._closure_reason(symbol, start, fills)
        agg = self._aggregate_fills(fills)
        exit_fee = agg.fee
        if reason is ExitReason.LIQUIDATION:
            # SPEC-GAP: verify on Demo whether the clearance fee is already inside realizedPnl/commission; if it is,
            # INSURANCE_CLEAR rows will simply be absent.
            exit_fee += abs(min(0.0, self._income_sum(symbol, "INSURANCE_CLEAR", entry_time, now)))
        closure = PositionClosure(
            exit_time=int(agg.last_time or now),
            exit_price=float(agg.price),
            qty=float(agg.qty),
            reason=reason,
            exit_fee=float(exit_fee),
            funding=float(funding),
            gross_pnl=float(agg.realized_pnl),
        )
        logger.info(
            "%s position closed on the exchange (%s): qty=%s exit=%.8g fee=%.8g pnl=%.8g funding=%.8g",
            symbol,
            reason.value,
            closure.qty,
            closure.exit_price,
            closure.exit_fee,
            agg.realized_pnl,
            closure.funding,
        )
        return closure, False

    def _closure_reason(self, symbol: str, start_ms: int, fills: Iterable[Mapping[str, Any]]) -> ExitReason:
        prefix = self._prefix(symbol)
        last_fill_time: dict[str, int] = {}
        for t in fills:
            oid = str(t.get("orderId"))
            last_fill_time[oid] = max(last_fill_time.get(oid, 0), _int(t.get("time")))
        if not last_fill_time:
            return ExitReason.UNKNOWN
        try:
            history = _rows(self._signed("GET", ALL_ALGO_ORDERS_PATH, {"symbol": symbol, "startTime": int(start_ms)}))
        except ExchangeError as exc:
            if _is_fatal(exc):
                raise
            logger.warning("allAlgoOrders of %s unavailable (%s); closure reason unknown", symbol, _err_text(exc))
            return ExitReason.UNKNOWN
        triggered: dict[str, ExitReason] = {}
        for a in history:
            if not is_own(str(a.get("clientAlgoId") or ""), prefix):
                continue
            actual = a.get("actualOrderId")
            if actual in (None, "", 0, "0"):
                continue
            kind = _ALGO_TYPE_TO_KIND.get(str(a.get("orderType") or a.get("type") or ""))
            if kind is not None and str(actual) in last_fill_time:
                triggered[str(actual)] = _ALGO_KIND_TO_EXIT[kind]
        if triggered:
            latest = max(triggered, key=lambda oid: last_fill_time[oid])
            return triggered[latest]

        last_oid = max(last_fill_time, key=lambda oid: last_fill_time[oid])
        try:
            order = self._signed("GET", ORDER_PATH, {"symbol": symbol, "orderId": last_oid})
        except ExchangeError as exc:
            if _is_fatal(exc):
                raise
            logger.warning("lookup of closing order %s failed (%s); closure reason unknown", last_oid, _err_text(exc))
            return ExitReason.UNKNOWN
        cid = str(order.get("clientOrderId") or "") if isinstance(order, Mapping) else ""
        if cid.startswith(LIQUIDATION_CLIENT_ID_PREFIX):
            return ExitReason.LIQUIDATION
        # SPEC-GAP: one of our own close orders (e.g. a flatten whose result was never recorded) keeps its meaning
        own_kind = _kind_of_own_id(cid, prefix)
        if own_kind in _OWN_KIND_TO_EXIT:
            return _OWN_KIND_TO_EXIT[own_kind]
        return ExitReason.MANUAL

    # ------------------------------------------------------------------------------------------ ensure_protection

    def ensure_protection(
        self,
        active: ActiveTrade,
        account: AccountSnapshot,
        *,
        sl_client_id: str,
        tp_client_id: str | None,
    ) -> tuple[ProtectiveOrder, ...]:
        symbol = active.symbol
        pos = account.position
        if pos is None or pos.direction is Direction.FLAT:
            logger.info("ensure_protection: %s has no position; nothing to protect", symbol)
            return ()
        prefix = self._prefix(symbol)
        closing = pos.direction.closing_side
        pos_qty = abs(float(pos.qty))
        reduce_only = self._reduce_only_mode
        own = [o for o in account.protective_orders if is_own(o.client_id, prefix)]

        def live(o: ProtectiveOrder) -> bool:
            return str(o.status) in LIVE_PROTECTIVE_STATUSES and o.side == closing

        valid_sls = [
            o
            for o in own
            if o.kind is OrderPurpose.STOP_LOSS and live(o) and (not reduce_only or covers_position(o, pos_qty))
        ]
        valid_tps = [
            o
            for o in own
            if o.kind is OrderPurpose.TAKE_PROFIT and live(o) and (not reduce_only or tp_matches_position(o, pos_qty))
        ]
        need_sl = not valid_sls
        need_tp = active.take_profit_price is not None and not valid_tps
        if not need_sl and not need_tp:
            return tuple(valid_sls + valid_tps)

        quantity = to_decimal(pos_qty)
        placed: list[ProtectiveOrder] = []
        replaced: set[OrderPurpose] = set()
        if need_sl:
            sl = self._protect(
                symbol,
                OrderPurpose.STOP_LOSS,
                active.stop_price,
                quantity,
                sl_client_id,
                side=closing,
                entry_price=float(active.entry_price),
            )
            if sl is None:
                fl_id = make_client_id(self.bot_id, symbol, "FL", active.entry_bar_open_time, active.protect_seq + 1)
                logger.critical("%s: stop-loss could not be re-placed; flattening the position", symbol)
                try:
                    closure = self.close_position(
                        symbol,
                        active,
                        reason=ExitReason.PROTECTION_FAILED,
                        client_id=fl_id,
                        ref_price=None,
                        bar_time=self._server_now(),
                    )
                except Exception as exc:
                    raise EmergencyError(f"{symbol} position unprotected and flatten failed: {exc}") from exc
                raise ProtectionFailedError(
                    "stop-loss could not be re-placed; position flattened", flattened=True, closure=closure, entry=None
                )
            placed.append(sl)
            replaced.add(OrderPurpose.STOP_LOSS)
        if need_tp:
            if tp_client_id is None:
                logger.warning("%s: take-profit missing but no client id given; skipped", symbol)
            else:
                assert active.take_profit_price is not None
                tp = self._protect(
                    symbol,
                    OrderPurpose.TAKE_PROFIT,
                    active.take_profit_price,
                    quantity,
                    tp_client_id,
                    side=closing,
                    entry_price=float(active.entry_price),
                )
                if tp is None:
                    logger.warning("%s: take-profit %s could not be placed (SL exists)", symbol, tp_client_id)
                else:
                    placed.append(tp)
                    replaced.add(OrderPurpose.TAKE_PROFIT)

        # place-before-cancel: the stale own orders of the replaced kinds go only after the new ones exist
        new_ids = {p.client_id for p in placed}
        for o in own:
            if o.kind in replaced and o.client_id not in new_ids:
                try:
                    self._cancel_algo(o.client_id)
                except ExchangeError as exc:
                    if _is_fatal(exc):
                        raise
                    logger.warning("cancelling stale %s %s failed: %s", o.kind.value, o.client_id, _err_text(exc))
        kept = [o for o in valid_sls if OrderPurpose.STOP_LOSS not in replaced] + [
            o for o in valid_tps if OrderPurpose.TAKE_PROFIT not in replaced
        ]
        return tuple(placed + kept)
