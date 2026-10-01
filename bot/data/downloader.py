"""Historical data cache: mainnet klines, funding rates and symbol filters (SPEC §7).

Cache layout under ``cfg.cache_dir``:
- ``klines/{SYMBOL}_{interval}.csv``  — header row, ``KLINE_COLUMNS``, closed candles only
- ``funding/{SYMBOL}.csv``            — ``funding_time,funding_rate,mark_price``
- ``exchange_info/{SYMBOL}.json``     — ``SymbolFilters.to_dict()`` + ``{"fetched_at": ms, "host": base_url}``

Downloads are incremental (only missing ranges are fetched), never forward-fill gaps and write atomically
(``<file>.tmp`` + ``bot.fsutil.atomic_replace``). Backtests always use mainnet public data.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd

from bot.config import MAINNET_REST_URL
from bot.errors import ConfigError, DataError, ExchangeError
from bot.exchange.market import (
    FUNDING_COLUMNS,
    FUNDING_DTYPES,
    KLINES_MAX_LIMIT,
    MarketData,
    empty_funding_df,
    empty_klines_df,
)
from bot.fsutil import atomic_replace, atomic_write_text, tmp_path_for
from bot.models import KLINE_COLUMNS, KLINE_DTYPES, SymbolFilters, validate_candles_df
from bot.timeutil import floor_time, interval_to_ms, ms_to_iso, now_ms

logger = logging.getLogger(__name__)

_SYMBOL_PATH_RE: Final = re.compile(r"^[A-Z0-9]{2,30}$")
_DOWNLOAD_HINT: Final = "python -m bot download --symbol {symbol}"


# ---------------------------------------------------------------------------------------------
# Cache paths
# ---------------------------------------------------------------------------------------------


def _check_symbol(symbol: str) -> str:
    # The symbol becomes part of a file name: only plain upper-case alphanumerics (no path tricks).
    if not isinstance(symbol, str) or not _SYMBOL_PATH_RE.fullmatch(symbol):
        raise ValueError(f"invalid symbol for a cache path: {symbol!r}")
    return symbol


def klines_cache_path(cache_dir: Path, symbol: str, interval: str) -> Path:
    """``<cache_dir>/klines/{SYMBOL}_{interval}.csv``."""
    interval_to_ms(interval)  # ConfigError for unsupported intervals
    return Path(cache_dir) / "klines" / f"{_check_symbol(symbol)}_{interval}.csv"


def funding_cache_path(cache_dir: Path, symbol: str) -> Path:
    """``<cache_dir>/funding/{SYMBOL}.csv``."""
    return Path(cache_dir) / "funding" / f"{_check_symbol(symbol)}.csv"


def filters_cache_path(cache_dir: Path, symbol: str) -> Path:
    """``<cache_dir>/exchange_info/{SYMBOL}.json``."""
    return Path(cache_dir) / "exchange_info" / f"{_check_symbol(symbol)}.json"


# ---------------------------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------------------------


def _write_csv_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(path)
    try:
        df.to_csv(tmp, index=False, encoding="utf-8")
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    atomic_replace(tmp, path)


def _read_csv(path: Path, columns: tuple[str, ...], dtypes: dict[str, str], what: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(path, dtype=dtypes, encoding="utf-8", float_precision="round_trip")
    except (ValueError, TypeError, pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as exc:
        raise DataError(f"cannot read {what} cache {path}: {exc}; delete the file and download again") from None
    if list(df.columns) != list(columns):
        raise DataError(
            f"{what} cache {path} has columns {list(df.columns)}, expected {list(columns)}; "
            "delete the file and download again"
        )
    try:
        return df.astype(dtypes)
    except (TypeError, ValueError) as exc:
        raise DataError(f"{what} cache {path} has invalid values: {exc}") from None


def _merge(frames: list[pd.DataFrame], key: str, dtypes: dict[str, str]) -> pd.DataFrame:
    """Concatenate (later frames win on duplicate ``key``), sort ascending, RangeIndex, exact dtypes."""
    parts = [f for f in frames if f is not None and len(f)]
    if not parts:
        return pd.DataFrame({c: pd.Series(dtype=t) for c, t in dtypes.items()})
    merged = pd.concat([p.astype(dtypes) for p in parts], ignore_index=True)
    merged = merged.drop_duplicates(key, keep="last").sort_values(key, kind="stable")
    return merged.reset_index(drop=True).astype(dtypes)


def _filter_range(df: pd.DataFrame, key: str, start_ms: int | None, end_ms: int | None) -> pd.DataFrame:
    if start_ms is not None:
        df = df.loc[df[key] >= int(start_ms)]
    if end_ms is not None:
        df = df.loc[df[key] <= int(end_ms)]
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------------------------
# Klines
# ---------------------------------------------------------------------------------------------


def load_klines(
    cache_dir: Path,
    symbol: str,
    interval: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> pd.DataFrame:
    """Cached klines with ``start_ms <= open_time <= end_ms``; missing file -> empty frame (exact dtypes)."""
    path = klines_cache_path(cache_dir, symbol, interval)
    if not path.exists():
        return empty_klines_df()
    df = _read_csv(path, KLINE_COLUMNS, dict(KLINE_DTYPES), "kline")
    try:
        validate_candles_df(df, interval_to_ms(interval))
    except DataError as exc:
        raise DataError(f"kline cache {path} is corrupted ({exc}); delete the file and download again") from None
    return _filter_range(df, "open_time", start_ms, end_ms)


def find_gaps(df: pd.DataFrame, interval_ms: int) -> list[tuple[int, int]]:
    """``(prev_open_time, next_open_time)`` for every consecutive pair whose distance != ``interval_ms``."""
    if df is None or len(df) < 2:
        return []
    open_time = df["open_time"].to_numpy(dtype=np.int64)
    diffs = np.diff(open_time)
    idx = np.nonzero(diffs != int(interval_ms))[0]
    return [(int(open_time[k]), int(open_time[k + 1])) for k in idx]


def download_klines(
    market: MarketData,
    cache_dir: Path,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int | None = None,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> pd.DataFrame:
    """Incrementally download closed klines into the cache and return ``load_klines(..., start_ms, end_ms)``."""
    path = klines_cache_path(cache_dir, symbol, interval)
    server_now = int(market.server_time())
    end = min(int(end_ms), server_now) if end_ms is not None else server_now
    i = interval_to_ms(interval)
    start = floor_time(int(start_ms), i)

    cached = load_klines(cache_dir, symbol, interval)
    ranges: list[tuple[int, int]] = []
    if cached.empty:
        if start <= end:
            ranges.append((start, end))
    else:
        cached_min = int(cached["open_time"].iloc[0])
        cached_max = int(cached["open_time"].iloc[-1])
        if start < cached_min:
            ranges.append((start, cached_min - i))
        if cached_max + i <= end:
            ranges.append((cached_max + i, end))

    expected_rows = sum((range_end - range_start) // i + 1 for range_start, range_end in ranges)
    rows_so_far = 0
    fetched: list[pd.DataFrame] = []
    for range_start, range_end in ranges:
        logger.info("downloading %s %s klines %s .. %s", symbol, interval, ms_to_iso(range_start), ms_to_iso(range_end))
        cur = range_start
        while cur <= range_end:
            page = market.klines(symbol, interval, start_ms=cur, end_ms=range_end, limit=KLINES_MAX_LIMIT)
            n_raw = len(page)
            if n_raw == 0:
                break
            last_open = int(page["open_time"].iloc[-1])
            closed = page.loc[page["close_time"] < server_now]  # the open candle is never cached
            if len(closed):
                fetched.append(closed)
                rows_so_far += len(closed)
            if progress is not None:
                progress(rows_so_far, max(expected_rows, rows_so_far))
            next_cur = last_open + i
            if n_raw < KLINES_MAX_LIMIT or next_cur <= cur:
                break
            cur = next_cur

    if fetched:
        merged = _merge([cached, *fetched], "open_time", dict(KLINE_DTYPES))
        merged = merged.loc[:, list(KLINE_COLUMNS)]
        validate_candles_df(merged, i)
        _write_csv_atomic(merged, path)
        logger.info("%s %s: %d new closed candles, %d cached in %s", symbol, interval, rows_so_far, len(merged), path)
        gaps = find_gaps(merged, i)
        if gaps:
            logger.warning("%d gaps in %s %s (exchange maintenance?)", len(gaps), symbol, interval)
            for prev_open, next_open in gaps[:10]:
                logger.debug("gap %s -> %s", ms_to_iso(prev_open), ms_to_iso(next_open))
    else:
        logger.info("%s %s: kline cache is up to date (%d rows)", symbol, interval, len(cached))
    return load_klines(cache_dir, symbol, interval, start_ms, end_ms)


# ---------------------------------------------------------------------------------------------
# Funding
# ---------------------------------------------------------------------------------------------


def load_funding(cache_dir: Path, symbol: str, start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame:
    """Cached funding rates with ``start_ms <= funding_time <= end_ms``; missing file -> empty frame."""
    path = funding_cache_path(cache_dir, symbol)
    if not path.exists():
        return empty_funding_df()
    df = _read_csv(path, FUNDING_COLUMNS, dict(FUNDING_DTYPES), "funding")
    times = df["funding_time"]
    if not times.is_unique or not times.is_monotonic_increasing:
        raise DataError(f"funding cache {path} is not sorted/unique; delete the file and download again")
    return _filter_range(df, "funding_time", start_ms, end_ms)


def download_funding(
    market: MarketData,
    cache_dir: Path,
    symbol: str,
    start_ms: int,
    end_ms: int | None = None,
) -> pd.DataFrame:
    """Incrementally download funding rates (next start = last ``funding_time`` + 1) and return the range."""
    path = funding_cache_path(cache_dir, symbol)
    server_now = int(market.server_time())
    end = min(int(end_ms), server_now) if end_ms is not None else server_now
    start = int(start_ms)

    cached = load_funding(cache_dir, symbol)
    ranges: list[tuple[int, int]] = []
    if cached.empty:
        if start <= end:
            ranges.append((start, end))
    else:
        cached_min = int(cached["funding_time"].iloc[0])
        cached_max = int(cached["funding_time"].iloc[-1])
        if start < cached_min:
            ranges.append((start, cached_min - 1))
        if cached_max + 1 <= end:
            ranges.append((cached_max + 1, end))

    fetched: list[pd.DataFrame] = []
    for range_start, range_end in ranges:
        logger.info("downloading %s funding rates %s .. %s", symbol, ms_to_iso(range_start), ms_to_iso(range_end))
        page = market.funding_rates(symbol, range_start, range_end)
        if len(page):
            fetched.append(page.loc[:, list(FUNDING_COLUMNS)])

    if fetched:
        merged = _merge([cached, *fetched], "funding_time", dict(FUNDING_DTYPES))
        merged = merged.loc[:, list(FUNDING_COLUMNS)]
        _write_csv_atomic(merged, path)
        logger.info("%s: %d funding rows cached in %s", symbol, len(merged), path)
    else:
        logger.info("%s: funding cache is up to date (%d rows)", symbol, len(cached))
    return load_funding(cache_dir, symbol, start_ms, end_ms)


# ---------------------------------------------------------------------------------------------
# Exchange filters
# ---------------------------------------------------------------------------------------------


def _read_filters_cache(path: Path) -> tuple[SymbolFilters, int | None, str | None] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise DataError("not a JSON object")
        filters = SymbolFilters.from_dict(data)
    except (OSError, ValueError, DataError) as exc:
        logger.warning("ignoring unreadable exchange filter cache %s: %s", path, exc)
        return None
    fetched_at: int | None
    try:
        fetched_at = int(data["fetched_at"])
    except (KeyError, TypeError, ValueError):
        fetched_at = None
    host = data.get("host")
    return filters, fetched_at, (str(host) if host is not None else None)


def _market_host(market: Any) -> str:
    """REST host behind ``market`` (``market.client.base_url``); objects without one count as mainnet."""
    client = getattr(market, "client", None)
    host = getattr(client, "base_url", None)
    return host.strip().rstrip("/") if isinstance(host, str) and host.strip() else MAINNET_REST_URL


def load_or_fetch_filters(
    market: MarketData | None,
    cache_dir: Path,
    symbol: str,
    *,
    max_age_sec: float = 86400.0,
) -> SymbolFilters:
    """Mainnet symbol filters for backtests.

    Fresh cache -> it; else with ``market``: fetch (mainnet host) and rewrite the cache; else a stale cache with a
    warning; else ``DataError("no cached exchange filters; run: python -m bot download ...")``.
    """
    path = filters_cache_path(cache_dir, symbol)
    cached = _read_filters_cache(path) if path.exists() else None
    now = now_ms()
    if cached is not None:
        filters, fetched_at, host = cached
        foreign_host = host is not None and host.rstrip("/") != MAINNET_REST_URL
        if foreign_host:
            # Never trusted as "fresh": refreshed from mainnet when possible, else used with the stale warning.
            logger.warning("exchange filter cache %s was fetched from %s, not mainnet", path, host)
        if not foreign_host and fetched_at is not None and 0 <= now - fetched_at < max_age_sec * 1000:
            return filters

    if market is not None:
        host = _market_host(market)
        if host != MAINNET_REST_URL:
            raise ConfigError(
                f"exchange filters for backtests must come from mainnet ({MAINNET_REST_URL}), got {host}"
            )
        try:
            fresh = market.symbol_filters(symbol)
        except ExchangeError as exc:
            if cached is None:
                raise
            logger.warning("could not refresh exchange filters for %s (%s); using the cached ones from %s",
                           symbol, exc, path)
            return cached[0]
        payload = fresh.to_dict() | {"fetched_at": now, "host": host}
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        logger.info("exchange filters for %s cached in %s", symbol, path)
        return fresh

    if cached is not None:
        age_h = (now - cached[1]) / 3_600_000 if cached[1] is not None else float("nan")
        logger.warning("using stale cached exchange filters for %s (age %.1f h) from %s", symbol, age_h, path)
        return cached[0]
    raise DataError(
        f"no cached exchange filters; run: {_DOWNLOAD_HINT.format(symbol=symbol)} "
        f"({symbol} 의 캐시된 거래 규칙이 없습니다. 먼저 download 를 실행하세요)"
    )
