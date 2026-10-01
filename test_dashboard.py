"""Read-only dashboard (SPEC §12.3, §14.2 U7): FastAPI ``TestClient`` against a tmp database with seeded rows."""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

import bot.dashboard.app as dash_app
from bot import models
from bot.config import AppConfig
from bot.dashboard import CDN_LIGHTWEIGHT_CHARTS, EXIT_REASON_KO, create_app, run_dashboard
from bot.errors import ConfigError
from bot.models import (
    TRADE_COLUMNS,
    AccountSnapshot,
    BacktestResult,
    BotState,
    BotStatus,
    Direction,
    ExitReason,
    Mode,
    OrderPurpose,
    Position,
    ProtectiveOrder,
    Side,
    Signal,
    SignalAction,
    Trade,
)
from bot.storage import Storage

NOW_S = 1_790_769_600.0  # FakeClock default start (LOCAL clock of the dashboard)
NOW_MS = int(NOW_S * 1000)
HOUR_MS = 3_600_000
START_MS = 1_790_726_400_000  # 2026-09-30T00:00:00Z, aligned to 1h

STATIC_DIR = Path(dash_app.__file__).resolve().parent / "static"

API_GET_PATHS = (
    "/api/health",
    "/api/meta",
    "/api/status",
    "/api/trades",
    "/api/equity",
    "/api/candles",
    "/api/events",
    "/api/backtests",
)


# ---------------------------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def clock(fixed_clock: Callable[..., Any]) -> Any:
    return fixed_clock(NOW_S)


@pytest.fixture
def db(app_config: AppConfig) -> Iterator[Storage]:
    """Writer on the dashboard's database path (``cfg.db_path`` = tmp/data/bot.db)."""
    with Storage(app_config.db_path) as st:
        yield st


@pytest.fixture
def client(app_config: AppConfig, clock: Any) -> Iterator[TestClient]:
    with TestClient(create_app(app_config, clock=clock)) as c:
        yield c


def make_trade(
    trade_id: str,
    *,
    source: str = "paper",
    run_id: str | None = None,
    symbol: str = "BTCUSDT",
    direction: Direction = Direction.LONG,
    entry_time: int = START_MS,
    exit_time: int = START_MS + 2 * HOUR_MS,
    exit_reason: ExitReason = ExitReason.SIGNAL,
    net_pnl: float = 12.5,
    r_multiple: float | None = 0.5,
) -> Trade:
    return Trade(
        trade_id=trade_id,
        source=source,
        run_id=run_id,
        symbol=symbol,
        direction=direction,
        qty=0.01,
        entry_time=entry_time,
        entry_price=84000.0,
        exit_time=exit_time,
        exit_price=85250.0,
        exit_reason=exit_reason,
        gross_pnl=net_pnl + 1.0,
        fees=0.84,
        funding=0.16,
        net_pnl=net_pnl,
        r_multiple=r_multiple,
        initial_stop=83000.0,
        take_profit=86000.0,
        leverage=3,
    )


def make_status(
    *,
    updated_at: int,
    mode: Mode = Mode.PAPER,
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    with_position: bool = True,
) -> BotStatus:
    position = None
    protective: tuple[ProtectiveOrder, ...] = ()
    if with_position:
        position = Position(
            symbol=symbol,
            qty=0.015,
            entry_price=84000.0,
            mark_price=84500.0,
            unrealized_pnl=7.5,
            liquidation_price=56250.0,
            isolated_margin=420.0,
            leverage=3,
            updated_at=updated_at,
        )
        protective = (
            ProtectiveOrder(
                kind=OrderPurpose.STOP_LOSS,
                client_id="mab1-4314-SL-1790769600-1",
                exchange_id=None,
                side=Side.SELL,
                trigger_price=82000.0,
                status="NEW",
                close_position=True,
                quantity=None,
            ),
            ProtectiveOrder(
                kind=OrderPurpose.TAKE_PROFIT,
                client_id="mab1-4314-TP-1790769600-1",
                exchange_id=None,
                side=Side.SELL,
                trigger_price=88000.0,
                status="NEW",
                close_position=True,
                quantity=None,
            ),
        )
    account = AccountSnapshot(
        ts=updated_at,
        wallet_balance=10_000.0,
        equity=10_007.5,
        available_balance=9_580.0,
        unrealized_pnl=7.5,
        position=position,
        protective_orders=protective,
    )
    return BotStatus(
        updated_at=updated_at,
        started_at=updated_at - 600_000,
        mode=mode,
        symbol=symbol,
        interval=interval,
        strategy="ma_cross",
        state=BotState.RUNNING,
        message="",
        account=account,
        last_signal=Signal(
            SignalAction.LONG, START_MS, 84000.0, "golden_cross", {"ma_fast": 1.0, "ma_slow": float("nan")}
        ),
        last_bar_open_time=START_MS,
        entries_blocked_reason="cooldown",
        pid=4242,
    )


