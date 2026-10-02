from __future__ import annotations

from datetime import date

import pytest

from investment_excel import prices
from investment_excel.models import Ticker
from investment_excel.prices import PriceError

from .conftest import binance_payload, fake_get, naver_payload, yahoo_payload


def test_binance_parses_klines():
    bars = prices.parse_binance(binance_payload(3))
    assert [b.day for b in bars] == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    assert bars[0].open == 60000 and bars[0].close == pytest.approx(60600)
    assert bars[0].volume == pytest.approx(123.4)


def test_binance_falls_back_to_next_host():
    get = fake_get({"api.binance.com": PriceError("blocked"), "data-api.binance.vision": binance_payload(5)})
    bars = prices.fetch_binance("btcusdt", get=get)
    assert len(bars) == 5
    assert "symbol=BTCUSDT" in get.calls[0] and "interval=1d" in get.calls[0]


def test_binance_unknown_symbol_message():
    get = fake_get({"binance": b'{"code":-1121,"msg":"Invalid symbol."}'})
    with pytest.raises(PriceError, match="심볼"):
        prices.fetch_binance("NOPEUSDT", get=get)


def test_naver_parses_euc_kr_xml():
    bars = prices.fetch_naver("005930", get=fake_get({"fchart.stock.naver.com": naver_payload(3)}))
    assert bars[0].day == date(2026, 3, 2)
    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close, bars[0].volume) == (60000, 61000, 59000, 60500, 1000)


def test_naver_rejects_bad_code():
    with pytest.raises(PriceError, match="6자리"):
        prices.fetch_naver("삼성전자", get=fake_get({}))


def test_yahoo_skips_missing_rows_and_uses_exchange_date():
    bars = prices.fetch_yahoo("AAPL", get=fake_get({"finance/chart/AAPL": yahoo_payload(6)}))
    assert len(bars) == 5  # None 이 있던 하루는 빠짐
    assert bars[0].day == date(2026, 3, 2)
    assert bars[0].close == 201


def test_yahoo_error_message():
    payload = b'{"chart":{"result":null,"error":{"code":"Not Found","description":"No data found, symbol may be delisted"}}}'
    with pytest.raises(PriceError, match="delisted"):
        prices.fetch_yahoo("ZZZZ", get=fake_get({"finance/chart": payload}))


def test_usd_krw():
    assert prices.fetch_usd_krw(fake_get({"KRW%3DX": yahoo_payload(5, price=1380.0)})) == 1385.0


def test_fetch_ticker_routes_by_market():
    get = fake_get({"binance": binance_payload(2), "naver": naver_payload(2), "finance/chart": yahoo_payload(2)})
    assert len(prices.fetch_ticker(Ticker("BTCUSDT", "", "바이낸스", "USDT"), get=get)) == 2
    assert len(prices.fetch_ticker(Ticker("005930", "", "국내주식", "KRW"), get=get)) == 2
    assert len(prices.fetch_ticker(Ticker("AAPL", "", "해외주식", "USD"), get=get)) == 2
    with pytest.raises(PriceError, match="기타"):
        prices.fetch_ticker(Ticker("금", "", "기타", "KRW"), get=get)
