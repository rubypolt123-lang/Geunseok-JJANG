"""Public market data (SPEC §6.3): klines, server time, exchange filters, funding, mark price.

All frames follow the candle DataFrame convention of §4.1 (``KLINE_COLUMNS`` / ``KLINE_DTYPES``, int64 ms times,
``RangeIndex``, ascending unique ``open_time``). ``MarketData.klines`` returns the forming candle too;
``split_closed`` separates it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

import pandas as pd

from bot.errors import DataError
from bot.exchange.filters import parse_symbol_filters
from bot.exchange.rest import BinanceRestClient
from bot.models import KLINE_COLUMNS, KLINE_DTYPES, Candle, SymbolFilters, candles_from_df
from bot.timeutil import interval_to_ms

logger = logging.getLogger(__name__)

KLINES_PATH: Final = "/fapi/v1/klines"
EXCHANGE_INFO_PATH: Final = "/fapi/v1/exchangeInfo"
FUNDING_RATE_PATH: Final = "/fapi/v1/fundingRate"
PREMIUM_INDEX_PATH: Final = "/fapi/v1/premiumIndex"

KLINES_MAX_LIMIT: Final = 1500
FUNDING_PAGE_LIMIT: Final = 1000

FUNDING_COLUMNS: Final = ("funding_time", "funding_rate", "mark_price")
FUNDING_DTYPES: Final = {"funding_time": "int64", "funding_rate": "float64", "mark_price": "float64"}

_KLINE_FIELD_COUNT: Final = len(KLINE_COLUMNS)  # 11 used fields; field 11 ("ignore") is dropped


def empty_klines_df() -> pd.DataFrame:
    """An empty candle frame with the exact columns/dtypes."""
    return pd.DataFrame({c: pd.Series(dtype=KLINE_DTYPES[c]) for c in KLINE_COLUMNS})


def empty_funding_df() -> pd.DataFrame:
    """An empty funding frame (``funding_time`` int64, ``funding_rate``/``mark_price`` float64)."""
    return pd.DataFrame({c: pd.Series(dtype=FUNDING_DTYPES[c]) for c in FUNDING_COLUMNS})


def klines_to_df(rows: list[list[Any]]) -> pd.DataFrame:
    """Binance kline rows (12 fields, numbers as strings) -> candle DataFrame.

    Drops field 11 ("ignore"), converts with ``pd.to_numeric``, sorts by ``open_time`` and drops duplicate
    ``open_time`` values (keeping the last one received). Empty input -> empty frame with the right dtypes.
    """
    if rows is None or len(rows) == 0:
        return empty_klines_df()
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise DataError(f"klines must be a list of rows, got {type(rows).__name__}")
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < _KLINE_FIELD_COUNT:
            raise DataError(f"malformed kline row (expected >= {_KLINE_FIELD_COUNT} fields): {row!r}"[:200])
    data = {col: [row[i] for row in rows] for i, col in enumerate(KLINE_COLUMNS)}
    try:
        df = pd.DataFrame({col: pd.to_numeric(pd.Series(values), errors="raise") for col, values in data.items()})
        df = df.astype(KLINE_DTYPES)
    except (TypeError, ValueError) as exc:
        raise DataError(f"malformed kline values: {exc}") from None
    df = df.loc[:, list(KLINE_COLUMNS)]
    df = df.sort_values("open_time", kind="stable").drop_duplicates("open_time", keep="last")
    return df.reset_index(drop=True)


def split_closed(df: pd.DataFrame, server_time_ms: int) -> tuple[pd.DataFrame, Candle | None]:
    """Split into closed candles (``close_time < server_time_ms``) and the forming candle.

    forming = the last row if ``open_time <= server_time_ms <= close_time``, else None.
    """
    now = int(server_time_ms)
    if df is None or len(df) == 0:
        return (empty_klines_df() if df is None else df.reset_index(drop=True)), None
    closed = df.loc[df["close_time"] < now].reset_index(drop=True)
    last_open = int(df["open_time"].iloc[-1])
    last_close = int(df["close_time"].iloc[-1])
    forming: Candle | None = None
    if last_open <= now <= last_close:
        forming = candles_from_df(df.iloc[[-1]])[0]
    return closed, forming


def funding_rows_to_df(rows: list[Mapping[str, Any]]) -> pd.DataFrame:
    """``/fapi/v1/fundingRate`` rows -> frame (``mark_price`` "" -> NaN), sorted by time, deduplicated."""
    if not rows:
        return empty_funding_df()
    try:
        times = [int(r["fundingTime"]) for r in rows]
        rates = pd.to_numeric(pd.Series([r["fundingRate"] for r in rows]), errors="raise")
        marks = pd.to_numeric(pd.Series([r.get("markPrice") for r in rows]), errors="coerce")
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError(f"malformed funding rate rows: {exc!r}") from None
    df = pd.DataFrame(
        {"funding_time": pd.Series(times, dtype="int64"), "funding_rate": rates, "mark_price": marks}
    ).astype(FUNDING_DTYPES)
    df = df.sort_values("funding_time", kind="stable").drop_duplicates("funding_time", keep="last")
    return df.reset_index(drop=True)


def _as_float(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise DataError(f"invalid {name} in premiumIndex response: {value!r}") from None
    return result


def _as_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise DataError(f"invalid {name} in premiumIndex response: {value!r}") from None


class MarketData:
    """Public market data through a ``BinanceRestClient`` (no credentials needed)."""

    def __init__(self, client: BinanceRestClient, *, filters_ttl_sec: float = 3600.0) -> None:
        self.client = client
        self.filters_ttl_sec = float(filters_ttl_sec)
        self._filters_cache: dict[str, tuple[float, SymbolFilters]] = {}

    def _now_s(self) -> float:
        clock: Callable[[], float] = getattr(self.client, "clock", None) or time.time
        return float(clock())

    # -- time / exchange info -----------------------------------------------------------------

    def server_time(self) -> int:
        """Re-sync the client clock and return the estimated server time (ms)."""
        self.client.sync_time()
        return self.client.server_time_ms()

    def exchange_info(self) -> dict:
        """``GET /fapi/v1/exchangeInfo``; updates ``client.weight_limit`` from the REQUEST_WEIGHT/MINUTE/1 limit."""
        info = self.client.public_get(EXCHANGE_INFO_PATH)
        if not isinstance(info, dict):
            raise DataError("exchangeInfo response is not an object")
        for rl in info.get("rateLimits") or []:
            if not isinstance(rl, Mapping):
                continue
            try:
                interval_num = int(rl.get("intervalNum", 0))
            except (TypeError, ValueError):
                continue
            if rl.get("rateLimitType") == "REQUEST_WEIGHT" and rl.get("interval") == "MINUTE" and interval_num == 1:
                try:
                    limit = int(rl["limit"])
                except (KeyError, TypeError, ValueError):
                    continue
                if limit > 0 and limit != self.client.weight_limit:
                    logger.info("request weight limit of %s: %d/min", self.client.base_url, limit)
                if limit > 0:
                    self.client.weight_limit = limit
        return info

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        """Filters of ``symbol`` from exchangeInfo of the client's host, cached for ``filters_ttl_sec``."""
        now = self._now_s()
        cached = self._filters_cache.get(symbol)
        if cached is not None and 0 <= now - cached[0] < self.filters_ttl_sec:
            return cached[1]
        info = self.exchange_info()
        for entry in info.get("symbols") or []:
            if isinstance(entry, Mapping) and entry.get("symbol") == symbol:
                filters = parse_symbol_filters(dict(entry))
                self._filters_cache[symbol] = (now, filters)
                return filters
        raise DataError(f"symbol {symbol} not found in exchangeInfo of {self.client.base_url}")

    # -- klines ---------------------------------------------------------------------------------

    def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        """``GET /fapi/v1/klines`` (limit clamped to 1..1500). INCLUDES the forming candle."""
        interval_to_ms(interval)  # ConfigError for unsupported intervals, before any request
        lim = max(1, min(KLINES_MAX_LIMIT, int(limit)))
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": None if start_ms is None else int(start_ms),
            "endTime": None if end_ms is None else int(end_ms),
            "limit": lim,
        }
        rows = self.client.public_get(KLINES_PATH, params)
        if not isinstance(rows, list):
            raise DataError("klines response is not a list")
        return klines_to_df(rows)

    def recent_klines(self, symbol: str, interval: str, limit: int) -> tuple[pd.DataFrame, Candle | None, int]:
        """Re-sync the clock, fetch ``limit + 1`` klines and split them: ``(closed, forming, server_now)``."""
        server_now = self.server_time()
        df = self.klines(symbol, interval, limit=int(limit) + 1)
        closed, forming = split_closed(df, server_now)
        return closed, forming, server_now

    # -- funding / mark price -------------------------------------------------------------------

    def funding_rates(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """``GET /fapi/v1/fundingRate`` paginated (limit 1000; next ``startTime`` = last ``fundingTime`` + 1).

        Columns ``funding_time`` (int64), ``funding_rate``, ``mark_price`` (float64; "" in old rows -> NaN).
        """
        start = int(start_ms)
        end = int(end_ms)
        rows: list[Mapping[str, Any]] = []
        cur = start
        while cur <= end:
            page = self.client.public_get(
                FUNDING_RATE_PATH,
                {"symbol": symbol, "startTime": cur, "endTime": end, "limit": FUNDING_PAGE_LIMIT},
            )
            if not isinstance(page, list):
                raise DataError("fundingRate response is not a list")
            rows.extend(r for r in page if isinstance(r, Mapping))
            if len(page) < FUNDING_PAGE_LIMIT:
                break
            try:
                last = max(int(r["fundingTime"]) for r in page)
            except (KeyError, TypeError, ValueError):
                raise DataError("fundingRate rows lack fundingTime") from None
            if last + 1 <= cur:  # no progress: never loop forever on a misbehaving response
                break
            cur = last + 1
        df = funding_rows_to_df(rows)
        if len(df):
            df = df.loc[(df["funding_time"] >= start) & (df["funding_time"] <= end)].reset_index(drop=True)
        return df

    def _premium_index_raw(self, symbol: str) -> Mapping[str, Any]:
        data = self.client.public_get(PREMIUM_INDEX_PATH, {"symbol": symbol})
        if isinstance(data, list):  # defensive: the no-symbol variant returns a list
            data = next((d for d in data if isinstance(d, Mapping) and d.get("symbol") == symbol), None)
        if not isinstance(data, Mapping):
            raise DataError(f"premiumIndex response for {symbol} is not an object")
        return data

    def mark_price(self, symbol: str) -> float:
        """Current mark price (``GET /fapi/v1/premiumIndex?symbol=``)."""
        return _as_float(self._premium_index_raw(symbol).get("markPrice"), "markPrice")

    def premium_index(self, symbol: str) -> dict[str, float | int]:
        """``{"mark_price", "last_funding_rate", "next_funding_time", "time"}`` from ``premiumIndex``."""
        d = self._premium_index_raw(symbol)
        return {
            "mark_price": _as_float(d.get("markPrice"), "markPrice"),
            "last_funding_rate": _as_float(d.get("lastFundingRate"), "lastFundingRate"),
            "next_funding_time": _as_int(d.get("nextFundingTime"), "nextFundingTime"),
            "time": _as_int(d.get("time"), "time"),
        }