def make_backtest(run_id: str, *, created_at: int, n_points: int = 10, n_trades: int = 2) -> BacktestResult:
    times = [START_MS + i * HOUR_MS for i in range(n_points)]
    equity = pd.DataFrame(
        {
            "time": pd.Series(times, dtype="int64"),
            "equity": pd.Series([10_000.0 + i for i in range(n_points)], dtype="float64"),
            "in_position": pd.Series([i % 2 == 0 for i in range(n_points)], dtype="bool"),
            "position_qty": pd.Series([0.0] * n_points, dtype="float64"),
        }
    )
    trades = [
        make_trade(
            f"{run_id}-{k:05d}",
            source="backtest",
            run_id=run_id,
            entry_time=START_MS + k * HOUR_MS,
            exit_time=START_MS + (k + 1) * HOUR_MS,
            exit_reason=ExitReason.TAKE_PROFIT,
        )
        for k in range(n_trades)
    ]
    metrics: dict[str, float | int | None] = {
        "total_return": 0.0123,
        "max_drawdown": 0.045,
        "sharpe": 1.25,
        "n_trades": n_trades,
        "win_rate": 0.5,
        "profit_factor": None,
        "final_equity": 10_123.0,
    }
    return BacktestResult(
        run_id=run_id,
        created_at=created_at,
        symbol="BTCUSDT",
        interval="1h",
        strategy="ma_cross",
        params={"fast_period": 20, "slow_period": 50, "ma_type": "EMA", "allow_short": True},
        config={"mode": "paper", "funding_coverage": {"included": True, "rows": 3}},
        start_time=times[0],
        end_time=times[-1] + HOUR_MS - 1,
        initial_balance=10_000.0,
        metrics=metrics,
        equity=equity,
        trades=trades,
        result_dir=None,
    )


def with_heartbeat_sec(cfg: AppConfig, heartbeat_sec: int) -> AppConfig:
    return dataclasses.replace(cfg, execution=dataclasses.replace(cfg.execution, heartbeat_sec=heartbeat_sec))


# ---------------------------------------------------------------------------------------------
# Required cases (SPEC §14.2 U7)
# ---------------------------------------------------------------------------------------------


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "time": NOW_MS}


def test_meta(client: TestClient, app_config: AppConfig) -> None:
    r = client.get("/api/meta")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "mode": "paper",
        "symbol": app_config.symbol,
        "interval": app_config.interval,
        "refresh_sec": app_config.dashboard.refresh_sec,
        "heartbeat_sec": app_config.execution.heartbeat_sec,
        "version": "0.1.0",
        "read_only": True,
        "metric_labels": models.METRIC_LABELS_KO,
        "percent_metrics": sorted(models.PERCENT_METRICS),
    }
    assert body["metric_labels"] == models.METRIC_LABELS_KO
    assert body["refresh_sec"] == 10 and body["heartbeat_sec"] == 30


def test_missing_db_returns_empty_payloads_and_creates_nothing(client: TestClient, app_config: AppConfig) -> None:
    db_path = app_config.db_path
    assert not db_path.exists() and not db_path.parent.exists()

    assert client.get("/api/status").json() == {"status": None}
    assert client.get("/api/trades").json() == {"trades": []}
    assert client.get("/api/trades?source=paper&run_id=x").json() == {"trades": []}
    assert client.get("/api/equity").json()["points"] == []
    candles = client.get("/api/candles").json()
    assert candles["candles"] == [] and candles["markers"] == []
    assert candles["symbol"] == app_config.symbol and candles["interval"] == app_config.interval
    assert client.get("/api/events").json() == {"events": []}
    assert client.get("/api/backtests").json() == {"runs": []}
    r = client.get("/api/backtests/bt-20260101-000000-abcdef")
    assert r.status_code == 404
    assert r.json() == {"detail": "backtest not found"}
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/meta").status_code == 200
    assert client.get("/").status_code == 200

    # the dashboard never creates the database, its WAL files or its folder
    assert not db_path.exists()
    assert not db_path.parent.exists()
    assert not Path(f"{db_path}-wal").exists() and not Path(f"{db_path}-shm").exists()


