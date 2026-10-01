"""Live checks against PUBLIC, unauthenticated Binance mainnet market-data endpoints (SPEC §14.2 U2).

Deselected by default (pytest.ini: ``-m "not network"``); run explicitly with ``pytest -m network``.
Only GET /fapi/v1/time, exchangeInfo, klines, fundingRate and premiumIndex are used — no credentials, no signed
endpoints, no orders.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from bot.config import MAINNET_REST_URL
from bot.exchange.market import MarketData
from bot.exchange.rest import BinanceRestClient
from bot.models import KLINE_DTYPES, validate_candles_df
from bot.timeutil import interval_to_ms, now_ms

pytestmark = pytest.mark.network

SYMBOL = "BTCUSDT"


@pytest.fixture
def market() -> Iterator[MarketData]:
    client = BinanceRestClient(MAINNET_REST_URL)  # no credentials: signed calls are impossible
    assert client.has_credentials is False
    try:
        yield MarketData(client)
    finally:
        client.close()


def test_server_time_within_60s_of_local(market: MarketData) -> None:
    server = market.server_time()
    assert isinstance(server, int)
    assert abs(server - now_ms()) < 60_000
    assert market.client.time_offset_ms is not None


def test_recent_klines_closed_filter(market: MarketData) -> None:
    closed, forming, server_now = market.recent_klines(SYMBOL, "1h", 3)
    assert 3 <= len(closed) <= 4  # limit + 1 rows requested; the forming one (if any) is split off
    assert {c: str(closed[c].dtype) for c in closed.columns} == KLINE_DTYPES
    validate_candles_df(closed, interval_to_ms("1h"))
    assert (closed["close_time"] < server_now).all()
    if forming is not None:
        assert forming.open_time <= server_now <= forming.close_time
        assert forming.open_time == int(closed["open_time"].iloc[-1]) + interval_to_ms("1h")


def test_exchange_info_btcusdt_parses(market: MarketData) -> None:
    filters = market.symbol_filters(SYMBOL)
    assert filters.symbol == SYMBOL
    assert filters.status == "TRADING"
    assert filters.contract_type == "PERPETUAL"
    assert filters.tick_size > 0 and filters.step_size > 0 and filters.market_step_size > 0
    assert filters.min_notional > 0
    assert market.client.weight_limit > 0


def test_funding_rate_latest_rows_parse(market: MarketData) -> None:
    end = market.server_time()
    df = market.funding_rates(SYMBOL, end - 3 * 86_400_000, end)
    assert len(df) >= 1
    assert list(df.columns) == ["funding_time", "funding_rate", "mark_price"]
    assert str(df["funding_time"].dtype) == "int64"
    assert str(df["funding_rate"].dtype) == "float64"
    assert df["funding_time"].is_monotonic_increasing and df["funding_time"].is_unique
    assert int(df["funding_time"].iloc[-1]) <= end


def test_premium_index_parses(market: MarketData) -> None:
    p = market.premium_index(SYMBOL)
    assert set(p) == {"mark_price", "last_funding_rate", "next_funding_time", "time"}
    assert p["mark_price"] > 0
    assert isinstance(p["next_funding_time"], int) and p["next_funding_time"] > 0
    assert abs(p["time"] - now_ms()) < 3_600_000
    assert market.mark_price(SYMBOL) > 0
