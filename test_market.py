"""Tests for bot.exchange.market (SPEC §6.3, §14.2 U2). HTTP is mocked with ``responses``; no network."""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pandas as pd
import pytest
import responses

from bot.config import MAINNET_REST_URL
from bot.errors import ConfigError, DataError
from bot.exchange.market import MarketData, klines_to_df, split_closed
from bot.exchange.rest import BinanceRestClient
from bot.models import KLINE_COLUMNS, KLINE_DTYPES, Candle, SymbolFilters, validate_candles_df
from bot.timeutil import now_ms

BASE = MAINNET_REST_URL
H = 3_600_000
T0 = 1_790_755_200_000  # 2026-09-30T08:00:00Z, a multiple of 1h


def kline_row(open_time: int, interval_ms: int = H, close: float = 100.5) -> list[Any]:
    """A raw Binance kline row: numbers as strings except times/trades, 12th field "ignore"."""
    return [
        open_time, "100.0", "101.0", "99.0", f"{close}", "10.5", open_time + interval_ms - 1,
        "1055.25", 42, "5.25", "527.625", "0",
    ]


@pytest.fixture
def rsps() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def market(fixed_clock: Callable[..., Any]) -> Iterator[tuple[MarketData, BinanceRestClient, Any]]:
    clock = fixed_clock(T0 / 1000 + 3 * 3600 + 1800)  # 30 min into the bar opening at T0 + 3h
    client = BinanceRestClient(BASE, clock=clock, sleep=clock.sleep)  # public data: no credentials
    yield MarketData(client), client, clock
    client.close()


def _query(call: Any) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(call.request.url).query))


# ---------------------------------------------------------------------------------------------
# klines_to_df / split_closed
# ---------------------------------------------------------------------------------------------


def test_klines_to_df_dtypes_and_order() -> None:
    rows = [kline_row(T0 + 2 * H), kline_row(T0), kline_row(T0 + H, close=1.0), kline_row(T0 + H, close=2.0)]
    df = klines_to_df(rows)
    assert tuple(df.columns) == KLINE_COLUMNS  # field 11 ("ignore") dropped
    assert {c: str(df[c].dtype) for c in df.columns} == KLINE_DTYPES
    assert isinstance(df.index, pd.RangeIndex)
    assert df["open_time"].tolist() == [T0, T0 + H, T0 + 2 * H]  # sorted, deduplicated
    assert df.loc[1, "close"] == 2.0  # duplicate open_time: last one kept
    assert df.loc[0, "close_time"] == T0 + H - 1
    assert df.loc[0, "trades"] == 42
    assert df.loc[0, "taker_buy_quote"] == pytest.approx(527.625)
    validate_candles_df(df, H)

    empty = klines_to_df([])
    assert len(empty) == 0
    assert tuple(empty.columns) == KLINE_COLUMNS
    assert {c: str(empty[c].dtype) for c in empty.columns} == KLINE_DTYPES
    validate_candles_df(empty, H)

    with pytest.raises(DataError):
        klines_to_df([[T0, "1"]])  # too few fields
    with pytest.raises(DataError):
        klines_to_df([kline_row(T0)[:4] + ["oops"] + kline_row(T0)[5:]])


def test_split_closed_drops_forming_candle() -> None:
    df = klines_to_df([kline_row(T0 + k * H) for k in range(4)])
    now = T0 + 3 * H + 1234  # inside the last bar
    closed, forming = split_closed(df, now)
    assert closed["open_time"].tolist() == [T0, T0 + H, T0 + 2 * H]
    assert isinstance(closed.index, pd.RangeIndex) and closed.index[0] == 0
    assert (closed["close_time"] < now).all()
    assert isinstance(forming, Candle)
    assert forming.open_time == T0 + 3 * H
    assert type(forming.open_time) is int and type(forming.close_time) is int
    assert forming.open == 100.0

    # exactly at the last close_time: still forming (open_time <= now <= close_time)
    closed, forming = split_closed(df, T0 + 4 * H - 1)
    assert len(closed) == 3 and forming is not None

    # after the last bar closed: everything is closed, nothing forming
    closed, forming = split_closed(df, T0 + 4 * H)
    assert len(closed) == 4 and forming is None

    closed, forming = split_closed(klines_to_df([]), now)
    assert len(closed) == 0 and forming is None


# ---------------------------------------------------------------------------------------------
# MarketData
# ---------------------------------------------------------------------------------------------