def test_status_empty_returns_null(client: TestClient, db: Storage) -> None:
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json() == {"status": None}


def test_status_populated_with_heartbeat_age_and_stale(client: TestClient, db: Storage) -> None:
    db.upsert_status(make_status(updated_at=NOW_MS - 12_500))
    body = client.get("/api/status").json()["status"]
    expected_keys = {
        "updated_at",
        "started_at",
        "mode",
        "symbol",
        "interval",
        "strategy",
        "state",
        "message",
        "account",
        "last_signal",
        "last_bar_open_time",
        "entries_blocked_reason",
        "pid",
        "heartbeat_age_sec",
        "stale",
        "position",
        "protective_orders",
    }
    assert set(body) == expected_keys
    assert body["heartbeat_age_sec"] == pytest.approx(12.5)
    assert isinstance(body["heartbeat_age_sec"], float)
    assert body["stale"] is False
    assert body["mode"] == "paper" and body["state"] == "RUNNING" and body["pid"] == 4242
    assert body["entries_blocked_reason"] == "cooldown"
    assert body["position"]["qty"] == pytest.approx(0.015)
    assert body["position"]["liquidation_price"] == pytest.approx(56250.0)
    assert body["position"] == body["account"]["position"]
    assert [o["kind"] for o in body["protective_orders"]] == ["STOP_LOSS", "TAKE_PROFIT"]
    assert body["protective_orders"][0]["trigger_price"] == pytest.approx(82000.0)
    assert body["last_signal"]["action"] == "LONG"
    assert body["last_signal"]["meta"]["ma_slow"] is None  # NaN -> null

    # stale = heartbeat_age_sec > max(3 * heartbeat_sec, 90): 90 s exactly is not stale, 91 s is
    db.touch_heartbeat(NOW_MS - 90_000)
    body = client.get("/api/status").json()["status"]
    assert body["heartbeat_age_sec"] == pytest.approx(90.0) and body["stale"] is False
    db.touch_heartbeat(NOW_MS - 91_000)
    body = client.get("/api/status").json()["status"]
    assert body["heartbeat_age_sec"] == pytest.approx(91.0) and body["stale"] is True

    # flat account: position null, protective orders []
    db.upsert_status(make_status(updated_at=NOW_MS - 1_000, with_position=False))
    body = client.get("/api/status").json()["status"]
    assert body["position"] is None
    assert body["protective_orders"] == []
    assert body["stale"] is False


def test_status_stale_threshold_scales_with_heartbeat_sec(app_config: AppConfig, clock: Any, db: Storage) -> None:
    cfg = with_heartbeat_sec(app_config, 60)  # threshold max(180, 90) = 180 s
    db.upsert_status(make_status(updated_at=NOW_MS - 150_000))
    with TestClient(create_app(cfg, clock=clock)) as c:
        assert c.get("/api/status").json()["status"]["stale"] is False
        db.touch_heartbeat(NOW_MS - 181_000)
        assert c.get("/api/status").json()["status"]["stale"] is True
    assert dash_app.heartbeat_stale_after_sec(5) == 90.0
    assert dash_app.heartbeat_stale_after_sec(30) == 90.0
    assert dash_app.heartbeat_stale_after_sec(600) == 1800.0


