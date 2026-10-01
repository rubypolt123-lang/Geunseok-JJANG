"""Tests for bot.exchange.rest (SPEC §6.1, §14.2 U2). HTTP is mocked with ``responses``; no network."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import numpy as np
import pytest
import requests
import responses

from bot.errors import (
    AlgoLimitError,
    AuthError,
    DuplicateClientIdError,
    ExchangeError,
    ImmediateTriggerError,
    InsufficientMarginError,
    IpBannedError,
    MinNotionalError,
    NoChangeNeededError,
    NoSuchOrderError,
    OrderRejectedError,
    RateLimitError,
    ReduceOnlyModeError,
    ReduceOnlyRejectedError,
    TimestampError,
    TransientError,
    UnknownOrderStatusError,
)
from bot.exchange.rest import (
    BACKOFF_SECONDS,
    USER_AGENT,
    BinanceRestClient,
    encode_params,
    hmac_sha256_signature,
    map_error,
    parse_rate_limit_headers,
)
from bot.timeutil import now_ms

BASE = "https://demo-fapi.binance.com"

# Official Binance HMAC test vector (public documentation values, research §3; never real credentials).
VECTOR_KEY = "dbefbc809e3e83c283a984c3a1459732ea7db1360ca80c5c2c8867408d28cc83"
VECTOR_SECRET = "2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9"
VECTOR_PAYLOAD = (
    "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC&recvWindow=5000&timestamp=1591702613943"
)
VECTOR_SIGNATURE = "3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9"
VECTOR_PARAMS = {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": "1", "price": "9000",
                 "timeInForce": "GTC"}
VECTOR_CLOCK_S = 1591702613.943

FAKE_KEY = "k" * 64
FAKE_SECRET = "s" * 64

SERVICE_UNAVAILABLE = "Service Unavailable"
UNKNOWN_ERROR = "Unknown error, please check your request or try again later."


# ---------------------------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def rsps() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def make_client(fixed_clock: Callable[..., Any]) -> Iterator[Callable[..., tuple[BinanceRestClient, Any]]]:
    """``make(signed=True, start_s=..., **kwargs) -> (client, clock)``; the offset is pre-synced to 0."""
    created: list[BinanceRestClient] = []

    def make(signed: bool = True, start_s: float = 1_790_769_600.0, **kwargs: Any) -> tuple[BinanceRestClient, Any]:
        clock = fixed_clock(start_s)
        key, secret = (FAKE_KEY, FAKE_SECRET) if signed else (None, None)
        client = BinanceRestClient(BASE, key, secret, clock=clock, sleep=clock.sleep, **kwargs)
        client.set_time_offset(0)
        created.append(client)
        return client, clock

    yield make
    for c in created:
        c.close()


def _query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def _query_pairs(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query, keep_blank_values=True)


def _paths(rsps: responses.RequestsMock) -> list[str]:
    return [urlsplit(c.request.url).path for c in rsps.calls]


# ---------------------------------------------------------------------------------------------
# signing
# ---------------------------------------------------------------------------------------------


def test_hmac_official_vector() -> None:
    assert hmac_sha256_signature(VECTOR_SECRET, VECTOR_PAYLOAD) == VECTOR_SIGNATURE


def test_hmac_wrong_order_differs() -> None:
    wrong_order = (
        "symbol=BTCUSDT&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=9000&recvWindow=5000&timestamp=1591702613943"
    )
    sig = hmac_sha256_signature(VECTOR_SECRET, wrong_order)
    assert sig == "ec11dcc17e67e47f0d3c3f513dfe9062307e37619c5c82ebaa8fe0bdf3d59519"
    assert sig != VECTOR_SIGNATURE


def test_signed_request_url_matches_official_vector(rsps: responses.RequestsMock, fixed_clock: Callable[..., Any]) -> None:
    clock = fixed_clock(VECTOR_CLOCK_S)
    client = BinanceRestClient(BASE, VECTOR_KEY, VECTOR_SECRET, recv_window_ms=5000, clock=clock, sleep=clock.sleep)
    client.set_time_offset(0)
    rsps.add(responses.POST, f"{BASE}/fapi/v1/order", json={"orderId": 1, "status": "NEW"})
    try:
        out = client.signed_request("POST", "/fapi/v1/order", VECTOR_PARAMS)
    finally:
        client.close()
    assert out == {"orderId": 1, "status": "NEW"}
    assert len(rsps.calls) == 1
    req = rsps.calls[0].request
    assert req.method == "POST"
    assert req.url == f"{BASE}/fapi/v1/order?{VECTOR_PAYLOAD}&signature={VECTOR_SIGNATURE}"
    assert req.headers["X-MBX-APIKEY"] == VECTOR_KEY
    assert req.headers["User-Agent"] == USER_AGENT
    assert not req.body  # every parameter travels in the query string
    assert clock.sleeps == []


def test_signed_params_drop_none_and_append_recv_window_and_timestamp_last(
    rsps: responses.RequestsMock, make_client: Callable[..., Any]
) -> None:
    client, clock = make_client(recv_window_ms=7000)
    rsps.add(responses.DELETE, f"{BASE}/fapi/v1/algoOrder", json={"code": 200, "msg": "success"})
    out = client.signed_request(
        "DELETE", "/fapi/v1/algoOrder", {"symbol": "BTCUSDT", "algoId": None, "clientAlgoId": "mab1-4314-SL-1-0"}
    )
    assert out == {"code": 200, "msg": "success"}  # code 200 is a success
    pairs = _query_pairs(rsps.calls[0].request.url)
    assert [k for k, _ in pairs] == ["symbol", "clientAlgoId", "recvWindow", "timestamp", "signature"]
    q = dict(pairs)
    assert q["recvWindow"] == "7000"
    assert q["timestamp"] == str(now_ms(clock))
    unsigned = rsps.calls[0].request.url.split("?", 1)[1].rsplit("&signature=", 1)[0]
    assert q["signature"] == hmac_sha256_signature(FAKE_SECRET, unsigned)


def test_public_get_sends_no_key_or_signature(rsps: responses.RequestsMock, make_client: Callable[..., Any]) -> None:
    client, _ = make_client(signed=True)
    rsps.add(responses.GET, f"{BASE}/fapi/v1/premiumIndex", json={"markPrice": "1"})
    client.public_get("/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"})
    req = rsps.calls[0].request
    assert req.url == f"{BASE}/fapi/v1/premiumIndex?symbol=BTCUSDT"
    assert "X-MBX-APIKEY" not in req.headers
    assert "signature" not in req.url and "timestamp" not in req.url


def test_encode_params_bool_decimal_none() -> None:
    params = {
        "symbol": "BTCUSDT",
        "reduceOnly": True,
        "closePosition": False,
        "quantity": Decimal("0.00100"),
        "stopPrice": None,
        "triggerPrice": Decimal("1E+2"),
        "price": 0.1 + 0.2,
        "limit": 1500,
        "big": np.int64(1_790_769_600_000),
        "text": "a b/c",
    }
    assert encode_params(params) == (
        "symbol=BTCUSDT&reduceOnly=true&closePosition=false&quantity=0.001&triggerPrice=100"
        "&price=0.30000000000000004&limit=1500&big=1790769600000&text=a%20b%2Fc"
    )
    assert encode_params({"a": 1.0, "b": 84000.10}) == "a=1&b=84000.1"
    assert encode_params(None) == ""
    assert encode_params({}) == ""
    assert encode_params({"only": None}) == ""
    with pytest.raises(ValueError):
        encode_params({"x": float("nan")})


def test_no_credentials_raises_before_http(rsps: responses.RequestsMock, make_client: Callable[..., Any]) -> None:
    client, clock = make_client(signed=False)
    assert client.has_credentials is False
    for method in ("GET", "POST", "PUT", "DELETE"):
        with pytest.raises(AuthError, match="no API credentials"):
            client.signed_request(method, "/fapi/v1/order", {"symbol": "BTCUSDT"})
    assert len(rsps.calls) == 0
    assert clock.sleeps == []

    half = BinanceRestClient(BASE, FAKE_KEY, "", clock=clock, sleep=clock.sleep)  # key without secret
    try:
        assert half.has_credentials is False
        with pytest.raises(AuthError):
            half.signed_request("GET", "/fapi/v2/account")
    finally:
        half.close()
    assert len(rsps.calls) == 0


# ---------------------------------------------------------------------------------------------
# error mapping (every row of the §6.1 table, incl. the method-dependent rows)
# ---------------------------------------------------------------------------------------------


def _err(code: int, msg: str = "error") -> dict[str, Any]:
    return {"code": code, "msg": msg}


ORDER = "/fapi/v1/order"
ALGO = "/fapi/v1/algoOrder"
ACCOUNT = "/fapi/v2/account"

# (id, http_status, payload, method, path, headers, expected class, not_executed or None, retry_after or None)
ERROR_CASES: list[tuple[str, int, Any, str, str, dict[str, str], type[ExchangeError], bool | None, float | None]] = [
    ("418-retry-after", 418, _err(-1003), "GET", "/fapi/v1/klines", {"Retry-After": "300"}, IpBannedError, None, 300.0),
    ("418-default", 418, "banned", "POST", ORDER, {}, IpBannedError, None, 120.0),
    ("429-retry-after", 429, _err(-1003), "GET", "/fapi/v1/klines", {"retry-after": "7"}, RateLimitError, None, 7.0),
    ("429-post", 429, _err(-1015), "POST", ORDER, {}, RateLimitError, None, 60.0),
    ("-1003", 400, _err(-1003), "GET", "/fapi/v1/klines", {}, RateLimitError, None, 60.0),
    ("-1015", 400, _err(-1015), "POST", ORDER, {}, RateLimitError, None, 60.0),
    ("-1021", 400, _err(-1021), "GET", ACCOUNT, {}, TimestampError, None, None),
    ("-5028", 400, _err(-5028), "POST", ORDER, {}, TimestampError, None, None),
    ("-1022", 400, _err(-1022), "GET", ACCOUNT, {}, AuthError, None, None),
    ("-2014", 401, _err(-2014), "GET", ACCOUNT, {}, AuthError, None, None),
    ("-2015", 401, _err(-2015), "POST", ORDER, {}, AuthError, None, None),
    # POST "known not executed" (checked before the non-JSON rule)
    ("post-503-service-unavailable-text", 503, SERVICE_UNAVAILABLE, "POST", ORDER, {}, TransientError, True, None),
    ("post-503-service-unavailable-json", 503, _err(-1001, SERVICE_UNAVAILABLE), "POST", ORDER, {},
     TransientError, True, None),
    ("post-1008", 503, _err(-1008, "Request throttled by system-level protection."), "POST", ALGO, {},
     TransientError, True, None),
    ("post-1008-400", 400, _err(-1008), "POST", ORDER, {}, TransientError, True, None),
    # POST outcome unknown
    ("post-503-unknown-error-text", 503, UNKNOWN_ERROR, "POST", ORDER, {}, UnknownOrderStatusError, None, None),
    ("post-503-unknown-error-json", 503, _err(-1000, UNKNOWN_ERROR), "POST", ORDER, {},
     UnknownOrderStatusError, None, None),
    ("post-502-non-json", 502, "<html>Bad Gateway</html>", "POST", ORDER, {}, UnknownOrderStatusError, None, None),
    ("post-1007", 408, _err(-1007, "Timeout waiting for response from backend server."), "POST", ORDER, {},
     UnknownOrderStatusError, None, None),
    ("post-1000", 400, _err(-1000), "POST", ALGO, {}, UnknownOrderStatusError, None, None),
    # transient (not_executed False)
    ("post-500-json-1001", 500, _err(-1001, "Internal error"), "POST", ORDER, {}, TransientError, False, None),
    ("post-1001-400", 400, _err(-1001), "POST", ORDER, {}, TransientError, False, None),
    ("get-503-service-unavailable", 503, SERVICE_UNAVAILABLE, "GET", "/fapi/v1/klines", {}, TransientError, False, None),
    ("get-503-unknown-error", 503, UNKNOWN_ERROR, "GET", ORDER, {}, TransientError, False, None),
    ("delete-502-non-json", 502, "<html>Bad Gateway</html>", "DELETE", ALGO, {}, TransientError, False, None),
    ("put-500-json", 500, _err(-1000), "PUT", ORDER, {}, TransientError, False, None),
    ("get-1000", 400, _err(-1000), "GET", ORDER, {}, TransientError, False, None),
    ("get-1001", 400, _err(-1001), "GET", ACCOUNT, {}, TransientError, False, None),
    ("delete-1007", 400, _err(-1007), "DELETE", ORDER, {}, TransientError, False, None),
    ("put-1008", 400, _err(-1008), "PUT", ORDER, {}, TransientError, False, None),
    # order-level codes
    ("-4046", 400, _err(-4046), "POST", "/fapi/v1/marginType", {}, NoChangeNeededError, None, None),
    ("-4059", 400, _err(-4059), "POST", "/fapi/v1/positionSide/dual", {}, NoChangeNeededError, None, None),
    ("-4171", 400, _err(-4171), "POST", "/fapi/v1/multiAssetsMargin", {}, NoChangeNeededError, None, None),
    ("-2018", 400, _err(-2018), "POST", ORDER, {}, InsufficientMarginError, None, None),
    ("-2019", 400, _err(-2019, "Margin is insufficient."), "POST", ORDER, {}, InsufficientMarginError, None, None),
    ("-2021", 400, _err(-2021), "POST", ALGO, {}, ImmediateTriggerError, None, None),
    ("-4142", 400, _err(-4142), "POST", ALGO, {}, ImmediateTriggerError, None, None),
    ("-2022", 400, _err(-2022), "POST", ORDER, {}, ReduceOnlyRejectedError, None, None),
    ("-4118", 400, _err(-4118), "POST", ORDER, {}, ReduceOnlyRejectedError, None, None),
    ("-2013", 400, _err(-2013), "GET", ORDER, {}, NoSuchOrderError, None, None),
    ("-2011", 400, _err(-2011), "DELETE", ORDER, {}, NoSuchOrderError, None, None),
    ("-4116", 400, _err(-4116), "POST", ORDER, {}, DuplicateClientIdError, None, None),
    ("-4045", 400, _err(-4045), "POST", ALGO, {}, AlgoLimitError, None, None),
    ("-4164", 400, _err(-4164), "POST", ORDER, {}, MinNotionalError, None, None),
    ("-4400", 400, _err(-4400), "POST", ORDER, {}, ReduceOnlyModeError, None, None),
    ("-4401", 400, _err(-4401), "POST", ORDER, {}, ReduceOnlyModeError, None, None),
    # generic order-path rejection vs. anything else
    ("order-path-4xx", 400, _err(-2010, "Order would immediately match"), "POST", ORDER, {},
     OrderRejectedError, None, None),
    ("algo-path-4xx", 400, _err(-1111, "Precision is over the maximum"), "POST", ALGO, {},
     OrderRejectedError, None, None),
    ("algo-path-2027", 400, _err(-2027), "POST", ALGO, {}, OrderRejectedError, None, None),
    ("non-order-path-4xx", 400, _err(-1102), "POST", "/fapi/v1/leverage", {}, ExchangeError, None, None),
    ("leverage-4161", 400, _err(-4161), "POST", "/fapi/v1/leverage", {}, ExchangeError, None, None),
    ("404-text", 404, "Not Found", "GET", "/fapi/v1/nothing", {}, ExchangeError, None, None),
    ("order-path-4xx-without-code", 400, "<html>bad request</html>", "POST", ORDER, {}, ExchangeError, None, None),
]


@pytest.mark.parametrize(
    ("status", "payload", "method", "path", "headers", "expected", "not_executed", "retry_after"),
    [pytest.param(*case[1:], id=case[0]) for case in ERROR_CASES],
)
def test_error_mapping(
    status: int,
    payload: Any,
    method: str,
    path: str,
    headers: dict[str, str],
    expected: type[ExchangeError],
    not_executed: bool | None,
    retry_after: float | None,
) -> None:
    err = map_error(status, payload, path=path, method=method, headers=headers)
    assert type(err) is expected
    assert err.http_status == status
    assert err.path == path
    if isinstance(payload, dict):
        assert err.code == payload["code"]
    if not_executed is not None:
        assert isinstance(err, TransientError)
        assert err.not_executed is not_executed
    if retry_after is not None:
        assert err.retry_after == pytest.approx(retry_after)


def test_error_mapping_strips_query_and_sanitizes_message() -> None:
    err = map_error(
        400,
        {"code": -1102, "msg": "bad https://x.example/fapi/v1/order?symbol=BTCUSDT&signature=abcdef0123 value"},
        path="/fapi/v1/order?symbol=BTCUSDT&timestamp=1&signature=abcdef0123",
        method="POST",
        headers={},
    )
    assert isinstance(err, OrderRejectedError)
    assert err.path == "/fapi/v1/order"
    assert "abcdef0123" not in str(err)
    assert "timestamp=" not in str(err)


# ---------------------------------------------------------------------------------------------
# retry policy
# ---------------------------------------------------------------------------------------------


def test_get_retries_on_503_service_unavailable(rsps: responses.RequestsMock, make_client: Callable[..., Any]) -> None:
    client, clock = make_client(signed=False)
    url = f"{BASE}/fapi/v1/klines"
    for _ in range(3):
        rsps.add(responses.GET, url, body=SERVICE_UNAVAILABLE, status=503, content_type="text/plain")
    rsps.add(responses.GET, url, json=[[1, "1"]])
    assert client.public_get("/fapi/v1/klines", {"symbol": "BTCUSDT"}) == [[1, "1"]]
    assert len(rsps.calls) == 4
    assert clock.sleeps == pytest.approx([0.2, 0.4, 0.8])
    assert tuple(clock.sleeps) == BACKOFF_SECONDS

    # retries exhausted -> the last TransientError is raised (1 call + 3 retries)
    rsps.reset()
    clock.sleeps.clear()
    rsps.add(responses.GET, url, body=SERVICE_UNAVAILABLE, status=503, content_type="text/plain")
    with pytest.raises(TransientError) as ei:
        client.public_get("/fapi/v1/klines", {"symbol": "BTCUSDT"})
    assert ei.value.http_status == 503
    assert len(rsps.calls) == 4
    assert clock.sleeps == pytest.approx([0.2, 0.4, 0.8])


def test_delete_5xx_non_json_is_transient_and_retried(
    rsps: responses.RequestsMock, make_client: Callable[..., Any]
) -> None:
    client, clock = make_client()
    url = f"{BASE}/fapi/v1/algoOrder"
    rsps.add(responses.DELETE, url, body="<html>502 Bad Gateway</html>", status=502, content_type="text/html")
    rsps.add(responses.DELETE, url, json={"algoId": 5, "code": "200", "msg": "success"})
    out = client.signed_request("DELETE", "/fapi/v1/algoOrder", {"symbol": "BTCUSDT", "clientAlgoId": "x-SL-1-0"})
    assert out["algoId"] == 5
    assert len(rsps.calls) == 2
    assert clock.sleeps == pytest.approx([0.2])
    # each attempt is freshly signed
    sigs = [_query(c.request.url)["signature"] for c in rsps.calls]
    assert all(sigs)


def test_post_503_service_unavailable_retried(rsps: responses.RequestsMock, make_client: Callable[..., Any]) -> None:
    client, clock = make_client()
    url = f"{BASE}/fapi/v1/order"
    rsps.add(responses.POST, url, body=SERVICE_UNAVAILABLE, status=503, content_type="text/plain")
    rsps.add(responses.POST, url, json={"code": -1008, "msg": "Request throttled by system-level protection."},
             status=503)
    rsps.add(responses.POST, url, json={"orderId": 42, "status": "FILLED"})
    out = client.signed_request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET"})
    assert out == {"orderId": 42, "status": "FILLED"}
    assert len(rsps.calls) == 3
    assert clock.sleeps == pytest.approx([0.2, 0.4])


def test_post_503_service_unavailable_exhausted_raises_not_executed(
    rsps: responses.RequestsMock, make_client: Callable[..., Any]
) -> None:
    client, clock = make_client()
    rsps.add(responses.POST, f"{BASE}/fapi/v1/order", body=SERVICE_UNAVAILABLE, status=503, content_type="text/plain")
    with pytest.raises(TransientError) as ei:
        client.signed_request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    assert ei.value.not_executed is True
    assert len(rsps.calls) == 4
    assert clock.sleeps == pytest.approx([0.2, 0.4, 0.8])


def test_post_unknown_error_not_retried(rsps: responses.RequestsMock, make_client: Callable[..., Any]) -> None:
    client, clock = make_client()
    rsps.add(responses.POST, f"{BASE}/fapi/v1/order", body=UNKNOWN_ERROR, status=503, content_type="text/plain")
    with pytest.raises(UnknownOrderStatusError):
        client.signed_request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    assert len(rsps.calls) == 1
    assert clock.sleeps == []


def test_post_transient_without_not_executed_is_raised(
    rsps: responses.RequestsMock, make_client: Callable[..., Any]
) -> None:
    client, clock = make_client()
    rsps.add(responses.POST, f"{BASE}/fapi/v1/order", json={"code": -1001, "msg": "Internal error"}, status=500)
    with pytest.raises(TransientError) as ei:
        client.signed_request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    assert ei.value.not_executed is False
    assert len(rsps.calls) == 1
    assert clock.sleeps == []


def test_post_read_timeout_is_unknown_status(rsps: responses.RequestsMock, make_client: Callable[..., Any]) -> None:
    client, clock = make_client()
    marker = "deadbeefcafe0123"
    rsps.add(
        responses.POST,
        f"{BASE}/fapi/v1/order",
        body=requests.exceptions.ReadTimeout(
            f"HTTPSConnectionPool: Read timed out. url=/fapi/v1/order?symbol=BTCUSDT&signature={marker}"
        ),
    )
    with pytest.raises(UnknownOrderStatusError) as ei:
        client.signed_request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    err = ei.value
    assert len(rsps.calls) == 1  # never retried
    assert clock.sleeps == []
    assert err.__suppress_context__ is True
    assert err.__cause__ is None
    assert err.__context__ is None
    assert err.path == "/fapi/v1/order"
    shown = "".join(traceback.format_exception(err))
    assert marker not in shown
    assert "signature=" not in shown
    assert "ReadTimeout" not in shown
    assert marker not in str(err)


def test_get_read_timeout_is_retried_as_transient(
    rsps: responses.RequestsMock, make_client: Callable[..., Any]
) -> None:
    client, clock = make_client()
    url = f"{BASE}/fapi/v2/positionRisk"
    rsps.add(responses.GET, url, body=requests.exceptions.ReadTimeout("read timed out ?signature=abc"))
    rsps.add(responses.GET, url, body=requests.exceptions.ConnectionError("connection reset ?signature=abc"))
    rsps.add(responses.GET, url, json=[])
    assert client.signed_request("GET", "/fapi/v2/positionRisk", {"symbol": "BTCUSDT"}) == []
    assert len(rsps.calls) == 3
    assert clock.sleeps == pytest.approx([0.2, 0.4])

    rsps.reset()
    clock.sleeps.clear()
    rsps.add(responses.GET, url, body=requests.exceptions.ReadTimeout("read timed out ?signature=abc"))
    with pytest.raises(TransientError) as ei:
        client.signed_request("GET", "/fapi/v2/positionRisk", {"symbol": "BTCUSDT"})
    assert ei.value.__suppress_context__ is True
    assert ei.value.__cause__ is None
    assert "signature" not in str(ei.value)
    assert len(rsps.calls) == 4


def test_connect_timeout_retried(rsps: responses.RequestsMock, make_client: Callable[..., Any]) -> None:
    client, clock = make_client()
    # POST: a connect timeout means the request never reached the server -> safe to retry
    url = f"{BASE}/fapi/v1/order"
    rsps.add(responses.POST, url, body=requests.exceptions.ConnectTimeout("connect timed out"))
    rsps.add(responses.POST, url, json={"orderId": 7})
    assert client.signed_request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"}) == {"orderId": 7}
    assert len(rsps.calls) == 2
    assert clock.sleeps == pytest.approx([0.2])

    # GET as well; exhausted -> TransientError(not_executed=True) without a chained requests exception
    rsps.reset()
    clock.sleeps.clear()
    rsps.add(responses.GET, f"{BASE}/fapi/v1/time", body=requests.exceptions.ConnectTimeout("connect timed out"))
    with pytest.raises(TransientError) as ei:
        client.public_get("/fapi/v1/time")
    assert ei.value.not_executed is True
    assert ei.value.__suppress_context__ is True
    assert len(rsps.calls) == 4
    assert clock.sleeps == pytest.approx([0.2, 0.4, 0.8])


def test_timestamp_error_resyncs_and_retries_once(
    rsps: responses.RequestsMock, make_client: Callable[..., Any]
) -> None:
    client, clock = make_client()
    account = f"{BASE}/fapi/v2/account"
    server_ms = now_ms(clock) + 2500
    rsps.add(responses.GET, f"{BASE}/fapi/v1/time", json={"serverTime": server_ms})
    rsps.add(responses.GET, account, json={"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."},
             status=400)
    rsps.add(responses.GET, account, json={"totalWalletBalance": "100"})
    assert client.signed_request("GET", "/fapi/v2/account") == {"totalWalletBalance": "100"}
    assert _paths(rsps) == ["/fapi/v2/account", "/fapi/v1/time", "/fapi/v2/account"]
    assert client.time_offset_ms == 2500
    first, second = _query(rsps.calls[0].request.url), _query(rsps.calls[2].request.url)
    assert int(second["timestamp"]) - int(first["timestamp"]) == 2500  # new timestamp ...
    assert first["signature"] != second["signature"]  # ... and new signature
    assert clock.sleeps == []

    # a second -1021 after the resync is raised (only one resync per call)
    rsps.reset()
    rsps.add(responses.GET, f"{BASE}/fapi/v1/time", json={"serverTime": server_ms})
    rsps.add(responses.GET, account, json={"code": -5028, "msg": "Timestamp outside of the ME recvWindow."},
             status=400)
    with pytest.raises(TimestampError):
        client.signed_request("GET", "/fapi/v2/account")
    assert _paths(rsps) == ["/fapi/v2/account", "/fapi/v1/time", "/fapi/v2/account"]

    # POST is resynced and re-signed once as well
    rsps.reset()
    rsps.add(responses.GET, f"{BASE}/fapi/v1/time", json={"serverTime": server_ms})
    rsps.add(responses.POST, f"{BASE}/fapi/v1/order", json={"code": -1021, "msg": "ts"}, status=400)
    rsps.add(responses.POST, f"{BASE}/fapi/v1/order", json={"orderId": 1})
    assert client.signed_request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"}) == {"orderId": 1}
    assert _paths(rsps) == ["/fapi/v1/order", "/fapi/v1/time", "/fapi/v1/order"]


def test_429_get_sleeps_retry_after(rsps: responses.RequestsMock, make_client: Callable[..., Any]) -> None:
    client, clock = make_client(signed=False)
    url = f"{BASE}/fapi/v1/klines"
    rsps.add(responses.GET, url, json={"code": -1003, "msg": "Too many requests"}, status=429,
             headers={"Retry-After": "3"})
    rsps.add(responses.GET, url, json={"code": -1003, "msg": "Too many requests"}, status=429,
             headers={"Retry-After": "120"})
    rsps.add(responses.GET, url, json=[])
    assert client.public_get("/fapi/v1/klines", {"symbol": "BTCUSDT"}) == []
    assert len(rsps.calls) == 3
    assert clock.sleeps == pytest.approx([3.0, 60.0])  # min(retry_after, 60)

    # POST is never retried on 429
    rsps.reset()
    clock.sleeps.clear()
    signed, signed_clock = make_client(signed=True)
    rsps.add(responses.POST, f"{BASE}/fapi/v1/order", json={"code": -1015, "msg": "Too many new orders"}, status=429,
             headers={"Retry-After": "1"})
    with pytest.raises(RateLimitError) as ei:
        signed.signed_request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    assert ei.value.retry_after == pytest.approx(1.0)
    assert len(rsps.calls) == 1
    assert signed_clock.sleeps == []


def test_418_raises_ip_banned_no_retry(rsps: responses.RequestsMock, make_client: Callable[..., Any]) -> None:
    client, clock = make_client(signed=False)
    rsps.add(responses.GET, f"{BASE}/fapi/v1/klines", json={"code": -1003, "msg": "banned"}, status=418,
             headers={"Retry-After": "600"})
    with pytest.raises(IpBannedError) as ei:
        client.public_get("/fapi/v1/klines", {"symbol": "BTCUSDT"})
    assert isinstance(ei.value, RateLimitError)
    assert ei.value.retry_after == pytest.approx(600.0)
    assert len(rsps.calls) == 1
    assert clock.sleeps == []


def test_other_errors_not_retried(rsps: responses.RequestsMock, make_client: Callable[..., Any]) -> None:
    client, clock = make_client()
    rsps.add(responses.DELETE, f"{BASE}/fapi/v1/order", json={"code": -2011, "msg": "Unknown order sent."},
             status=400)
    with pytest.raises(NoSuchOrderError):
        client.signed_request("DELETE", "/fapi/v1/order", {"symbol": "BTCUSDT", "origClientOrderId": "x"})
    assert len(rsps.calls) == 1
    assert clock.sleeps == []


def test_2xx_with_negative_code_is_an_error(rsps: responses.RequestsMock, make_client: Callable[..., Any]) -> None:
    client, _ = make_client()
    rsps.add(responses.POST, f"{BASE}/fapi/v1/leverage", json={"code": -4161, "msg": "Leverage reduction..."},
             status=200)
    with pytest.raises(ExchangeError) as ei:
        client.signed_request("POST", "/fapi/v1/leverage", {"symbol": "BTCUSDT", "leverage": 3})
    assert ei.value.code == -4161
    assert ei.value.http_status == 200


# ---------------------------------------------------------------------------------------------
# rate-limit headers, weight guard, time sync
# ---------------------------------------------------------------------------------------------


def test_rate_limit_headers_parsed_case_insensitive(
    rsps: responses.RequestsMock, make_client: Callable[..., Any]
) -> None:
    parsed = parse_rate_limit_headers({
        "X-MBX-USED-WEIGHT-1M": "51",
        "x-mbx-order-count-10s": "3",
        "X-Mbx-Order-Count-1M": "7",
        "Content-Type": "application/json",
        "X-MBX-USED-WEIGHT": "51",  # no interval suffix -> ignored
    })
    assert parsed == {"used-weight-1m": 51, "order-count-10s": 3, "order-count-1m": 7}
    assert parse_rate_limit_headers({}) == {}

    client, _ = make_client()
    rsps.add(responses.POST, f"{BASE}/fapi/v1/order", json={"orderId": 1},
             headers={"x-mbx-used-weight-1m": "123", "X-MBX-ORDER-COUNT-10S": "2", "x-mbx-order-count-1m": "9"})
    client.signed_request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    assert client.used_weight_1m == 123
    assert client.order_count_10s == 2
    assert client.order_count_1m == 9


def test_weight_guard_sleeps_at_80_percent(rsps: responses.RequestsMock, make_client: Callable[..., Any]) -> None:
    # 10 s into a UTC minute (1_790_769_600 is a multiple of 60)
    client, clock = make_client(signed=False, start_s=1_790_769_610.0, weight_limit=2400)
    url = f"{BASE}/fapi/v1/klines"
    rsps.add(responses.GET, url, json=[], headers={"X-MBX-USED-WEIGHT-1M": "1919"})
    client.public_get("/fapi/v1/klines")
    client.public_get("/fapi/v1/klines")  # 1919 < 1920 -> no pause
    assert clock.sleeps == []

    rsps.reset()
    rsps.add(responses.GET, url, json=[], headers={"X-MBX-USED-WEIGHT-1M": "1920"})
    client.public_get("/fapi/v1/klines")
    assert clock.sleeps == []
    assert client.used_weight_1m == 1920
    client.public_get("/fapi/v1/klines")  # >= 80 % within the same minute -> wait for the next minute + 0.5 s
    assert clock.sleeps == pytest.approx([50.5])
    assert len(rsps.calls) == 2

    # a header from a previous minute does not pause
    clock.sleeps.clear()
    clock.advance(70.0)
    client.public_get("/fapi/v1/klines")
    assert clock.sleeps == []


def test_sync_time_offset_midpoint(
    rsps: responses.RequestsMock, fixed_clock: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    clock = fixed_clock(1_000.0)
    client = BinanceRestClient(BASE, clock=clock, sleep=clock.sleep, resync_interval_sec=300.0)
    served: list[int] = []

    def time_callback(request: Any) -> tuple[int, dict[str, str], str]:
        # The server answers mid-way through a 200 ms round trip, 1500 ms ahead of the local clock:
        # first call t0=1000.0 s, t1=1000.2 s -> midpoint 1_000_100 ms, serverTime 1_001_600 -> offset +1500.
        clock.advance(0.1)
        server = now_ms(clock) + 1500
        clock.advance(0.1)
        served.append(server)
        return 200, {}, json.dumps({"serverTime": server})

    rsps.add_callback(responses.GET, f"{BASE}/fapi/v1/time", callback=time_callback)
    assert client.time_offset_ms is None
    with caplog.at_level(logging.WARNING, logger="bot.exchange.rest"):
        assert client.sync_time() == 1500
        assert client.sync_time() == 1500  # second sync shortly after: no second warning
    try:
        assert served[0] == 1_001_600
        assert client.time_offset_ms == 1500
        assert client.server_time_ms() == now_ms(clock) + 1500
        warnings = [r for r in caplog.records if "differs from Binance" in r.getMessage()]
        assert len(warnings) == 1

        # timestamp_ms(): no HTTP while fresh, resync once older than resync_interval_sec
        n = len(rsps.calls)
        client.timestamp_ms()
        assert len(rsps.calls) == n
        clock.advance(301.0)
        client.timestamp_ms()
        assert len(rsps.calls) == n + 1

        client.set_time_offset(-20)
        assert client.time_offset_ms == -20
        assert client.server_time_ms() == now_ms(clock) - 20
    finally:
        client.close()


def test_signed_request_syncs_time_first_when_never_synced(
    rsps: responses.RequestsMock, fixed_clock: Callable[..., Any]
) -> None:
    clock = fixed_clock()
    client = BinanceRestClient(BASE, FAKE_KEY, FAKE_SECRET, clock=clock, sleep=clock.sleep)
    rsps.add(responses.GET, f"{BASE}/fapi/v1/time", json={"serverTime": now_ms(clock) + 700})
    rsps.add(responses.GET, f"{BASE}/fapi/v2/account", json={})
    try:
        client.signed_request("GET", "/fapi/v2/account")
    finally:
        client.close()
    assert _paths(rsps) == ["/fapi/v1/time", "/fapi/v2/account"]
    assert int(_query(rsps.calls[1].request.url)["timestamp"]) == now_ms(clock) + 700


def test_exception_str_has_no_secret_or_query(
    rsps: responses.RequestsMock, make_client: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = make_client()
    rsps.add(responses.POST, f"{BASE}/fapi/v1/order", json={"code": -2019, "msg": "Margin is insufficient."},
             status=400)
    with caplog.at_level(logging.DEBUG, logger="bot.exchange.rest"):
        with pytest.raises(InsufficientMarginError) as ei:
            client.signed_request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT", "side": "BUY", "quantity": "0.01"})
    err = ei.value
    sig = _query(rsps.calls[0].request.url)["signature"]
    for text in (str(err), repr(err), err.msg, str(err.path), "".join(traceback.format_exception(err)), caplog.text):
        assert FAKE_SECRET not in text
        assert FAKE_KEY not in text
        assert sig not in text
        assert "signature" not in text
        assert "timestamp=" not in text
        assert "quantity=" not in text
    assert str(err) == "[400 -2019] /fapi/v1/order: Margin is insufficient."
    assert err.path == "/fapi/v1/order"
    assert "POST /fapi/v1/order -> 400" in caplog.text  # DEBUG line: method + path only

    # a URL-bearing requests exception on GET (retried, then raised) carries neither query nor signature
    rsps.reset()
    rsps.add(responses.GET, f"{BASE}/fapi/v2/account",
             body=requests.exceptions.ConnectionError(f"Max retries exceeded with url: /fapi/v2/account?signature={sig}"))
    with pytest.raises(TransientError) as ei2:
        client.signed_request("GET", "/fapi/v2/account")
    shown = "".join(traceback.format_exception(ei2.value))
    assert sig not in shown and "signature" not in shown
    assert str(ei2.value).startswith("[None None] /fapi/v2/account: GET /fapi/v2/account")
    assert "BinanceRestClient" in repr(client) and FAKE_SECRET not in repr(client)
