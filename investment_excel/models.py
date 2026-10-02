"""엑셀에 들어가는 데이터 모양."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

MARKETS = ("바이낸스", "국내주식", "해외주식", "기타")
CURRENCIES = ("USDT", "KRW", "USD")
DEFAULT_CURRENCY = {"바이낸스": "USDT", "국내주식": "KRW", "해외주식": "USD", "기타": "KRW"}
SIDES = ("매수", "매도")


@dataclass(frozen=True)
class Ticker:
    code: str  # 바이낸스: BTCUSDT / 국내주식: 005930 / 해외주식: AAPL (야후 파이낸스 기호)
    name: str
    market: str
    currency: str
    manual_price: float | None = None  # 시세를 못 받는 종목은 직접 입력
    memo: str = ""


@dataclass(frozen=True)
class Trade:
    day: date
    code: str
    side: str  # 매수 / 매도
    quantity: float
    price: float
    fee: float = 0.0
    memo: str = ""


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Inputs:
    """사용자가 직접 입력하는 값들. 업데이트할 때 이 값들은 그대로 옮겨 담습니다."""

    usd_krw: float = 1400.0
    auto_fx: bool = True
    tickers: list[Ticker] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