def test_trades_filter_by_source(client: TestClient, db: Storage) -> None:
    db.insert_trades(
        [
            make_trade("p1", source="paper", exit_time=START_MS + HOUR_MS),
            make_trade("p2", source="paper", exit_time=START_MS + 3 * HOUR_MS),
            make_trade("t1", source="testnet", exit_time=START_MS + 2 * HOUR_MS),
            make_trade("b1", source="backtest", run_id="bt-a"),
            make_trade("b2", source="backtest", run_id="bt-b"),
        ]
    )
    # default source = cfg.mode (no status row yet) -> paper, newest exit first
    trades = client.get("/api/trades").json()["trades"]
    assert [t["trade_id"] for t in trades] == ["p2", "p1"]
    assert all(list(t) == list(TRADE_COLUMNS) for t in trades)
    assert trades[0]["entry_time"] == START_MS  # table rows keep ms fields
    assert trades[0]["direction"] == "LONG" and trades[0]["exit_reason"] == "SIGNAL"

    assert [t["trade_id"] for t in client.get("/api/trades?source=testnet").json()["trades"]] == ["t1"]
    assert {t["trade_id"] for t in client.get("/api/trades?source=backtest").json()["trades"]} == {"b1", "b2"}
    assert [t["trade_id"] for t in client.get("/api/trades?source=backtest&run_id=bt-b").json()["trades"]] == ["b2"]
    assert [t["trade_id"] for t in client.get("/api/trades?source=paper&limit=1").json()["trades"]] == ["p2"]
    # blank query values mean "not given"
    assert [t["trade_id"] for t in client.get("/api/trades?source=&run_id=").json()["trades"]] == ["p2", "p1"]

    # default source follows the running trader's mode (status row)
    db.upsert_status(make_status(updated_at=NOW_MS, mode=Mode.TESTNET))
    assert [t["trade_id"] for t in client.get("/api/trades").json()["trades"]] == ["t1"]


def test_trades_limit_bounds(client: TestClient, db: Storage) -> None:
    assert client.get("/api/trades?limit=0").status_code == 422
    assert client.get("/api/trades?limit=2001").status_code == 422
    assert client.get("/api/trades?limit=2000").status_code == 200
    assert client.get("/api/trades?limit=1").status_code == 200


