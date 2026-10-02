"""처음 파일을 만들 때 넣는 예시 종목·거래와, 인터넷이 안 될 때 쓰는 예시 일봉."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

from .models import Bar, Inputs, Ticker, Trade

EXAMPLE_MEMO = "예시 — 지우고 쓰세요"


def example_inputs(today: date | None = None) -> Inputs:
    today = today or date.today()
    return Inputs(
        usd_krw=1400.0,
        auto_fx=True,
        tickers=[
            Ticker("BTCUSDT", "비트코인", "바이낸스", "USDT", memo=EXAMPLE_MEMO),
            Ticker("ETHUSDT", "이더리움", "바이낸스", "USDT", memo=EXAMPLE_MEMO),
            Ticker("005930", "삼성전자", "국내주식", "KRW", memo=EXAMPLE_MEMO),
            Ticker("AAPL", "애플", "해외주식", "USD", memo=EXAMPLE_MEMO),
        ],
        trades=[
            Trade(today - timedelta(days=90), "BTCUSDT", "매수", 0.05, 60000, 3.0, EXAMPLE_MEMO),
            Trade(today - timedelta(days=60), "ETHUSDT", "매수", 1.2, 2500, 1.5, EXAMPLE_MEMO),
            Trade(today - timedelta(days=45), "005930", "매수", 30, 60000, 900, EXAMPLE_MEMO),
            Trade(today - timedelta(days=30), "AAPL", "매수", 10, 200, 1.0, EXAMPLE_MEMO),
            Trade(today - timedelta(days=10), "BTCUSDT", "매도", 0.02, 66000, 1.3, EXAMPLE_MEMO),
        ],
    )


def synthetic_bars(code: str, start_price: float, days: int = 250, today: date | None = None, *, weekdays_only: bool = False,
                   decimals: int = 2) -> list[Bar]:
    """실제 시세가 아닌, 차트 모양을 보여 주기 위한 예시 일봉 (항상 같은 값이 나오게 고정된 난수)."""
    rng = random.Random(sum(map(ord, code)))
    today = today or date.today()
    bars: list[Bar] = []
    price = start_price
    day = today - timedelta(days=int(days * (1.45 if weekdays_only else 1)))
    while len(bars) < days:
        day += timedelta(days=1)
        if weekdays_only and day.weekday() >= 5:
            continue
        drift = 0.0006 + 0.004 * math.sin(len(bars) / 23)
        change = rng.gauss(drift, 0.018)
        open_ = price * (1 + rng.gauss(0, 0.004))
        close = max(open_ * (1 + change), 0.01)
        high = max(open_, close) * (1 + abs(rng.gauss(0, 0.008)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, 0.008)))
        volume = abs(rng.gauss(1.0, 0.35)) * (1 + 3 * abs(change)) * 1000
        bars.append(Bar(day, round(open_, decimals), round(high, decimals), round(low, decimals), round(close, decimals),
                        round(volume, 2)))
        price = close
    return bars


def example_bars(today: date | None = None) -> dict[str, list[Bar]]:
    return {
        "BTCUSDT": synthetic_bars("BTCUSDT", 58000, today=today),
        "ETHUSDT": synthetic_bars("ETHUSDT", 2400, today=today),
        "005930": synthetic_bars("005930", 58000, today=today, weekdays_only=True, decimals=0),
        "AAPL": synthetic_bars("AAPL", 190, today=today, weekdays_only=True),
    }
