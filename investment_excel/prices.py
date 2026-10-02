"""무료 공개 시세 받기 (API 키 필요 없음).

- 바이낸스: 공개 캔들(klines) API — 현물, 없으면 USDT-M 선물
- 국내주식: 네이버 금융 차트 데이터 (6자리 종목코드)
- 해외주식·환율: 야후 파이낸스 차트 데이터 (AAPL, TSLA, KRW=X 등)
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from .models import Bar, Ticker

DAYS = 400  # 받아 둘 일봉 개수 (차트 120일 + 60일 이동평균 계산 여유)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) investment-excel/1.0"

Fetch = Callable[[str], bytes]


class PriceError(Exception):
    """시세를 받지 못한 경우. 메시지는 사용자에게 그대로 보여 줍니다."""


def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise PriceError(f"서버가 요청을 거절했습니다 (HTTP {exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PriceError(f"인터넷 연결 문제로 시세를 받지 못했습니다 ({exc})") from exc


# ---------------------------------------------------------------------------------------------
# 바이낸스
# ---------------------------------------------------------------------------------------------

BINANCE_URLS = (
    "https://api.binance.com/api/v3/klines",
    "https://data-api.binance.vision/api/v3/klines",
    "https://fapi.binance.com/fapi/v1/klines",  # 현물에 없는 선물 전용 심볼
)


def parse_binance(payload: bytes) -> list[Bar]:
    rows = json.loads(payload)
    if not isinstance(rows, list):
        raise PriceError(f"바이낸스 응답을 해석하지 못했습니다: {str(rows)[:120]}")
    bars = []
    for row in rows:
        day = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).date()
        bars.append(Bar(day, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])))
    return bars


def fetch_binance(symbol: str, days: int = DAYS, get: Fetch = http_get) -> list[Bar]:
    query = urllib.parse.urlencode({"symbol": symbol.upper(), "interval": "1d", "limit": min(days, 1000)})
    problems = []
    for base in BINANCE_URLS:
        try:
            bars = parse_binance(get(f"{base}?{query}"))
        except (PriceError, ValueError, IndexError, TypeError) as exc:
            problems.append(str(exc))
            continue
        if bars:
            return bars
    raise PriceError(f"바이낸스에서 '{symbol}' 시세를 받지 못했습니다. 심볼(예: BTCUSDT)을 확인하세요. ({problems[-1]})")


# ---------------------------------------------------------------------------------------------
# 네이버 (국내주식)
# ---------------------------------------------------------------------------------------------

_NAVER_ITEM = re.compile(r'<item data="([^"]+)"')


def parse_naver(payload: bytes) -> list[Bar]:
    text = payload.decode("euc-kr", errors="replace")
    bars = []
    for raw in _NAVER_ITEM.findall(text):
        parts = raw.split("|")
        if len(parts) < 6:
            continue
        try:
            day = datetime.strptime(parts[0], "%Y%m%d").date()
            o, h, low, c, v = (float(p) for p in parts[1:6])
        except ValueError:
            continue
        if c > 0:
            bars.append(Bar(day, o or c, h or c, low or c, c, v))
    return bars


def fetch_naver(code: str, days: int = DAYS, get: Fetch = http_get) -> list[Bar]:
    code = code.strip()
    if not re.fullmatch(r"[0-9A-Z]{6}", code):
        raise PriceError(f"국내주식 종목코드는 6자리입니다 (예: 삼성전자 005930). 입력값: '{code}'")
    query = urllib.parse.urlencode({"symbol": code, "timeframe": "day", "count": days, "requestType": 0})
    bars = parse_naver(get(f"https://fchart.stock.naver.com/sise.nhn?{query}"))
    if not bars:
        raise PriceError(f"네이버 금융에서 '{code}' 시세를 찾지 못했습니다. 종목코드를 확인하세요.")
    return bars


# ---------------------------------------------------------------------------------------------
# 야후 파이낸스 (해외주식, 환율)
# ---------------------------------------------------------------------------------------------


def parse_yahoo(payload: bytes) -> list[Bar]:
    data = json.loads(payload)
    chart = data.get("chart") or {}
    if chart.get("error"):
        raise PriceError(str(chart["error"].get("description") or chart["error"]))
    results = chart.get("result") or []
    if not results:
        return []
    result = results[0]
    offset = timezone(timedelta(seconds=int(result.get("meta", {}).get("gmtoffset") or 0)))
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    bars: dict[date, Bar] = {}
    for i, ts in enumerate(result.get("timestamp") or []):
        values = [quote.get(k, [None])[i] if i < len(quote.get(k, [])) else None for k in ("open", "high", "low", "close")]
        if any(v is None for v in values):
            continue
        volume = quote.get("volume", [])
        vol = volume[i] if i < len(volume) and volume[i] is not None else 0
        day = datetime.fromtimestamp(ts, tz=offset).date()
        bars[day] = Bar(day, *(float(v) for v in values), float(vol))  # 같은 날이 두 번 오면 마지막 값
    return sorted(bars.values(), key=lambda b: b.day)


def fetch_yahoo(symbol: str, days: int = DAYS, get: Fetch = http_get) -> list[Bar]:
    years = 2 if days > 250 else 1
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol.strip())}"
        f"?range={years}y&interval=1d&includePrePost=false"
    )
    bars = parse_yahoo(get(url))
    if not bars:
        raise PriceError(f"야후 파이낸스에서 '{symbol}' 시세를 찾지 못했습니다. 기호(예: AAPL, TSLA)를 확인하세요.")
    return bars[-days:]


def fetch_usd_krw(get: Fetch = http_get) -> float:
    bars = fetch_yahoo("KRW=X", days=10, get=get)
    return round(bars[-1].close, 2)


# ---------------------------------------------------------------------------------------------


def fetch_ticker(ticker: Ticker, days: int = DAYS, get: Fetch = http_get) -> list[Bar]:
    if ticker.market == "바이낸스":
        return fetch_binance(ticker.code, days, get)
    if ticker.market == "국내주식":
        return fetch_naver(ticker.code, days, get)
    if ticker.market == "해외주식":
        return fetch_yahoo(ticker.code, days, get)
    raise PriceError("'기타' 시장 종목은 자동 시세가 없습니다. '일봉데이터' 시트에 직접 입력하거나 수동 현재가를 쓰세요.")
