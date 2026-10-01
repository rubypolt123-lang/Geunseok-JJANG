"""Shared pytest fixtures (SPEC §14.1). Unit tests never touch the network."""

from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from bot import logging_setup
from bot.config import AppConfig, load_config
from bot.models import KLINE_COLUMNS, KLINE_DTYPES, SymbolFilters
from bot.storage import Storage
from bot.timeutil import interval_to_ms

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
TEST_DATA_DIR = TESTS_DIR / "data"
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"

DEFAULT_START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z

NETWORK_DISABLED_MESSAGE = "network disabled in unit tests"
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "::1", "localhost"})


# ---------------------------------------------------------------------------------------------
# Autouse safety fixtures
# ---------------------------------------------------------------------------------------------


def _is_loopback_address(address: Any) -> bool:
    """Loopback = a tuple whose host is 127.0.0.1 / ::1 / localhost. AF_UNIX and other shapes pass through."""
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        return host in _LOOPBACK_NAMES
    return True


def _host_needs_dns(host: Any) -> bool:
    """True if resolving ``host`` would query DNS (a non-loopback name that is not an IP literal)."""
    if host is None:
        return False
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str) or host == "" or host in _LOOPBACK_NAMES:
        return False
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return True
    return False


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Block every non-loopback connection unless the test is marked ``network``.

    Loopback must pass: on this CPython ``socket.socketpair`` is ``_fallback_socketpair`` (connects to
    127.0.0.1), which asyncio's Proactor loop and therefore Starlette's TestClient use.
    """
    if request.node.get_closest_marker("network") is not None:
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if not _is_loopback_address(address):
            raise RuntimeError(NETWORK_DISABLED_MESSAGE)
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        if not _is_loopback_address(address):
            raise RuntimeError(NETWORK_DISABLED_MESSAGE)
        return real_connect_ex(self, address)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        if not _is_loopback_address(address):
            raise RuntimeError(NETWORK_DISABLED_MESSAGE)
        return real_create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        # No DNS lookups either (IP literals and loopback names resolve locally).
        if _host_needs_dns(host):
            raise RuntimeError(NETWORK_DISABLED_MESSAGE)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test sees real keys or the live confirmation; tests pass ``environ={...}`` explicitly."""
    for key in (
        "CONFIRM_LIVE_TRADING",
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Close log handlers (they block tmp_path cleanup on Windows) and clear the secret registry."""
    yield
    logging_setup.shutdown_logging()
    logging_setup.clear_secrets()


# ---------------------------------------------------------------------------------------------
# Candle factories
# ---------------------------------------------------------------------------------------------


def _frame(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    start_ms: int,
    interval: str,
) -> pd.DataFrame:
    step = interval_to_ms(interval)
    n = len(closes)
    open_time = [int(start_ms) + i * step for i in range(n)]
    data = {
        "open_time": open_time,
        "open": [float(x) for x in opens],
        "high": [float(x) for x in highs],
        "low": [float(x) for x in lows],
        "close": [float(x) for x in closes],
        "volume": [1.0] * n,
        "close_time": [t + step - 1 for t in open_time],
        "quote_volume": [float(c) for c in closes],
        "trades": [1] * n,
        "taker_buy_base": [0.5] * n,
        "taker_buy_quote": [0.5 * float(c) for c in closes],
    }
    return pd.DataFrame(data, columns=list(KLINE_COLUMNS)).astype(KLINE_DTYPES)


@pytest.fixture
def candle_factory() -> Callable[..., pd.DataFrame]:
    """``make(closes, *, start_ms, interval, wick, opens)`` -> candle DataFrame passing ``validate_candles_df``.

    open = previous close (first open = first close) unless ``opens`` is given;
    high = max(o, c) * (1 + wick); low = min(o, c) * (1 - wick).
    """

    def make(
        closes: Sequence[float],
        *,
        start_ms: int = DEFAULT_START_MS,
        interval: str = "1h",
        wick: float = 0.001,
        opens: Sequence[float] | None = None,
    ) -> pd.DataFrame:
        c = [float(x) for x in closes]
        if opens is None:
            o = ([c[0]] + c[:-1]) if c else []
        else:
            if len(opens) != len(c):
                raise ValueError("opens and closes must have the same length")
            o = [float(x) for x in opens]
        highs = [max(a, b) * (1 + wick) for a, b in zip(o, c)]
        lows = [min(a, b) * (1 - wick) for a, b in zip(o, c)]
        return _frame(o, highs, lows, c, start_ms=start_ms, interval=interval)

    return make


@pytest.fixture
def ohlc_factory() -> Callable[..., pd.DataFrame]:
    """``make(rows, *, start_ms, interval)`` with explicit (open, high, low, close) tuples."""

    def make(
        rows: Sequence[tuple[float, float, float, float]],
        *,
        start_ms: int = DEFAULT_START_MS,
        interval: str = "1h",
    ) -> pd.DataFrame:
        o = [float(r[0]) for r in rows]
        h = [float(r[1]) for r in rows]
        lo = [float(r[2]) for r in rows]
        c = [float(r[3]) for r in rows]
        return _frame(o, h, lo, c, start_ms=start_ms, interval=interval)

    return make


# ---------------------------------------------------------------------------------------------
# Exchange fixtures
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def btc_filters() -> SymbolFilters:
    """Mainnet BTCUSDT filters (research §4.1)."""
    return SymbolFilters(
        symbol="BTCUSDT",
        status="TRADING",
        contract_type="PERPETUAL",
        tick_size=Decimal("0.10"),
        min_price=Decimal("556.80"),
        max_price=Decimal("4529764"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("1000"),
        market_step_size=Decimal("0.001"),
        market_min_qty=Decimal("0.001"),
        market_max_qty=Decimal("120"),
        min_notional=Decimal("50"),
        multiplier_up=Decimal("1.0500"),
        multiplier_down=Decimal("0.9500"),
        trigger_protect=Decimal("0.0500"),
        market_take_bound=Decimal("0.05"),
    )


@pytest.fixture
def exchange_info_btc() -> dict[str, Any]:
    """Mainnet exchangeInfo payload trimmed to BTCUSDT (tests/data/exchange_info_btcusdt.json)."""
    with open(TEST_DATA_DIR / "exchange_info_btcusdt.json", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------------------------
# Config / storage / clock
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    """config.example.yaml with base_dir = tmp_path (db/cache/logs go to tmp). config.yaml is never read."""
    return load_config(EXAMPLE_CONFIG, base_dir=tmp_path)


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[Storage]:
    with Storage(tmp_path / "test.db") as st:
        yield st


class FakeClock:
    """Deterministic clock/sleep pair: pass ``clock=c, sleep=c.sleep``."""

    def __init__(self, start_s: float = 1_790_769_600.0) -> None:
        self.now_s: float = float(start_s)
        self.sleeps: list[float] = []  # every requested sleep, in order

    def __call__(self) -> float:
        return self.now_s

    def advance(self, s: float) -> None:
        self.now_s += s

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.advance(s)


@pytest.fixture
def fixed_clock() -> Callable[..., FakeClock]:
    """Factory ``make(start_s=1_790_769_600.0) -> FakeClock``."""

    def make(start_s: float = 1_790_769_600.0) -> FakeClock:
        return FakeClock(start_s)

    return make
