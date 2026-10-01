"""Binance USDT-M futures REST client (SPEC §6.1).

- HMAC-SHA256 signing; every parameter (incl. ``signature``) travels in the query string, never in a body.
- Method-aware error mapping (``map_error``) onto ``bot.errors`` classes.
- Retry policy: GET/DELETE/PUT are retried on transient failures; POST only when the request is known not to
  have been executed (connect timeout, 503 "Service Unavailable", -1008).
- Weight guard, clock-offset sync (``/fapi/v1/time``) and resync on -1021/-5028.
- Security: messages/attributes of raised errors contain only the method and the path — never the query string,
  API key or signature. ``requests`` exceptions (their text holds the full signed URL) are converted and raised
  ``from None`` outside the ``except`` block, so they are never chained into a traceback.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any, Final, Literal
from urllib.parse import quote, urlencode

import numpy as np
import requests

from bot import logging_setup
from bot.errors import (
    AlgoLimitError,
    AuthError,
    DuplicateClientIdError,
    ExchangeError,
    ImmediateTriggerError,
    InsufficientMarginError,
    IpBannedError,
    MinNotionalError,
    NoChangeNeededError,
    NoSuchOrderError,
    OrderRejectedError,
    RateLimitError,
    ReduceOnlyModeError,
    ReduceOnlyRejectedError,
    TimestampError,
    TransientError,
    UnknownOrderStatusError,
)
from bot.exchange.filters import format_decimal, to_decimal
from bot.timeutil import floor_time, now_ms

logger = logging.getLogger(__name__)

USER_AGENT: Final = "binance-futures-bot/0.1"
BACKOFF_SECONDS: Final = (0.2, 0.4, 0.8)
WEIGHT_GUARD_FRACTION: Final = 0.8

HttpMethod = Literal["GET", "POST", "PUT", "DELETE"]
HTTP_METHODS: Final[frozenset[str]] = frozenset({"GET", "POST", "PUT", "DELETE"})
ORDER_PATHS: Final[frozenset[str]] = frozenset({"/fapi/v1/order", "/fapi/v1/algoOrder"})
TIME_PATH: Final = "/fapi/v1/time"

IP_BAN_DEFAULT_RETRY_AFTER: Final = 120.0
RATE_LIMIT_DEFAULT_RETRY_AFTER: Final = 60.0
RATE_LIMIT_MAX_SLEEP: Final = 60.0
CLOCK_SKEW_WARN_MS: Final = 1000
CLOCK_SKEW_WARN_INTERVAL_SEC: Final = 600.0
WEIGHT_GUARD_MARGIN_SEC: Final = 0.5

_MINUTE_MS: Final = 60_000
_MAX_MESSAGE_LEN: Final = 200
_SIGNED_RESERVED: Final = frozenset({"recvWindow", "timestamp", "signature"})

_RATE_HEADER_RE: Final = re.compile(r"x-mbx-(used-weight|order-count)-(\d+)([smhd])", re.IGNORECASE)
_SIGNATURE_RE: Final = re.compile(r"(signature=)[0-9a-fA-F]+", re.IGNORECASE)
_QUERY_RE: Final = re.compile(r"\?[^\s\"'<>]*=[^\s\"'<>]*")

# Order-level error codes (§6.1 table rows after the transient rows).
_CODE_CLASSES: Final[dict[int, type[ExchangeError]]] = {
    -4046: NoChangeNeededError,
    -4059: NoChangeNeededError,
    -4171: NoChangeNeededError,
    -2018: InsufficientMarginError,
    -2019: InsufficientMarginError,
    -2021: ImmediateTriggerError,
    -4142: ImmediateTriggerError,
    -2022: ReduceOnlyRejectedError,
    -4118: ReduceOnlyRejectedError,
    -2013: NoSuchOrderError,
    -2011: NoSuchOrderError,
    -4116: DuplicateClientIdError,
    -4045: AlgoLimitError,
    -4164: MinNotionalError,
    -4400: ReduceOnlyModeError,
    -4401: ReduceOnlyModeError,
}


# ---------------------------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------------------------


def hmac_sha256_signature(secret: str, payload: str) -> str:
    """Lower-case hex HMAC-SHA256 of ``payload`` keyed with ``secret``."""
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _param_value(key: str, value: Any) -> str:
    if isinstance(value, np.generic):  # numpy boundary (§0.2)
        value = value.item()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, float):
        return format_decimal(to_decimal(value))  # Decimal(str(v)); NaN/inf -> ValueError
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, str):
        return str(value)  # StrEnum -> its value
    raise TypeError(f"unsupported type for request parameter {key!r}: {type(value).__name__}")


def encode_params(params: Mapping[str, Any] | None) -> str:
    """URL-encode ``params`` in insertion order, dropping ``None`` values.

    bool -> "true"/"false"; Decimal -> ``format_decimal``; float -> ``format_decimal(Decimal(str(v)))``;
    int/str unchanged. Spaces become ``%20`` (``quote_via=quote``).
    """
    if not params:
        return ""
    pairs = [(str(k), _param_value(str(k), v)) for k, v in params.items() if v is not None]
    return urlencode(pairs, quote_via=quote)


def parse_rate_limit_headers(headers: Mapping[str, str]) -> dict[str, int]:
    """``{"used-weight-1m": 51, "order-count-10s": 3, ...}`` from ``X-MBX-*`` headers (case-insensitive)."""
    out: dict[str, int] = {}
    if not headers:
        return out
    for name, value in headers.items():
        m = _RATE_HEADER_RE.fullmatch(str(name).strip())
        if m is None:
            continue
        try:
            count = int(str(value).strip())
        except ValueError:
            continue
        out[f"{m.group(1).lower()}-{m.group(2)}{m.group(3).lower()}"] = count
    return out


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return None if value is None else str(value)
    return None


def _retry_after(headers: Mapping[str, str] | None, default: float) -> float:
    raw = _header(headers, "Retry-After")
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return value if value >= 0 and value == value else default


def _error_code(payload: Any) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    code = payload.get("code")
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return int(code)
    if isinstance(code, str):
        try:
            return int(code.strip())
        except ValueError:
            return None
    return None


def _payload_text(payload: Any) -> str:
    if isinstance(payload, Mapping):
        msg = payload.get("msg")
        return "" if msg is None else str(msg)
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", "replace")
    return str(payload)


def _sanitize(text: str) -> str:
    """Single line, no query strings / signatures, bounded length."""
    t = " ".join(str(text).split())
    t = _SIGNATURE_RE.sub(r"\1***", t)
    t = _QUERY_RE.sub("?***", t)
    if len(t) > _MAX_MESSAGE_LEN:
        t = t[:_MAX_MESSAGE_LEN] + "..."
    return t


def _path_only(path: str) -> str:
    return str(path).split("?", 1)[0]


def map_error(http_status: int, payload: Any, *, path: str, method: str, headers: Mapping[str, str]) -> ExchangeError:
    """Map a failed response onto the ``bot.errors`` hierarchy (§6.1 table; first matching rule wins).

    ``payload`` is the parsed JSON (dict) or the raw response text.
    """
    status = int(http_status)
    verb = str(method).upper()
    clean_path = _path_only(path)
    code = _error_code(payload)
    text = _payload_text(payload)
    is_json = isinstance(payload, (Mapping, list))
    msg = _sanitize(text) or f"HTTP {status}"
    kw: dict[str, Any] = {"code": code, "http_status": status, "path": clean_path}

    if status == 418:
        return IpBannedError(msg, retry_after=_retry_after(headers, IP_BAN_DEFAULT_RETRY_AFTER), **kw)
    if status == 429 or code in (-1003, -1015):
        return RateLimitError(msg, retry_after=_retry_after(headers, RATE_LIMIT_DEFAULT_RETRY_AFTER), **kw)
    if code in (-1021, -5028):
        return TimestampError(msg, **kw)
    if code in (-1022, -2014, -2015):
        return AuthError(msg, **kw)
    # POST "known not executed" is checked BEFORE the non-JSON rule (a 503 "Service Unavailable" page is not JSON).
    if verb == "POST" and (code == -1008 or (status == 503 and "Service Unavailable" in text)):
        return TransientError(msg, not_executed=True, **kw)
    if verb == "POST" and (code in (-1007, -1000) or (status >= 500 and ("Unknown error" in text or not is_json))):
        return UnknownOrderStatusError(msg, **kw)
    if status >= 500 or code in (-1000, -1001, -1007, -1008):
        return TransientError(msg, not_executed=False, **kw)
    cls = _CODE_CLASSES.get(code) if code is not None else None
    if cls is not None:
        return cls(msg, **kw)
    if 400 <= status < 500 and code is not None and clean_path in ORDER_PATHS:
        return OrderRejectedError(msg, **kw)
    return ExchangeError(msg, **kw)


def _describe_request_exception(exc: requests.exceptions.RequestException) -> str:
    """A short, URL-free description of a ``requests`` exception."""
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "read timeout"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.SSLError):
        return "SSL error"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy error"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection error"
    if isinstance(exc, requests.exceptions.ChunkedEncodingError):
        return "connection broken while reading the response"
    if isinstance(exc, requests.exceptions.ContentDecodingError):
        return "response decoding error"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "too many redirects"
    return type(exc).__name__


_INVALID_REQUEST_EXCEPTIONS: Final = (
    requests.exceptions.InvalidURL,
    requests.exceptions.MissingSchema,
    requests.exceptions.InvalidSchema,
    requests.exceptions.InvalidHeader,
    requests.exceptions.URLRequired,
)


# ---------------------------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------------------------


class BinanceRestClient:
    """Synchronous Binance USDT-M futures REST client.

    Without credentials (paper mode) only ``public_get`` works; ``signed_request`` raises ``AuthError`` before any
    I/O, so a signed call is impossible by construction.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        recv_window_ms: int = 5000,
        timeout: tuple[float, float] = (5.0, 15.0),
        max_retries: int = 3,
        weight_limit: int = 2400,
        resync_interval_sec: float = 300.0,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        self.base_url: str = base_url.strip().rstrip("/")
        key = (api_key or "").strip()
        secret = (api_secret or "").strip()
        self._api_key = key
        self._api_secret = secret
        self.has_credentials: bool = bool(key) and bool(secret)
        if key or secret:
            # Defence in depth: the CLI registers them too, but a client must never be the reason a key is logged.
            logging_setup.add_secrets([s for s in (key, secret) if s])
        self.recv_window_ms: int = int(recv_window_ms)
        if isinstance(timeout, (int, float)):
            self.timeout: tuple[float, float] = (float(timeout), float(timeout))
        else:
            self.timeout = (float(timeout[0]), float(timeout[1]))
        self.max_retries: int = max(0, int(max_retries))
        self.weight_limit: int = int(weight_limit)
        self.resync_interval_sec: float = float(resync_interval_sec)
        self._owns_session = session is None
        self._session: requests.Session = session if session is not None else requests.Session()
        self._clock = clock
        self._sleep = sleep

        self.used_weight_1m: int | None = None
        self.order_count_10s: int | None = None
        self.order_count_1m: int | None = None
        self._weight_seen_ms: int | None = None  # server-time ms at which used_weight_1m arrived

        self._offset_ms: int | None = None
        self._synced_at_s: float | None = None  # clock() seconds of the last sync
        self._skew_warned_at_s: float | None = None

    # -- misc ---------------------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"BinanceRestClient(base_url={self.base_url!r}, has_credentials={self.has_credentials})"

    @property
    def clock(self) -> Callable[[], float]:
        """The injected wall clock (seconds); MarketData uses it for its filter cache TTL."""
        return self._clock

    def close(self) -> None:
        """Close the HTTP session if this client created it (idempotent)."""
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> BinanceRestClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- time -----------------------------------------------------------------------------------

    def sync_time(self) -> int:
        """Measure ``offset = serverTime - local midpoint`` with ``GET /fapi/v1/time``; returns the offset in ms."""
        t0 = self._clock()
        data = self._request("GET", TIME_PATH, None, signed=False)
        t1 = self._clock()
        try:
            server = int(data["serverTime"])
        except (TypeError, KeyError, ValueError):
            server = None
        if server is None:
            raise ExchangeError("invalid server time response", path=TIME_PATH)
        offset = server - int(round((t0 + t1) / 2 * 1000))
        first = self._offset_ms is None
        self._offset_ms = offset
        self._synced_at_s = t1
        if abs(offset) > CLOCK_SKEW_WARN_MS and (
            self._skew_warned_at_s is None or t1 - self._skew_warned_at_s >= CLOCK_SKEW_WARN_INTERVAL_SEC
        ):
            logger.warning("local clock differs from Binance by %d ms; sync Windows time", offset)
            self._skew_warned_at_s = t1
        (logger.info if first else logger.debug)(
            "server time synced with %s: offset %+d ms (round trip %.0f ms)", self.base_url, offset, (t1 - t0) * 1000
        )
        return offset

    def set_time_offset(self, offset_ms: int) -> None:
        """Set the clock offset directly (tests) and mark it as synced now."""
        self._offset_ms = int(offset_ms)
        self._synced_at_s = self._clock()

    @property
    def time_offset_ms(self) -> int | None:
        return self._offset_ms

    def server_time_ms(self) -> int:
        """Estimated Binance server time (local clock + offset); no HTTP."""
        return now_ms(self._clock) + (self._offset_ms or 0)

    def timestamp_ms(self) -> int:
        """``server_time_ms()``, re-syncing first if never synced or older than ``resync_interval_sec``."""
        if (
            self._offset_ms is None
            or self._synced_at_s is None
            or self._clock() - self._synced_at_s > self.resync_interval_sec
        ):
            self.sync_time()
        return self.server_time_ms()

    # -- public API ---------------------------------------------------------------------------

    def public_get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """Unauthenticated GET (market data). Returns the parsed JSON."""
        return self._request("GET", path, params, signed=False)

    def signed_request(
        self,
        method: Literal["GET", "POST", "PUT", "DELETE"],
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """Signed (USER_DATA/TRADE) request. ``AuthError`` before any I/O when no credentials are configured."""
        verb = str(method).upper()
        if verb not in HTTP_METHODS:
            raise ValueError(f"unsupported HTTP method {method!r}")
        if not self.has_credentials:
            raise AuthError("no API credentials configured (paper mode never signs)", path=_path_only(path))
        return self._request(verb, path, params, signed=True)

    # -- internals ----------------------------------------------------------------------------

    def _request(self, method: str, path: str, params: Mapping[str, Any] | None, *, signed: bool) -> Any:
        if not isinstance(path, str) or not path.startswith("/") or "?" in path:
            raise ValueError(f"path must start with '/' and carry no query string: {path!r}")
        if signed:
            # Make sure the offset is fresh BEFORE anything is sent (a sync failure means nothing was sent).
            self.timestamp_ms()
        retries = 0
        timestamp_resynced = False
        while True:
            self._weight_guard()
            try:
                return self._send(method, path, params, signed=signed)
            except IpBannedError:
                raise
            except RateLimitError as exc:
                if method == "POST" or retries >= self.max_retries:
                    raise
                retry_after = exc.retry_after if exc.retry_after is not None else RATE_LIMIT_DEFAULT_RETRY_AFTER
                delay = min(retry_after, RATE_LIMIT_MAX_SLEEP)
                retries += 1
                logger.warning(
                    "%s %s rate limited (HTTP %s, code %s); retry %d/%d in %.1f s",
                    method, path, exc.http_status, exc.code, retries, self.max_retries, delay,
                )
                self._sleep(delay)
            except TimestampError as exc:
                if timestamp_resynced or retries >= self.max_retries:
                    raise
                timestamp_resynced = True
                retries += 1
                logger.warning("%s %s rejected with timestamp error %s; resyncing server time and retrying once",
                               method, path, exc.code)
                self.sync_time()
            except UnknownOrderStatusError:
                raise
            except TransientError as exc:
                if method == "POST" and not exc.not_executed:
                    raise
                if retries >= self.max_retries:
                    raise
                delay = BACKOFF_SECONDS[min(retries, len(BACKOFF_SECONDS) - 1)]
                retries += 1
                logger.warning(
                    "%s %s transient failure (%s); retry %d/%d in %.1f s",
                    method, path, exc, retries, self.max_retries, delay,
                )
                self._sleep(delay)

    def _send(self, method: str, path: str, params: Mapping[str, Any] | None, *, signed: bool) -> Any:
        headers = {"User-Agent": USER_AGENT}
        if signed:
            p: dict[str, Any] = {
                str(k): v for k, v in (params or {}).items() if v is not None and str(k) not in _SIGNED_RESERVED
            }
            p["recvWindow"] = self.recv_window_ms
            p["timestamp"] = self.timestamp_ms()
            query = encode_params(p)
            signature = hmac_sha256_signature(self._api_secret, query)
            url = f"{self.base_url}{path}?{query}&signature={signature}"
            headers["X-MBX-APIKEY"] = self._api_key
        else:
            query = encode_params(params)
            url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"

        failure: ExchangeError | None = None
        response: requests.Response | None = None
        try:
            response = self._session.request(method, url, headers=headers, timeout=self.timeout)
        except requests.exceptions.ConnectTimeout:
            # Never connected: the request cannot have been executed (safe to retry for every method).
            failure = TransientError(f"{method} {path}: connect timeout", path=path, not_executed=True)
        except _INVALID_REQUEST_EXCEPTIONS:
            failure = ExchangeError(f"{method} {path}: invalid request", path=path)
        except requests.exceptions.RequestException as exc:
            what = _describe_request_exception(exc)
            if method == "POST":
                failure = UnknownOrderStatusError(f"{method} {path}: {what}; outcome unknown", path=path)
            else:
                failure = TransientError(f"{method} {path}: {what}", path=path)
        if failure is not None:
            # Raised outside the except block and ``from None``: the requests exception (full signed URL in its
            # text) is neither the __cause__ nor the __context__.
            raise failure from None
        assert response is not None
        return self._handle_response(method, path, response)

    def _handle_response(self, method: str, path: str, response: requests.Response) -> Any:
        status = int(response.status_code)
        self._update_rate_limits(response.headers)
        logger.debug("%s %s -> %d (w=%s)", method, path, status, self.used_weight_1m)
        is_json = True
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text
            is_json = False
        if 200 <= status < 300:
            code = _error_code(payload)
            if code is None or code >= 0:  # {"code": 200, "msg": "success"} is a success
                if is_json:
                    return payload
                # SPEC-GAP: a 2xx response without a JSON body (proxy/captive page). Nothing usable was returned.
                if method == "POST":
                    raise UnknownOrderStatusError(
                        f"{method} {path}: non-JSON success response; outcome unknown", http_status=status, path=path
                    )
                raise TransientError(f"{method} {path}: non-JSON response", http_status=status, path=path)
        raise map_error(status, payload, path=path, method=method, headers=response.headers)

    def _update_rate_limits(self, headers: Mapping[str, str]) -> None:
        parsed = parse_rate_limit_headers(headers)
        if "used-weight-1m" in parsed:
            self.used_weight_1m = parsed["used-weight-1m"]
            self._weight_seen_ms = self.server_time_ms()
        if "order-count-10s" in parsed:
            self.order_count_10s = parsed["order-count-10s"]
        if "order-count-1m" in parsed:
            self.order_count_1m = parsed["order-count-1m"]

    def _weight_guard(self) -> None:
        """Pause until the next (server) minute when >= 80 % of the 1-minute weight is used this minute."""
        used = self.used_weight_1m
        if used is None or used < self.weight_limit * WEIGHT_GUARD_FRACTION:
            return
        now = self.server_time_ms()
        minute_start = floor_time(now, _MINUTE_MS)
        seen = self._weight_seen_ms
        if seen is not None and seen >= minute_start:
            delay = (minute_start + _MINUTE_MS - now) / 1000.0 + WEIGHT_GUARD_MARGIN_SEC
            logger.warning(
                "request weight %d of %d used this minute; pausing %.1f s until the next minute",
                used, self.weight_limit, delay,
            )
            self._sleep(delay)
        self.used_weight_1m = None