def test_klines_request_params_and_limit_clamp(rsps: responses.RequestsMock, market: Any) -> None:
    md, _, _ = market
    rsps.add(responses.GET, f"{BASE}/fapi/v1/klines", json=[kline_row(T0)])
    df = md.klines("BTCUSDT", "1h", start_ms=T0, end_ms=T0 + 5 * H, limit=5000)
    assert len(df) == 1
    q = _query(rsps.calls[0])
    assert q == {"symbol": "BTCUSDT", "interval": "1h", "startTime": str(T0), "endTime": str(T0 + 5 * H),
                 "limit": "1500"}
    md.klines("BTCUSDT", "1h", limit=0)
    assert _query(rsps.calls[1]) == {"symbol": "BTCUSDT", "interval": "1h", "limit": "1"}
    with pytest.raises(ConfigError):
        md.klines("BTCUSDT", "1w")
    assert len(rsps.calls) == 2


def test_recent_klines_returns_forming(rsps: responses.RequestsMock, market: Any) -> None:
    md, client, clock = market
    offset = 250
    server_now = now_ms(clock) + offset

    def time_callback(request: Any) -> tuple[int, dict[str, str], str]:
        return 200, {}, f'{{"serverTime": {server_now}}}'

    rsps.add_callback(responses.GET, f"{BASE}/fapi/v1/time", callback=time_callback)
    rsps.add(responses.GET, f"{BASE}/fapi/v1/klines", json=[kline_row(T0 + k * H) for k in range(4)])
    closed, forming, now = md.recent_klines("BTCUSDT", "1h", 3)
    assert now == server_now
    assert client.time_offset_ms == offset  # the call re-synced the clock
    assert [urlsplit(c.request.url).path for c in rsps.calls] == ["/fapi/v1/time", "/fapi/v1/klines"]
    assert _query(rsps.calls[1])["limit"] == "4"  # limit + 1 (includes the forming candle)
    assert closed["open_time"].tolist() == [T0, T0 + H, T0 + 2 * H]
    validate_candles_df(closed, H)
    assert forming is not None and forming.open_time == T0 + 3 * H
    assert type(now) is int


def test_server_time_syncs_client(rsps: responses.RequestsMock, market: Any) -> None:
    md, client, clock = market
    rsps.add(responses.GET, f"{BASE}/fapi/v1/time", json={"serverTime": now_ms(clock) - 400})
    assert md.server_time() == now_ms(clock) - 400
    assert client.time_offset_ms == -400


def _funding_row(t: int, rate: str = "0.00010000", mark: str = "60000.00") -> dict[str, Any]:
    return {"symbol": "BTCUSDT", "fundingTime": t, "fundingRate": rate, "markPrice": mark}


def test_funding_rates_paginates(rsps: responses.RequestsMock, market: Any) -> None:
    md, _, _ = market
    eight_h = 8 * H
    start = 1_600_000_000_000
    first_page = [_funding_row(start + k * eight_h) for k in range(1000)]
    first_page[0]["markPrice"] = ""  # old rows have an empty markPrice
    last_t = first_page[-1]["fundingTime"]
    second_page = [_funding_row(last_t + k * eight_h, rate="-0.0002") for k in range(1, 4)]
    end = second_page[-1]["fundingTime"] + 1000
    rsps.add(responses.GET, f"{BASE}/fapi/v1/fundingRate", json=first_page)
    rsps.add(responses.GET, f"{BASE}/fapi/v1/fundingRate", json=second_page)

    df = md.funding_rates("BTCUSDT", start, end)
    assert len(rsps.calls) == 2
    q1, q2 = _query(rsps.calls[0]), _query(rsps.calls[1])
    assert q1 == {"symbol": "BTCUSDT", "startTime": str(start), "endTime": str(end), "limit": "1000"}
    assert q2["startTime"] == str(last_t + 1)  # next start = last fundingTime + 1
    assert q2["endTime"] == str(end) and q2["limit"] == "1000"

    assert list(df.columns) == ["funding_time", "funding_rate", "mark_price"]
    assert str(df["funding_time"].dtype) == "int64"
    assert str(df["funding_rate"].dtype) == "float64"
    assert str(df["mark_price"].dtype) == "float64"
    assert len(df) == 1003
    assert df["funding_time"].is_monotonic_increasing and df["funding_time"].is_unique
    assert math.isnan(df.loc[0, "mark_price"])
    assert df.loc[1, "mark_price"] == 60000.0
    assert df.loc[1002, "funding_rate"] == pytest.approx(-0.0002)


