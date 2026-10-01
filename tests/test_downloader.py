"""Tests for bot.data.downloader (SPEC §7, §14.2 U2). A fake MarketData serves synthetic history; no network."""

from __future__ import annotations

import csv
import json
import logging
import math
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from bot.config import MAINNET_REST_URL, TESTNET_REST_URL
from bot.errors import ConfigError, DataError, TransientError
from bot.data.downloader import (
    download_funding,
    download_klines,
    filters_cache_path,
    find_gaps,
    funding_cache_path,
    klines_cache_path,
    load_funding,
    load_klines,
    load_or_fetch_filters,
)
from bot.models import KLINE_COLUMNS, KLINE_DTYPES, SymbolFilters, validate_candles_df
from bot.timeutil import now_ms

H = 3_600_000
EIGHT_H = 8 * H
START = 1_704_067_200_000  # 2024-01-01T00:00:00Z
SYMBOL = "BTCUSDT"


class FakeMarket:
    """Behaves like ``MarketData`` for the downloader: Binance kline/funding range semantics, call log."""

    def __init__(
        self,
        history: pd.DataFrame,
        server_now: int,
        *,
        funding: pd.DataFrame | None = None,
        filters: SymbolFilters | None = None,
        host: str = MAINNET_REST_URL,
        overlap_bars: int = 0,
    ) -> None:
        self.history = history.reset_index(drop=True)
        self.server_now = int(server_now)
        self.funding = funding
        self.filters = filters
        self.client = SimpleNamespace(base_url=host)
        self.overlap_bars = overlap_bars  # misbehaving exchange: also return bars before startTime
        self.kline_calls: list[dict[str, Any]] = []
        self.funding_calls: list[tuple[int, int]] = []
        self.filter_calls = 0

    def server_time(self) -> int:
        return self.server_now

    def klines(self, symbol: str, interval: str, *, start_ms: int | None = None, end_ms: int | None = None,
               limit: int = 500) -> pd.DataFrame:
        self.kline_calls.append({"symbol": symbol, "interval": interval, "start_ms": start_ms, "end_ms": end_ms,
                                 "limit": limit})
        df = self.history
        # only bars that exist "now" (open_time <= server time); the last one may still be forming
        df = df.loc[df["open_time"] <= self.server_now]
        lo = start_ms - self.overlap_bars * H if start_ms is not None else None
        if lo is not None:
            df = df.loc[df["open_time"] >= lo]
        if end_ms is not None:
            df = df.loc[df["open_time"] <= end_ms]
        return df.head(limit).reset_index(drop=True)

    def funding_rates(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        self.funding_calls.append((int(start_ms), int(end_ms)))
        assert self.funding is not None
        f = self.funding
        f = f.loc[(f["funding_time"] >= start_ms) & (f["funding_time"] <= end_ms) & (f["funding_time"] <= self.server_now)]
        return f.reset_index(drop=True)

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        self.filter_calls += 1
        if self.filters is None:
            raise TransientError("exchange unavailable", path="/fapi/v1/exchangeInfo")
        return self.filters


def _history(candle_factory: Callable[..., pd.DataFrame], n: int, start_ms: int = START) -> pd.DataFrame:
    closes = [100.0 + 0.1 * (k % 50) + 0.1 + 0.2 for k in range(n)]  # 0.1 + 0.2 -> exercises float round trips
    return candle_factory(closes, start_ms=start_ms, interval="1h")


def _funding_frame(n: int, start_ms: int = START) -> pd.DataFrame:
    times = [start_ms + k * EIGHT_H for k in range(n)]
    marks = [float("nan") if k == 0 else 42000.0 + k for k in range(n)]
    return pd.DataFrame({
        "funding_time": pd.Series(times, dtype="int64"),
        "funding_rate": pd.Series([0.0001 * ((-1) ** k) for k in range(n)], dtype="float64"),
        "mark_price": pd.Series(marks, dtype="float64"),
    })


# ---------------------------------------------------------------------------------------------
# klines
# ---------------------------------------------------------------------------------------------


def test_cache_paths(tmp_path: Path) -> None:
    assert klines_cache_path(tmp_path, SYMBOL, "1h") == tmp_path / "klines" / "BTCUSDT_1h.csv"
    assert funding_cache_path(tmp_path, SYMBOL) == tmp_path / "funding" / "BTCUSDT.csv"
    assert filters_cache_path(tmp_path, SYMBOL) == tmp_path / "exchange_info" / "BTCUSDT.json"
    with pytest.raises(ValueError):
        funding_cache_path(tmp_path, "..\\evil")
    with pytest.raises(ConfigError):
        klines_cache_path(tmp_path, SYMBOL, "1w")


def test_download_paginates_with_start_plus_interval(
    tmp_path: Path, candle_factory: Callable[..., pd.DataFrame]
) -> None:
    hist = _history(candle_factory, 3201)  # 3200 closed bars + 1 forming bar
    server_now = START + 3200 * H + 1_800_000  # 30 min into the last bar
    market = FakeMarket(hist, server_now)
    progress: list[tuple[int, int]] = []
    df = download_klines(market, tmp_path, SYMBOL, "1h", START + 123, progress=lambda a, b: progress.append((a, b)))

    starts = [c["start_ms"] for c in market.kline_calls]
    assert starts == [START, START + 1500 * H, START + 3000 * H]  # aligned start, then last open + interval
    assert all(c["limit"] == 1500 for c in market.kline_calls)
    assert all(c["end_ms"] == server_now for c in market.kline_calls)
    assert len(df) == 3200 - 1  # load_klines(start_ms=START+123) excludes the bar opening at START
    full = load_klines(tmp_path, SYMBOL, "1h")
    assert len(full) == 3200
    assert full["open_time"].iloc[0] == START
    assert find_gaps(full, H) == []
    assert [p[0] for p in progress] == [1500, 3000, 3200]
    assert all(total >= done for done, total in progress)


def test_download_writes_csv_and_reloads_same_dtypes(
    tmp_path: Path, candle_factory: Callable[..., pd.DataFrame]
) -> None:
    hist = _history(candle_factory, 25)
    server_now = START + 24 * H + 10  # bar 24 is forming
    market = FakeMarket(hist, server_now)
    df = download_klines(market, tmp_path, SYMBOL, "1h", START)

    path = klines_cache_path(tmp_path, SYMBOL, "1h")
    assert path.exists()
    assert not Path(f"{path}.tmp").exists()
    with open(path, encoding="utf-8", newline="") as fh:
        header = next(csv.reader(fh))
    assert tuple(header) == KLINE_COLUMNS

    reloaded = load_klines(tmp_path, SYMBOL, "1h")
    assert {c: str(reloaded[c].dtype) for c in reloaded.columns} == KLINE_DTYPES
    assert tuple(reloaded.columns) == KLINE_COLUMNS
    assert isinstance(reloaded.index, pd.RangeIndex)
    validate_candles_df(reloaded, H)
    expected = hist.iloc[:24].reset_index(drop=True)
    pd.testing.assert_frame_equal(reloaded, expected, check_exact=True)  # floats round-trip exactly
    pd.testing.assert_frame_equal(df, expected, check_exact=True)

    # range filter on open_time
    part = load_klines(tmp_path, SYMBOL, "1h", START + 2 * H, START + 4 * H)
    assert part["open_time"].tolist() == [START + 2 * H, START + 3 * H, START + 4 * H]
    # missing file -> empty frame with exact dtypes
    empty = load_klines(tmp_path, "ETHUSDT", "1h")
    assert len(empty) == 0 and {c: str(empty[c].dtype) for c in empty.columns} == KLINE_DTYPES


def test_load_klines_rejects_corrupted_cache(tmp_path: Path, candle_factory: Callable[..., pd.DataFrame]) -> None:
    path = klines_cache_path(tmp_path, SYMBOL, "1h")
    path.parent.mkdir(parents=True)
    hist = _history(candle_factory, 3)
    hist.iloc[[0, 2, 1]].to_csv(path, index=False, encoding="utf-8")  # unsorted
    with pytest.raises(DataError):
        load_klines(tmp_path, SYMBOL, "1h")
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(DataError):
        load_klines(tmp_path, SYMBOL, "1h")


def test_incremental_download_fetches_only_new(tmp_path: Path, candle_factory: Callable[..., pd.DataFrame]) -> None:
    hist = _history(candle_factory, 200, start_ms=START - 50 * H)  # exchange history from START - 50h
    market = FakeMarket(hist, START + 100 * H + 5)
    download_klines(market, tmp_path, SYMBOL, "1h", START)
    cached = load_klines(tmp_path, SYMBOL, "1h")
    assert cached["open_time"].iloc[0] == START
    assert cached["open_time"].iloc[-1] == START + 99 * H
    assert len(market.kline_calls) == 1

    # time passes: only [cached_max + i, now] is requested
    market.kline_calls.clear()
    market.server_now = START + 130 * H + 5
    df = download_klines(market, tmp_path, SYMBOL, "1h", START)
    assert [c["start_ms"] for c in market.kline_calls] == [START + 100 * H]
    assert len(df) == 130
    assert find_gaps(df, H) == []

    # nothing new within the same bar: the forming bar is requested but dropped, the cache is not rewritten
    market.kline_calls.clear()
    mtime = klines_cache_path(tmp_path, SYMBOL, "1h").stat().st_mtime_ns
    market.server_now = START + 130 * H + 600_000
    df = download_klines(market, tmp_path, SYMBOL, "1h", START)
    assert [c["start_ms"] for c in market.kline_calls] == [START + 130 * H]
    assert len(df) == 130
    assert klines_cache_path(tmp_path, SYMBOL, "1h").stat().st_mtime_ns == mtime

    # a requested end before the cached end and a start inside the cache: no request at all
    market.kline_calls.clear()
    df = download_klines(market, tmp_path, SYMBOL, "1h", START + 5 * H, START + 50 * H)
    assert market.kline_calls == []
    assert df["open_time"].tolist() == [START + k * H for k in range(5, 51)]

    # an earlier start fetches only [start, cached_min - i]
    df = download_klines(market, tmp_path, SYMBOL, "1h", START - 10 * H, START + 5 * H)
    assert [(c["start_ms"], c["end_ms"]) for c in market.kline_calls] == [(START - 10 * H, START - H)]
    assert df["open_time"].tolist() == [START + k * H for k in range(-10, 6)]
    full = load_klines(tmp_path, SYMBOL, "1h")
    assert len(full) == 140 and full["open_time"].is_unique
    validate_candles_df(full, H)


def test_open_candle_never_cached(tmp_path: Path, candle_factory: Callable[..., pd.DataFrame]) -> None:
    hist = _history(candle_factory, 10)
    forming_open = START + 9 * H
    market = FakeMarket(hist, forming_open + 1)  # bar 9 opened 1 ms ago
    df = download_klines(market, tmp_path, SYMBOL, "1h", START)
    assert forming_open not in df["open_time"].tolist()
    cached = load_klines(tmp_path, SYMBOL, "1h")
    assert int(cached["close_time"].iloc[-1]) < market.server_now
    assert int(cached["open_time"].iloc[-1]) == START + 8 * H

    # once the bar has closed it is fetched and cached
    market.server_now = forming_open + H + 1
    df = download_klines(market, tmp_path, SYMBOL, "1h", START)
    assert int(df["open_time"].iloc[-1]) == forming_open
    assert (df["close_time"] < market.server_now).all()

    # end_ms in the future is clamped to the server time
    market.kline_calls.clear()
    market.server_now = forming_open + 2 * H + 1
    download_klines(market, tmp_path, SYMBOL, "1h", START, forming_open + 100 * H)
    assert market.kline_calls[0]["end_ms"] == market.server_now


def test_dedupe_on_merge(tmp_path: Path, candle_factory: Callable[..., pd.DataFrame]) -> None:
    hist = _history(candle_factory, 40)
    market = FakeMarket(hist, START + 20 * H + 5)
    download_klines(market, tmp_path, SYMBOL, "1h", START + 10 * H)  # cache: bars 10..19

    # the exchange now returns corrected values AND rows overlapping the cached range
    revised = hist.copy()
    revised.loc[:, "volume"] = 7.0
    market.history = revised
    market.overlap_bars = 3
    market.server_now = START + 30 * H + 5
    # fetches [START, 9h] and [20h, now]; the second range also returns bars 17..19 again
    download_klines(market, tmp_path, SYMBOL, "1h", START)
    assert [c["start_ms"] for c in market.kline_calls[-2:]] == [START, START + 20 * H]

    full = load_klines(tmp_path, SYMBOL, "1h")
    assert full["open_time"].tolist() == [START + k * H for k in range(30)]
    assert full["open_time"].is_unique
    # overlapping bars come from the new download (keep new); untouched cached bars keep their old values
    vol = dict(zip(full["open_time"].tolist(), full["volume"].tolist()))
    assert [vol[START + k * H] for k in (17, 18, 19)] == [7.0, 7.0, 7.0]
    assert [vol[START + k * H] for k in range(10, 17)] == [1.0] * 7  # not re-fetched
    assert vol[START] == 7.0 and vol[START + 25 * H] == 7.0  # new bars
    validate_candles_df(full, H)


def test_find_gaps(candle_factory: Callable[..., pd.DataFrame]) -> None:
    df = _history(candle_factory, 6)
    assert find_gaps(df, H) == []
    holes = df.drop(index=[2, 4]).reset_index(drop=True)
    gaps = find_gaps(holes, H)
    assert gaps == [(START + H, START + 3 * H), (START + 3 * H, START + 5 * H)]
    assert all(type(a) is int and type(b) is int for a, b in gaps)
    assert find_gaps(df.iloc[:1], H) == []
    assert find_gaps(df.iloc[:0], H) == []


def test_download_warns_about_gaps_without_filling(
    tmp_path: Path, candle_factory: Callable[..., pd.DataFrame], caplog: pytest.LogCaptureFixture
) -> None:
    hist = _history(candle_factory, 12).drop(index=[5]).reset_index(drop=True)  # exchange maintenance hole
    market = FakeMarket(hist, START + 11 * H + 5)
    with caplog.at_level(logging.WARNING, logger="bot.data.downloader"):
        df = download_klines(market, tmp_path, SYMBOL, "1h", START)
    assert "1 gaps in BTCUSDT 1h" in caplog.text
    assert len(df) == 10  # never forward-filled
    assert find_gaps(df, H) == [(START + 4 * H, START + 6 * H)]


# ---------------------------------------------------------------------------------------------
# funding
# ---------------------------------------------------------------------------------------------


def test_download_funding_incremental(tmp_path: Path, candle_factory: Callable[..., pd.DataFrame]) -> None:
    funding = _funding_frame(30)
    market = FakeMarket(_history(candle_factory, 2), START + 10 * EIGHT_H + 5, funding=funding)
    df = download_funding(market, tmp_path, SYMBOL, START)
    assert market.funding_calls == [(START, START + 10 * EIGHT_H + 5)]
    assert df["funding_time"].tolist() == [START + k * EIGHT_H for k in range(11)]

    path = funding_cache_path(tmp_path, SYMBOL)
    with open(path, encoding="utf-8", newline="") as fh:
        assert next(csv.reader(fh)) == ["funding_time", "funding_rate", "mark_price"]
    loaded = load_funding(tmp_path, SYMBOL)
    assert {c: str(loaded[c].dtype) for c in loaded.columns} == {
        "funding_time": "int64", "funding_rate": "float64", "mark_price": "float64"}
    assert math.isnan(loaded.loc[0, "mark_price"])  # "" / NaN survives the round trip
    pd.testing.assert_frame_equal(loaded, funding.iloc[:11].reset_index(drop=True), check_exact=True)

    # later: only (last funding_time + 1 .. now] is requested
    market.funding_calls.clear()
    market.server_now = START + 20 * EIGHT_H + 5
    df = download_funding(market, tmp_path, SYMBOL, START)
    assert market.funding_calls == [(START + 10 * EIGHT_H + 1, START + 20 * EIGHT_H + 5)]
    assert len(df) == 21 and df["funding_time"].is_unique and df["funding_time"].is_monotonic_increasing

    # an earlier start fetches [start, cached_min - 1]; end_ms restricts the returned range
    market.funding = _funding_frame(40, start_ms=START - 10 * EIGHT_H)
    market.funding_calls.clear()
    df = download_funding(market, tmp_path, SYMBOL, START - 2 * EIGHT_H, START + EIGHT_H)
    assert market.funding_calls == [(START - 2 * EIGHT_H, START - 1)]
    assert df["funding_time"].tolist() == [START + k * EIGHT_H for k in range(-2, 2)]
    assert len(load_funding(tmp_path, SYMBOL)) == 23
    assert len(load_funding(tmp_path, SYMBOL, START, START + 3 * EIGHT_H)) == 4
    assert len(load_funding(tmp_path, "ETHUSDT")) == 0


# ---------------------------------------------------------------------------------------------
# exchange filters
# ---------------------------------------------------------------------------------------------


def _write_filters_cache(cache_dir: Path, filters: SymbolFilters, fetched_at: int | None, host: str) -> Path:
    path = filters_cache_path(cache_dir, filters.symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = filters.to_dict() | {"host": host}
    if fetched_at is not None:
        payload["fetched_at"] = fetched_at
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_or_fetch_filters_uses_cache_offline(
    tmp_path: Path, btc_filters: SymbolFilters, candle_factory: Callable[..., pd.DataFrame],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_filters_cache(tmp_path, btc_filters, now_ms(), MAINNET_REST_URL)
    assert load_or_fetch_filters(None, tmp_path, SYMBOL) == btc_filters

    # a fresh cache is used even when a market is available (no request)
    market = FakeMarket(_history(candle_factory, 1), START, filters=btc_filters)
    assert load_or_fetch_filters(market, tmp_path, SYMBOL) == btc_filters
    assert market.filter_calls == 0

    # stale cache offline -> still used, with a warning
    _write_filters_cache(tmp_path, btc_filters, now_ms() - 3 * 86_400_000, MAINNET_REST_URL)
    with caplog.at_level(logging.WARNING, logger="bot.data.downloader"):
        assert load_or_fetch_filters(None, tmp_path, SYMBOL) == btc_filters
    assert "stale" in caplog.text

    # stale cache + exchange failure -> cached filters with a warning
    failing = FakeMarket(_history(candle_factory, 1), START, filters=None)
    assert load_or_fetch_filters(failing, tmp_path, SYMBOL) == btc_filters
    assert failing.filter_calls == 1


def test_load_or_fetch_filters_without_cache_raises(
    tmp_path: Path, btc_filters: SymbolFilters, candle_factory: Callable[..., pd.DataFrame]
) -> None:
    with pytest.raises(DataError, match="no cached exchange filters; run: python -m bot download"):
        load_or_fetch_filters(None, tmp_path, SYMBOL)
    assert not filters_cache_path(tmp_path, SYMBOL).exists()

    # with a mainnet market: fetched and cached (SymbolFilters.to_dict + fetched_at + host)
    market = FakeMarket(_history(candle_factory, 1), START, filters=btc_filters)
    before = now_ms()
    assert load_or_fetch_filters(market, tmp_path, SYMBOL) == btc_filters
    assert market.filter_calls == 1
    path = filters_cache_path(tmp_path, SYMBOL)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["host"] == MAINNET_REST_URL
    assert before <= data["fetched_at"] <= now_ms()
    assert {k: v for k, v in data.items() if k not in ("fetched_at", "host")} == btc_filters.to_dict()
    assert not Path(f"{path}.tmp").exists()
    assert load_or_fetch_filters(None, tmp_path, SYMBOL) == btc_filters

    # without a cache an exchange failure propagates
    failing = FakeMarket(_history(candle_factory, 1), START, filters=None)
    with pytest.raises(TransientError):
        load_or_fetch_filters(failing, tmp_path / "other", SYMBOL)


def test_load_or_fetch_filters_refuses_non_mainnet_host(
    tmp_path: Path, btc_filters: SymbolFilters, candle_factory: Callable[..., pd.DataFrame]
) -> None:
    demo = FakeMarket(_history(candle_factory, 1), START, filters=btc_filters, host=TESTNET_REST_URL)
    with pytest.raises(ConfigError):
        load_or_fetch_filters(demo, tmp_path, SYMBOL)
    assert demo.filter_calls == 0
    assert not filters_cache_path(tmp_path, SYMBOL).exists()

    # a (fresh) cache written from another host is never trusted as fresh: refreshed from mainnet
    _write_filters_cache(tmp_path, btc_filters, now_ms(), TESTNET_REST_URL)
    mainnet = FakeMarket(_history(candle_factory, 1), START, filters=btc_filters)
    assert load_or_fetch_filters(mainnet, tmp_path, SYMBOL) == btc_filters
    assert mainnet.filter_calls == 1
    data = json.loads(filters_cache_path(tmp_path, SYMBOL).read_text(encoding="utf-8"))
    assert data["host"] == MAINNET_REST_URL
