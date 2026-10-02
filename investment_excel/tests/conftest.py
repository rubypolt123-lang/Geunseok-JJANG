"""investment_excel 테스트 공용 도구. 실제 인터넷은 쓰지 않습니다."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


def binance_payload(days: int = 200, start: float = 60000.0) -> bytes:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    price = start
    for i in range(days):
        ts = int((base + timedelta(days=i)).timestamp() * 1000)
        o, c = price, price * 1.01
        rows.append([ts, str(o), str(c * 1.01), str(o * 0.99), str(c), "123.4", ts + 86_399_999, "0", 0, "0", "0", "0"])
        price = c
    return json.dumps(rows).encode()


def naver_payload(days: int = 150) -> bytes:
    items = []
    day = datetime(2026, 3, 2)
    for i in range(days):
        d = day + timedelta(days=i)
        items.append(f'<item data="{d:%Y%m%d}|{60000 + i}|{61000 + i}|{59000 + i}|{60500 + i}|{1000 + i}" />')
    xml = '<?xml version="1.0" encoding="EUC-KR" ?><protocol><chartdata symbol="005930" name="삼성전자">' + "".join(items) + "</chartdata></protocol>"
    return xml.encode("euc-kr")


def yahoo_payload(days: int = 150, price: float = 200.0, gmtoffset: int = -14400) -> bytes:
    start = datetime(2026, 3, 2, 13, 30, tzinfo=timezone.utc)
    stamps = [int((start + timedelta(days=i)).timestamp()) for i in range(days)]
    quote = {
        "open": [price + i for i in range(days)],
        "high": [price + i + 2 for i in range(days)],
        "low": [price + i - 2 for i in range(days)],
        "close": [price + i + 1 for i in range(days)],
        "volume": [1000 + i for i in range(days)],
    }
    if days > 3:
        quote["close"][3] = None  # 빠진 날은 건너뛰어야 함
    return json.dumps({"chart": {"result": [{"meta": {"gmtoffset": gmtoffset}, "timestamp": stamps, "indicators": {"quote": [quote]}}], "error": None}}).encode()


def fake_get(routes: dict[str, bytes | Exception]):
    calls: list[str] = []

    def get(url: str) -> bytes:
        calls.append(url)
        for key, value in routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        from investment_excel.prices import PriceError

        raise PriceError(f"no route for {url}")

    get.calls = calls  # type: ignore[attr-defined]
    return get