def test_funding_rates_single_short_page_stops(rsps: responses.RequestsMock, market: Any) -> None:
    md, _, _ = market
    rsps.add(responses.GET, f"{BASE}/fapi/v1/fundingRate", json=[])
    df = md.funding_rates("BTCUSDT", 1, 2)
    assert len(rsps.calls) == 1
    assert len(df) == 0 and list(df.columns) == ["funding_time", "funding_rate", "mark_price"]
    assert str(df["funding_time"].dtype) == "int64"


def test_exchange_info_updates_weight_limit(
    rsps: responses.RequestsMock, market: Any, exchange_info_btc: dict[str, Any]
) -> None:
    md, client, _ = market
    info = copy.deepcopy(exchange_info_btc)
    info["rateLimits"][0]["limit"] = 6000  # e.g. the demo host
    rsps.add(responses.GET, f"{BASE}/fapi/v1/exchangeInfo", json=info)
    assert client.weight_limit == 2400
    out = md.exchange_info()
    assert out["symbols"][0]["symbol"] == "BTCUSDT"
    assert client.weight_limit == 6000

    # ORDERS limits and other intervals never touch the weight limit
    info2 = copy.deepcopy(exchange_info_btc)
    info2["rateLimits"] = [
        {"rateLimitType": "ORDERS", "interval": "MINUTE", "intervalNum": 1, "limit": 1200},
        {"rateLimitType": "REQUEST_WEIGHT", "interval": "SECOND", "intervalNum": 10, "limit": 50},
    ]
    rsps.reset()
    rsps.add(responses.GET, f"{BASE}/fapi/v1/exchangeInfo", json=info2)
    md.exchange_info()
    assert client.weight_limit == 6000


def test_symbol_filters_cached(
    rsps: responses.RequestsMock, fixed_clock: Callable[..., Any], exchange_info_btc: dict[str, Any],
    btc_filters: SymbolFilters,
) -> None:
    clock = fixed_clock()
    client = BinanceRestClient(BASE, clock=clock, sleep=clock.sleep)
    md = MarketData(client, filters_ttl_sec=3600.0)
    rsps.add(responses.GET, f"{BASE}/fapi/v1/exchangeInfo", json=exchange_info_btc)
    try:
        assert md.symbol_filters("BTCUSDT") == btc_filters
        assert md.symbol_filters("BTCUSDT") == btc_filters
        assert len(rsps.calls) == 1  # cached
        clock.advance(3599.0)
        md.symbol_filters("BTCUSDT")
        assert len(rsps.calls) == 1
        clock.advance(2.0)  # TTL expired
        assert md.symbol_filters("BTCUSDT") == btc_filters
        assert len(rsps.calls) == 2
        with pytest.raises(DataError, match="ETHUSDT"):
            md.symbol_filters("ETHUSDT")
        assert len(rsps.calls) == 3
    finally:
        client.close()


def test_mark_price(rsps: responses.RequestsMock, market: Any) -> None:
    md, _, _ = market
    rsps.add(
        responses.GET,
        f"{BASE}/fapi/v1/premiumIndex",
        json={"symbol": "BTCUSDT", "markPrice": "84012.34000000", "indexPrice": "84000.1", "lastFundingRate": "0.0001",
              "interestRate": "0.0001", "nextFundingTime": 1790784000000, "time": 1790770782000},
    )
    price = md.mark_price("BTCUSDT")
    assert type(price) is float
    assert price == 84012.34
    assert _query(rsps.calls[0]) == {"symbol": "BTCUSDT"}


def test_premium_index(rsps: responses.RequestsMock, market: Any) -> None:
    md, _, _ = market
    rsps.add(
        responses.GET,
        f"{BASE}/fapi/v1/premiumIndex",
        json={"symbol": "BTCUSDT", "markPrice": "84012.34000000", "indexPrice": "84000.1",
              "estimatedSettlePrice": "84001.0", "lastFundingRate": "-0.00012500", "interestRate": "0.0001",
              "nextFundingTime": 1790784000000, "time": 1790770782000},
    )
    out = md.premium_index("BTCUSDT")
    assert out == {"mark_price": 84012.34, "last_funding_rate": -0.000125, "next_funding_time": 1790784000000,
                   "time": 1790770782000}
    assert type(out["mark_price"]) is float and type(out["last_funding_rate"]) is float
    assert type(out["next_funding_time"]) is int and type(out["time"]) is int

    rsps.reset()
    rsps.add(responses.GET, f"{BASE}/fapi/v1/premiumIndex", json={"symbol": "BTCUSDT", "markPrice": None})
    with pytest.raises(DataError):
        md.premium_index("BTCUSDT")