def test_equity_points_seconds(client: TestClient, db: Storage) -> None:
    for i in (2, 0, 1):  # inserted out of order
        db.append_equity("paper", START_MS + i * HOUR_MS, 10_000.0 + i, 9_990.0 + i)
    db.append_equity("testnet", START_MS, 5_000.0, 5_000.0)

    body = client.get("/api/equity").json()
    assert body["mode"] == "paper"
    assert body["points"] == [
        {"time": (START_MS + i * HOUR_MS) // 1000, "equity": 10_000.0 + i, "wallet": 9_990.0 + i} for i in range(3)
    ]
    assert all(isinstance(p["time"], int) for p in body["points"])

    body = client.get("/api/equity?mode=testnet").json()
    assert body == {"mode": "testnet", "points": [{"time": START_MS // 1000, "equity": 5_000.0, "wallet": 5_000.0}]}
    # limit keeps the LAST points, still ascending
    body = client.get("/api/equity?limit=2").json()
    assert [p["time"] for p in body["points"]] == [(START_MS + HOUR_MS) // 1000, (START_MS + 2 * HOUR_MS) // 1000]

    # default mode follows the status row
    db.upsert_status(make_status(updated_at=NOW_MS, mode=Mode.TESTNET))
    assert client.get("/api/equity").json()["mode"] == "testnet"


def test_candles_and_markers_sorted(
    client: TestClient, db: Storage, candle_factory: Callable[..., pd.DataFrame]
) -> None:
    closes = [84_000.0 + 10 * i for i in range(10)]
    df = candle_factory(closes, start_ms=START_MS, interval="1h")
    assert db.upsert_candles("BTCUSDT", "1h", df) == 10
    db.upsert_candles("ETHUSDT", "1h", candle_factory([3_000.0] * 3, start_ms=START_MS, interval="1h"))

    db.insert_trades(
        [
            # short, entry and exit inside the range (inserted first on purpose)
            make_trade(
                "s1",
                direction=Direction.SHORT,
                entry_time=START_MS + 6 * HOUR_MS,
                exit_time=START_MS + 8 * HOUR_MS,
                exit_reason=ExitReason.TAKE_PROFIT,
            ),
            # long, exit mid-bar (stop fill time) -> floored to the bar open
            make_trade(
                "l1",
                direction=Direction.LONG,
                entry_time=START_MS + 2 * HOUR_MS,
                exit_time=START_MS + 5 * HOUR_MS + 1_800_000,
                exit_reason=ExitReason.STOP_LOSS,
            ),
            # entered before the range: only the exit marker is shown
            make_trade(
                "l0",
                direction=Direction.LONG,
                entry_time=START_MS - 3 * HOUR_MS,
                exit_time=START_MS + HOUR_MS,
                exit_reason=ExitReason.SIGNAL,
            ),
            # entirely before the range
            make_trade("old", entry_time=START_MS - 5 * HOUR_MS, exit_time=START_MS - 2 * HOUR_MS),
            # other source / other symbol are never shown
            make_trade("tn", source="testnet", entry_time=START_MS + 3 * HOUR_MS, exit_time=START_MS + 4 * HOUR_MS),
            make_trade("eth", symbol="ETHUSDT", entry_time=START_MS + 3 * HOUR_MS, exit_time=START_MS + 4 * HOUR_MS),
        ]
    )

    body = client.get("/api/candles").json()
    assert body["symbol"] == "BTCUSDT" and body["interval"] == "1h"
    assert len(body["candles"]) == 10
    first = body["candles"][0]
    assert set(first) == {"time", "open", "high", "low", "close"}
    assert first["time"] == START_MS // 1000
    assert first["close"] == pytest.approx(84_000.0)
    times = [c["time"] for c in body["candles"]]
    assert times == sorted(times) and len(set(times)) == len(times)

    sec = lambda ms: ms // 1000  # noqa: E731
    assert body["markers"] == [
        {"time": sec(START_MS + HOUR_MS), "position": "aboveBar", "shape": "circle", "color": "#607d8b",
         "text": "신호 청산"},
        {"time": sec(START_MS + 2 * HOUR_MS), "position": "belowBar", "shape": "arrowUp", "color": "#26a69a",
         "text": "롱 진입"},
        {"time": sec(START_MS + 5 * HOUR_MS), "position": "aboveBar", "shape": "circle", "color": "#607d8b",
         "text": "손절"},
        {"time": sec(START_MS + 6 * HOUR_MS), "position": "aboveBar", "shape": "arrowDown", "color": "#ef5350",
         "text": "숏 진입"},
        {"time": sec(START_MS + 8 * HOUR_MS), "position": "belowBar", "shape": "circle", "color": "#607d8b",
         "text": "익절"},
    ]
    marker_times = [m["time"] for m in body["markers"]]
    assert marker_times == sorted(marker_times)

    # limit keeps the most recent candles; markers are restricted to that window
    body = client.get("/api/candles?limit=3").json()
    assert [c["time"] for c in body["candles"]] == [sec(START_MS + h * HOUR_MS) for h in (7, 8, 9)]
    assert body["markers"] == [
        {"time": sec(START_MS + 8 * HOUR_MS), "position": "belowBar", "shape": "circle", "color": "#607d8b",
         "text": "익절"},
    ]

    # symbol/interval come from the status row when one exists; markers use status.mode as the source
    db.upsert_status(make_status(updated_at=NOW_MS, mode=Mode.TESTNET, symbol="ETHUSDT"))
    body = client.get("/api/candles").json()
    assert body["symbol"] == "ETHUSDT" and len(body["candles"]) == 3
    assert body["markers"] == []


def test_build_markers_unknown_reason_and_same_bar_order() -> None:
    trades = [
        {"symbol": "BTCUSDT", "direction": "LONG", "entry_time": START_MS + 10, "exit_time": START_MS + 20,
         "exit_reason": "SOMETHING_NEW"},
        {"symbol": "BTCUSDT", "direction": "FLAT", "entry_time": START_MS, "exit_time": START_MS,
         "exit_reason": "SIGNAL"},
    ]
    markers = dash_app.build_markers(
        trades, symbol="BTCUSDT", interval="1h", range_start_ms=START_MS, range_end_ms=START_MS + HOUR_MS - 1
    )
    assert [m["shape"] for m in markers] == ["arrowUp", "circle"]  # same bar: entry before exit
    assert all(m["time"] == START_MS // 1000 for m in markers)
    assert markers[1]["text"] == "SOMETHING_NEW"  # unmapped reason shown raw
    assert set(EXIT_REASON_KO) == {r.value for r in ExitReason}


def test_events(client: TestClient, db: Storage) -> None:
    db.log_event("INFO", "paper", "STARTUP", "started", ts_ms=1_000)
    db.log_event("CRITICAL", "paper", "KILL_SWITCH", "킬스위치 발동", ts_ms=3_000)
    db.log_event("WARNING", "testnet", "STALE_DATA", "stale", ts_ms=2_000)

    events = client.get("/api/events").json()["events"]
    assert [e["ts"] for e in events] == [3_000, 2_000, 1_000]  # newest first
    assert events[0] == {"ts": 3_000, "level": "CRITICAL", "mode": "paper", "kind": "KILL_SWITCH",
                         "message": "킬스위치 발동"}
    assert all(set(e) == {"ts", "level", "mode", "kind", "message"} for e in events)
    assert [e["ts"] for e in client.get("/api/events?limit=2").json()["events"]] == [3_000, 2_000]
    assert [e["kind"] for e in client.get("/api/events?mode=testnet").json()["events"]] == ["STALE_DATA"]
    assert client.get("/api/events?limit=0").status_code == 422


def test_backtests_list_and_detail(client: TestClient, db: Storage) -> None:
    db.save_backtest(make_backtest("bt-20260930-000000-aaaaaa", created_at=NOW_MS - 10_000))
    db.save_backtest(make_backtest("bt-20260930-010000-bbbbbb", created_at=NOW_MS, n_points=4, n_trades=3))
    db.insert_trade(make_trade("paper-1", source="paper"))

    runs = client.get("/api/backtests").json()["runs"]
    assert [r["run_id"] for r in runs] == ["bt-20260930-010000-bbbbbb", "bt-20260930-000000-aaaaaa"]
    assert set(runs[0]) == {
        "run_id", "created_at", "symbol", "interval", "strategy", "params", "start_time", "end_time",
        "initial_balance", "metrics",
    }
    assert runs[0]["params"]["fast_period"] == 20
    assert runs[0]["metrics"]["total_return"] == pytest.approx(0.0123)
    assert runs[0]["metrics"]["profit_factor"] is None
    assert [r["run_id"] for r in client.get("/api/backtests?limit=1").json()["runs"]] == ["bt-20260930-010000-bbbbbb"]

    r = client.get("/api/backtests/bt-20260930-000000-aaaaaa")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"run", "equity", "trades"}
    run = body["run"]
    assert run["run_id"] == "bt-20260930-000000-aaaaaa"
    assert run["config"]["funding_coverage"]["included"] is True
    assert run["initial_balance"] == pytest.approx(10_000.0)
    assert run["start_time"] == START_MS  # ms kept in table data
    assert body["equity"] == [{"time": (START_MS + i * HOUR_MS) // 1000, "equity": 10_000.0 + i} for i in range(10)]
    assert len(body["trades"]) == 2
    assert all(t["source"] == "backtest" and t["run_id"] == "bt-20260930-000000-aaaaaa" for t in body["trades"])
    assert all(list(t) == list(TRADE_COLUMNS) for t in body["trades"])
    assert {t["exit_reason"] for t in body["trades"]} == {"TAKE_PROFIT"}


def test_backtest_404(client: TestClient, db: Storage) -> None:
    db.save_backtest(make_backtest("bt-20260930-000000-aaaaaa", created_at=NOW_MS))
    r = client.get("/api/backtests/bt-does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"detail": "backtest not found"}
    assert r.headers["cache-control"] == "no-store"


def test_equity_downsampled_over_5000(client: TestClient, db: Storage) -> None:
    n = 12_002  # step = ceil(12002 / 5000) = 3 -> indices 0, 3, ..., 12000 (4001 points) + the last one
    db.save_backtest(make_backtest("bt-big", created_at=NOW_MS, n_points=n, n_trades=0))
    equity = client.get("/api/backtests/bt-big").json()["equity"]
    assert len(equity) == 4002
    times = [p["time"] for p in equity]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert times[0] == START_MS // 1000
    assert times[-1] == (START_MS + (n - 1) * HOUR_MS) // 1000  # the last point is always kept
    assert times[1] == (START_MS + 3 * HOUR_MS) // 1000
    assert equity[-2]["time"] == (START_MS + 12_000 * HOUR_MS) // 1000

    # exactly 5000 points: untouched
    db.save_backtest(make_backtest("bt-5000", created_at=NOW_MS, n_points=5_000, n_trades=0))
    assert len(client.get("/api/backtests/bt-5000").json()["equity"]) == 5_000


def test_downsample_points_rules() -> None:
    pts = list(range(5_000))
    assert dash_app.downsample_points(pts) == pts
    pts = list(range(5_001))  # step 2: 0..5000 even -> last already included
    out = dash_app.downsample_points(pts)
    assert out == list(range(0, 5_001, 2)) and len(out) == 2_501
    pts = list(range(10_002))  # step 3: last index 10001 not on the grid -> appended
    out = dash_app.downsample_points(pts)
    assert out[-1] == 10_001 and out[-2] == 9_999 and len(out) == 3_335
    assert len(out) <= 5_001
    assert dash_app.downsample_points([]) == []


def test_index_html_korean_and_cdn(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert '<html lang="ko">' in html
    assert "<title>바이낸스 선물 자동매매 대시보드</title>" in html
    assert CDN_LIGHTWEIGHT_CHARTS in html
    assert CDN_LIGHTWEIGHT_CHARTS == (
        "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"
    )
    assert "/static/style.css" in html and "/static/app.js" in html
    # sections (SPEC order) and footer
    sections = ["봇 상태", "포지션", "보호 주문", "캔들 차트", "자산 곡선", "거래 내역", "최근 이벤트", "백테스트 결과"]
    positions = [html.index(f">{s}<") for s in sections]
    assert positions == sorted(positions)
    for label in ("읽기 전용", "하트비트", "지갑 잔고", "평가 자산", "사용 가능 잔고", "미실현 손익", "마지막 신호",
                  "진입 차단 사유", "포지션 없음", "트리거 가격", "주문 ID", "청산 사유", "순손익", "실행 ID", "샤프 지수"):
        assert label in html
    assert "본 소프트웨어는 투자 조언이 아니며, 모든 거래의 책임은 사용자에게 있습니다." in html
    # read-only page: no forms or write controls
    assert "<form" not in html.lower()


def test_static_assets_served(client: TestClient) -> None:
    css = client.get("/static/style.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert js.headers["cache-control"] == "no-cache"
    text = js.text
    assert "차트 라이브러리를 불러오지 못했습니다" in text
    assert "서버 연결 실패" in text
    assert "Asia/Seoul" in text and "timeFormatter" in text
    assert "/api/meta" in text and "metric_labels" in text
    assert client.get("/static/missing.js").status_code == 404


def test_app_js_is_read_only_and_uses_served_metric_labels() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"method\s*:", js)
    for verb in ("'POST'", '"POST"', "'PUT'", "'DELETE'", "'PATCH'"):
        assert verb not in js
    leaked = [label for label in models.METRIC_LABELS_KO.values() if label in js]
    assert leaked == []  # metric labels come from /api/meta only


def test_exit_reason_labels_match_app_js() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const EXIT_REASON_KO = \{(.*?)\};", js, re.S)
    assert block is not None
    js_map = dict(re.findall(r"([A-Z_]+):\s*'([^']*)'", block.group(1)))
    assert js_map == EXIT_REASON_KO
    for label in ("시작 중", "실행 중", "신규 진입 중지", "킬스위치 발동", "오류", "정지됨", "페이퍼(모의)", "테스트넷(데모)",
                  "실거래", "응답 없음"):
        assert label in js


def _walk_routes(routes: Any) -> Iterator[Any]:
    """Every route, descending into included routers (``original_router``) and sub-applications."""
    for route in routes or ():
        yield route
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _walk_routes(nested.routes)
        if not isinstance(route, Mount):
            continue
        yield from _walk_routes(getattr(route, "routes", None))


def test_only_get_routes(app_config: AppConfig) -> None:
    app = create_app(app_config)
    methods: set[str] = set()
    paths: set[str] = set()
    for route in _walk_routes(app.routes):
        route_methods = getattr(route, "methods", None)
        if route_methods:
            methods |= set(route_methods)
        paths.add(getattr(route, "path", ""))
    # the API routes are registered directly on the app, so app.routes itself lists them
    assert {getattr(r, "path", None) for r in app.routes if getattr(r, "methods", None)} >= set(API_GET_PATHS)
    assert methods, "no routes found"
    assert methods <= {"GET", "HEAD"}
    assert not methods & {"POST", "PUT", "PATCH", "DELETE"}
    assert {*API_GET_PATHS, "/", "/api/backtests/{run_id}"} <= paths
    mounts = [r for r in app.routes if isinstance(r, Mount)]
    assert [m.path for m in mounts] == ["/static"]
    assert app.docs_url is None and app.openapi_url is None


def test_post_returns_405(client: TestClient, db: Storage) -> None:
    r = client.post("/api/status")
    assert r.status_code == 405
    for path in (*API_GET_PATHS, "/api/backtests/x", "/"):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert client.request(method, path).status_code == 405, (method, path)
    assert client.post("/static/app.js").status_code == 405
    assert db.get_status() is None  # nothing written


def test_cache_control_no_store(client: TestClient, db: Storage) -> None:
    db.save_backtest(make_backtest("bt-x", created_at=NOW_MS))
    for path in (*API_GET_PATHS, "/api/backtests/bt-x", "/api/backtests/missing", "/api/trades?limit=0"):
        r = client.get(path)
        assert r.headers.get("cache-control") == "no-store", path
    assert client.post("/api/status").headers.get("cache-control") == "no-store"
    assert client.get("/").headers.get("cache-control") == "no-cache"


def test_cache_control_no_store_without_db(client: TestClient) -> None:
    for path in API_GET_PATHS:
        assert client.get(path).headers.get("cache-control") == "no-store", path


def test_foreign_host_header_rejected(client: TestClient) -> None:
    for host in ("evil.com", "evil.com:8000", "127.0.0.1.evil.com", "localhost.evil.com:8000", "192.168.0.10:8000"):
        r = client.get("/api/health", headers={"host": host})
        assert r.status_code == 400, host
    assert client.get("/", headers={"host": "evil.com"}).status_code == 400
    for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "localhost", "testserver"):
        assert client.get("/api/health", headers={"host": host}).status_code == 200, host


def test_storage_dependency_is_read_only(
    client: TestClient, db: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.upsert_status(make_status(updated_at=NOW_MS))
    opened: list[bool] = []
    real_storage = dash_app.Storage

    class SpyStorage(real_storage):  # type: ignore[misc, valid-type]
        def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
            opened.append(read_only)
            super().__init__(db_path, read_only=read_only)

    monkeypatch.setattr(dash_app, "Storage", SpyStorage)
    for path in API_GET_PATHS:
        assert client.get(path).status_code == 200
    assert opened and all(opened)


def test_unreadable_db_returns_empty_payloads(client: TestClient, app_config: AppConfig) -> None:
    # SPEC-GAP behaviour: a file that is not a bot database is treated like a missing one
    app_config.db_path.parent.mkdir(parents=True)
    app_config.db_path.write_bytes(b"this is not an sqlite database" * 10)
    assert client.get("/api/status").json() == {"status": None}
    assert client.get("/api/trades").json() == {"trades": []}
    assert client.get("/api/backtests/x").status_code == 404
    assert app_config.db_path.read_bytes() == b"this is not an sqlite database" * 10


def test_responses_contain_no_nan(client: TestClient, db: Storage) -> None:
    db.insert_trade(make_trade("nan-r", r_multiple=float("nan")))
    trades = client.get("/api/trades").json()["trades"]
    assert trades[0]["r_multiple"] is None
    for value in trades[0].values():
        assert not (isinstance(value, float) and math.isnan(value))


def test_run_dashboard_rejects_non_loopback(app_config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    for host in ("0.0.0.0", "192.168.0.10", "example.com", "::", ""):
        with pytest.raises(ConfigError):
            run_dashboard(app_config, host, 8000)
    with pytest.raises(ConfigError):
        run_dashboard(app_config, "127.0.0.1", 0)
    assert calls == []


def test_run_dashboard_passes_log_config_none(app_config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    run_dashboard(app_config, "127.0.0.1", 8123)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert "log_config" in kwargs and kwargs["log_config"] is None
    assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == 8123
    assert kwargs["access_log"] is False
    assert kwargs["log_level"] == "info"
    app = args[0] if args else kwargs["app"]
    assert isinstance(app, FastAPI)
    for host in ("localhost", "::1"):
        run_dashboard(app_config, host, 8000)
    assert [k["host"] for _, k in calls] == ["127.0.0.1", "localhost", "::1"]
    assert all(k["log_config"] is None for _, k in calls)
    assert not app_config.db_path.exists()  # starting the server never creates the database
