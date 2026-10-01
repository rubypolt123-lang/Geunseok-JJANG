"""Tests for bot/storage.py (SPEC §5, §14.2 U1)."""

from __future__ import annotations

import gc
import re
import sqlite3
import warnings
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bot.errors import DataError
from bot.models import (
    TRADE_COLUMNS,
    AccountSnapshot,
    Action,
    BacktestResult,
    BotState,
    BotStatus,
    Direction,
    ExitReason,
    Mode,
    OrderPurpose,
    OrderResult,
    OrderStatus,
    OrderType,
    Position,
    ProtectiveOrder,
    Side,
    Signal,
    SignalAction,
    Trade,
)
from bot.storage import SCHEMA_VERSION, Storage

BAR_MS = 1_790_769_600_000
HOUR = 3_600_000

EXPECTED_TABLES = {
    "schema_version",
    "kv_state",
    "bot_status",
    "signals",
    "orders",
    "trades",
    "equity",
    "candles",
    "backtest_runs",
    "backtest_equity",
    "events",
}


def raw_connect(path: Path) -> closing[sqlite3.Connection]:
    return closing(sqlite3.connect(path))


def make_trade(trade_id: str, *, source: str = "paper", run_id: str | None = None, exit_time: int = BAR_MS) -> Trade:
    return Trade(
        trade_id=trade_id,
        source=source,
        run_id=run_id,
        symbol="BTCUSDT",
        direction=Direction.LONG,
        qty=0.1,
        entry_time=exit_time - HOUR,
        entry_price=50_000.0,
        exit_time=exit_time,
        exit_price=51_000.0,
        exit_reason=ExitReason.TAKE_PROFIT,
        gross_pnl=100.0,
        fees=5.0,
        funding=1.0,
        net_pnl=94.0,
        r_multiple=0.85,
        initial_stop=49_000.0,
        take_profit=51_000.0,
        leverage=3,
    )


def make_status(**overrides: object) -> BotStatus:
    account = AccountSnapshot(
        ts=BAR_MS,
        wallet_balance=10_000.0,
        equity=10_050.0,
        available_balance=8_000.0,
        unrealized_pnl=50.0,
        position=Position("BTCUSDT", 0.1, 50_000.0, 50_500.0, 50.0, 33_636.06, 1_666.67, 3, BAR_MS),
        protective_orders=(
            ProtectiveOrder(OrderPurpose.STOP_LOSS, "mab1-4314-SL-1790769600-1", "111", Side.SELL, 49_000.0, "NEW", True, None),
            ProtectiveOrder(OrderPurpose.TAKE_PROFIT, "mab1-4314-TP-1790769600-1", "112", Side.SELL, 52_000.0, "NEW", True, None),
        ),
        open_orders_count=0,
    )
    values: dict[str, object] = dict(
        updated_at=1_790_770_000_000,
        started_at=1_790_769_000_000,
        mode=Mode.PAPER,
        symbol="BTCUSDT",
        interval="1h",
        strategy="ma_cross(fast_period=20, slow_period=50)",
        state=BotState.RUNNING,
        message="",
        account=account,
        last_signal=Signal(SignalAction.LONG, BAR_MS - HOUR, 50_000.0, "golden_cross", {"ma_fast": 1.0, "ma_slow": float("nan")}),
        last_bar_open_time=BAR_MS - HOUR,
        entries_blocked_reason=None,
        pid=4242,
    )
    values.update(overrides)
    return BotStatus(**values)  # type: ignore[arg-type]


def make_backtest(run_id: str, *, created_at: int, n: int = 5) -> BacktestResult:
    times = [BAR_MS + i * HOUR for i in range(n)]
    equity = pd.DataFrame(
        {
            "time": np.array(times, dtype="int64"),
            "equity": np.linspace(10_000.0, 10_400.0, n),
            "in_position": [False, True, True, False, False][:n],
            "position_qty": [0.0, 0.1, 0.1, 0.0, 0.0][:n],
        }
    )
    trades = [make_trade(f"{run_id}-00000", source="backtest", run_id=run_id, exit_time=times[2])]
    return BacktestResult(
        run_id=run_id,
        created_at=created_at,
        symbol="BTCUSDT",
        interval="1h",
        strategy="ma_cross",
        params={"fast_period": 20, "slow_period": 50},
        config={"mode": "paper", "funding_coverage": {"included": True, "rows": 3}},
        start_time=times[0],
        end_time=times[-1] + HOUR - 1,
        initial_balance=10_000.0,
        metrics={"total_return": 0.04, "sharpe": np.float64(1.5), "profit_factor": None, "n_trades": np.int64(1), "bad": float("nan")},
        equity=equity,
        trades=trades,
        result_dir=None,
    )


# ---------------------------------------------------------------------------------------------
# Required cases
# ---------------------------------------------------------------------------------------------


def test_schema_and_wal_mode(tmp_path: Path) -> None:
    db = tmp_path / "sub" / "bot.db"
    with Storage(db) as st:
        assert st.path == db
        assert db.exists()
        st.init_schema()  # idempotent
    with raw_connect(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert EXPECTED_TABLES <= tables
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"idx_trades_source_exit", "idx_events_ts"} <= indexes
        assert conn.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
    # reopening does not duplicate the version row
    with Storage(db):
        pass
    with raw_connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_schema_version_mismatch_rejected(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    with Storage(db):
        pass
    with raw_connect(db) as conn:
        conn.execute("UPDATE schema_version SET version = 2")
        conn.commit()
    with pytest.raises(DataError, match="schema version 2"):
        Storage(db)
    with pytest.raises(DataError, match="schema version 2"):
        Storage(db, read_only=True)


def test_status_roundtrip_and_heartbeat(storage: Storage) -> None:
    assert storage.get_status() is None
    status = make_status()
    storage.upsert_status(status)
    got = storage.get_status()
    assert got is not None
    assert list(got) == [
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
    ]
    assert got["mode"] == "paper" and got["state"] == "RUNNING" and got["pid"] == 4242
    assert got["updated_at"] == 1_790_770_000_000 and got["started_at"] == 1_790_769_000_000
    assert got["account"]["equity"] == 10_050.0
    assert got["account"]["position"]["qty"] == 0.1
    assert [o["kind"] for o in got["account"]["protective_orders"]] == ["STOP_LOSS", "TAKE_PROFIT"]
    assert got["last_signal"]["action"] == "LONG"
    assert got["last_signal"]["meta"] == {"ma_fast": 1.0, "ma_slow": None}
    assert got["last_bar_open_time"] == BAR_MS - HOUR and got["entries_blocked_reason"] is None

    storage.touch_heartbeat(1_790_770_030_000)
    got2 = storage.get_status()
    assert got2 is not None and got2["updated_at"] == 1_790_770_030_000
    assert got2["started_at"] == 1_790_769_000_000

    # single row; replace keeps id 1
    storage.upsert_status(
        make_status(state=BotState.HALTED, account=None, last_signal=None, entries_blocked_reason="halt_file", message="x")
    )
    got3 = storage.get_status()
    assert got3 is not None and got3["state"] == "HALTED" and got3["account"] is None and got3["last_signal"] is None
    assert got3["entries_blocked_reason"] == "halt_file" and got3["message"] == "x"
    with raw_connect(storage.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM bot_status").fetchone()[0] == 1


def test_kv_state_roundtrip(storage: Storage) -> None:
    assert storage.get_state("missing") is None
    storage.set_state("active_trade:paper:BTCUSDT", {"qty": 0.1, "direction": Direction.LONG, "tp": None})
    assert storage.get_state("active_trade:paper:BTCUSDT") == {"qty": 0.1, "direction": "LONG", "tp": None}
    storage.set_state("last_bar:paper:BTCUSDT:1h", BAR_MS)
    assert storage.get_state("last_bar:paper:BTCUSDT:1h") == BAR_MS
    storage.set_state("last_bar:paper:BTCUSDT:1h", BAR_MS + HOUR)  # overwrite
    assert storage.get_state("last_bar:paper:BTCUSDT:1h") == BAR_MS + HOUR
    storage.set_state("list", [1, "킬스위치", 2.5])
    assert storage.get_state("list") == [1, "킬스위치", 2.5]
    storage.delete_state("list")
    assert storage.get_state("list") is None
    storage.delete_state("never-existed")  # no error


def test_set_state_accepts_numpy_int(storage: Storage) -> None:
    storage.set_state("n", np.int64(5))
    value = storage.get_state("n")
    assert value == 5 and type(value) is int
    storage.set_state("nested", {"a": np.int64(1), "b": np.float64("nan"), "c": np.bool_(False), "d": [np.float32(0.5)]})
    assert storage.get_state("nested") == {"a": 1, "b": None, "c": False, "d": [0.5]}


def test_record_signal_idempotent(storage: Storage) -> None:
    sig1 = Signal(SignalAction.LONG, BAR_MS, 50_000.0, "golden_cross", {"ma_fast": 1.0})
    storage.record_signal("paper", "BTCUSDT", "1h", sig1, Action.OPEN_LONG, 1_790_773_203_000)
    storage.record_signal("paper", "BTCUSDT", "1h", sig1, Action.NONE, 1_790_773_204_000)
    sig2 = Signal(SignalAction.NONE, BAR_MS + HOUR, 50_100.0, "no_cross")
    storage.record_signal(Mode.PAPER, "BTCUSDT", "1h", sig2, Action.NONE, 1_790_776_803_000)
    storage.record_signal("testnet", "BTCUSDT", "1h", sig1, Action.OPEN_LONG, 1_790_773_203_000)
    rows = storage.recent_signals("paper")
    assert [r["bar_open_time"] for r in rows] == [BAR_MS + HOUR, BAR_MS]  # newest first
    first = rows[1]
    assert first["action"] == "LONG" and first["decided_action"] == "OPEN_LONG"  # first insert kept
    assert first["created_at"] == 1_790_773_203_000
    assert first["meta"] == {"ma_fast": 1.0} and first["reason"] == "golden_cross" and first["price"] == 50_000.0
    assert len(storage.recent_signals("paper", limit=1)) == 1
    assert len(storage.recent_signals("testnet")) == 1


def test_numpy_bar_time_stored_as_integer(storage: Storage) -> None:
    sig = Signal(SignalAction.SHORT, np.int64(BAR_MS), np.float64(50_000.0), "dead_cross")
    storage.record_signal("paper", "BTCUSDT", "1h", sig, Action.OPEN_SHORT, np.int64(BAR_MS + 3_000))
    storage.record_signal(
        "paper", "BTCUSDT", "1h", Signal(SignalAction.SHORT, BAR_MS, 1.0, "dup"), Action.NONE, BAR_MS + 4_000
    )
    with raw_connect(storage.path) as conn:
        rows = conn.execute(
            "SELECT typeof(bar_open_time), typeof(created_at), typeof(price), bar_open_time, reason FROM signals"
        ).fetchall()
    assert rows == [("integer", "integer", "real", BAR_MS, "dead_cross")]  # native-int duplicate ignored
    # the sqlite3 adapters registered by bot.storage cover raw numpy binds too
    with closing(sqlite3.connect(":memory:")) as conn:
        kinds = conn.execute(
            "SELECT typeof(?), typeof(?), typeof(?), typeof(?), typeof(?)",
            (np.int64(1), np.int32(2), np.float64(1.5), np.float32(2.5), np.bool_(True)),
        ).fetchone()
    assert kinds == ("integer", "integer", "real", "real", "integer")


def test_trades_insert_and_filter(storage: Storage) -> None:
    storage.insert_trade(make_trade("p1", exit_time=BAR_MS))
    storage.insert_trades(
        [
            make_trade("p2", exit_time=BAR_MS + 2 * HOUR),
            make_trade("b1", source="backtest", run_id="bt-1", exit_time=BAR_MS + HOUR),
            make_trade("b2", source="backtest", run_id="bt-2", exit_time=BAR_MS + 3 * HOUR),
        ]
    )
    storage.insert_trades([])  # no-op
    all_rows = storage.list_trades()
    assert [r["trade_id"] for r in all_rows] == ["b2", "p2", "b1", "p1"]  # newest exit_time first
    assert all(tuple(r) == TRADE_COLUMNS for r in all_rows)
    assert [r["trade_id"] for r in storage.list_trades(source="paper")] == ["p2", "p1"]
    assert [r["trade_id"] for r in storage.list_trades(source="backtest", run_id="bt-1")] == ["b1"]
    assert [r["trade_id"] for r in storage.list_trades(run_id="bt-2")] == ["b2"]
    assert len(storage.list_trades(limit=2)) == 2
    row = storage.list_trades(source="paper")[0]
    assert row["direction"] == "LONG" and row["exit_reason"] == "TAKE_PROFIT" and row["run_id"] is None
    assert Trade.from_dict(row) == make_trade("p2", exit_time=BAR_MS + 2 * HOUR)
    # INSERT OR REPLACE by trade_id
    storage.insert_trade(make_trade("p1", exit_time=BAR_MS + 5 * HOUR))
    assert storage.list_trades(source="paper")[0]["trade_id"] == "p1"
    assert len(storage.list_trades(source="paper")) == 2
    # nullable REAL columns
    t = make_trade("p3")
    storage.insert_trade(Trade.from_dict(t.to_dict() | {"r_multiple": None, "take_profit": None}))
    stored = [r for r in storage.list_trades() if r["trade_id"] == "p3"][0]
    assert stored["r_multiple"] is None and stored["take_profit"] is None


def test_equity_append_and_curve(storage: Storage) -> None:
    for i in range(5):
        storage.append_equity("paper", np.int64(BAR_MS + i * HOUR), np.float64(10_000.0 + i), 10_000.0)
    storage.append_equity("paper", BAR_MS + 4 * HOUR, 20_000.0, 19_000.0)  # replace
    storage.append_equity("testnet", BAR_MS, 5.0, 5.0)
    curve = storage.equity_curve("paper")
    assert [p["time"] for p in curve] == [BAR_MS + i * HOUR for i in range(5)]
    assert curve[-1] == {"time": BAR_MS + 4 * HOUR, "equity": 20_000.0, "wallet": 19_000.0}
    last2 = storage.equity_curve("paper", limit=2)
    assert [p["time"] for p in last2] == [BAR_MS + 3 * HOUR, BAR_MS + 4 * HOUR]  # last points, ascending
    assert storage.equity_curve("live") == []


def test_candles_upsert_get(storage: Storage, candle_factory: Callable[..., pd.DataFrame]) -> None:
    df = candle_factory([100.0 + i for i in range(10)], start_ms=BAR_MS)
    assert storage.upsert_candles("BTCUSDT", "1h", df) == 10
    got = storage.get_candles("BTCUSDT", "1h")
    assert len(got) == 10
    assert list(got[0]) == ["open_time", "open", "high", "low", "close", "volume"]
    assert [c["open_time"] for c in got] == [BAR_MS + i * HOUR for i in range(10)]
    assert got[3]["close"] == 103.0 and type(got[3]["open_time"]) is int
    last3 = storage.get_candles("BTCUSDT", "1h", limit=3)
    assert [c["open_time"] for c in last3] == [BAR_MS + i * HOUR for i in (7, 8, 9)]
    # overlapping upsert replaces
    df2 = candle_factory([500.0, 501.0], start_ms=BAR_MS + 9 * HOUR)
    assert storage.upsert_candles("BTCUSDT", "1h", df2) == 2
    got = storage.get_candles("BTCUSDT", "1h", limit=100)
    assert len(got) == 11 and got[-2]["close"] == 500.0
    assert storage.get_candles("BTCUSDT", "4h") == []
    assert storage.upsert_candles("BTCUSDT", "1h", df.iloc[0:0]) == 0
    with pytest.raises(DataError):
        storage.upsert_candles("BTCUSDT", "1h", df.drop(columns=["volume"]))


def test_save_and_get_backtest(storage: Storage) -> None:
    older = make_backtest("bt-older", created_at=1_000)
    newer = make_backtest("bt-newer", created_at=2_000, n=3)
    storage.save_backtest(older)
    storage.save_backtest(newer)
    runs = storage.list_backtests()
    assert [r["run_id"] for r in runs] == ["bt-newer", "bt-older"]
    assert list(runs[0]) == [
        "run_id",
        "created_at",
        "symbol",
        "interval",
        "strategy",
        "params",
        "start_time",
        "end_time",
        "initial_balance",
        "metrics",
        "result_dir",
    ]
    assert runs[1]["params"] == {"fast_period": 20, "slow_period": 50}
    assert runs[1]["metrics"] == {"total_return": 0.04, "sharpe": 1.5, "profit_factor": None, "n_trades": 1, "bad": None}
    detail = storage.get_backtest("bt-older")
    assert detail is not None
    assert detail["config"] == {"mode": "paper", "funding_coverage": {"included": True, "rows": 3}}
    assert detail["start_time"] == BAR_MS and detail["end_time"] == BAR_MS + 5 * HOUR - 1
    assert storage.get_backtest("nope") is None
    eq = storage.backtest_equity("bt-older")
    assert [p["time"] for p in eq] == [BAR_MS + i * HOUR for i in range(5)]
    assert eq[0] == {"time": BAR_MS, "equity": 10_000.0} and eq[-1]["equity"] == 10_400.0
    trades = storage.list_trades(source="backtest", run_id="bt-older")
    assert [t["trade_id"] for t in trades] == ["bt-older-00000"]
    assert trades[0]["source"] == "backtest" and trades[0]["run_id"] == "bt-older"
    assert len(storage.list_backtests(limit=1)) == 1

    # saving the same run again replaces its rows (no duplicates, no stale equity points)
    older.result_dir = "C:/results/bt-older"
    older.equity = older.equity.iloc[:2]
    storage.save_backtest(older)
    assert len(storage.backtest_equity("bt-older")) == 2
    assert len(storage.list_trades(source="backtest", run_id="bt-older")) == 1
    assert storage.get_backtest("bt-older")["result_dir"] == "C:/results/bt-older"  # type: ignore[index]
    assert len(storage.list_backtests()) == 2


def test_read_only_storage_cannot_write(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    with Storage(db) as writer:
        writer.set_state("k", 1)
        writer.log_event("INFO", "paper", "STARTED", "hello")
    with Storage(db, read_only=True) as reader:
        assert reader.read_only
        assert reader.get_state("k") == 1
        assert reader.recent_events()[0]["kind"] == "STARTED"
        with pytest.raises(sqlite3.OperationalError):
            reader.set_state("k", 2)
        with pytest.raises(sqlite3.OperationalError):
            reader.log_event("INFO", "paper", "X", "y")
        with pytest.raises(DataError):
            reader.init_schema()
        assert reader.get_state("k") == 1  # rolled back, still usable


def test_read_only_does_not_create_db(tmp_path: Path) -> None:
    folder = tmp_path / "data"
    db = folder / "bot.db"
    with pytest.raises(DataError, match="database not found"):
        Storage(db, read_only=True)
    assert not db.exists()
    assert not folder.exists()
    # an existing file that is not a bot database is rejected without modification
    other = tmp_path / "other.db"
    with raw_connect(other) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
    before = other.read_bytes()
    with pytest.raises(DataError, match="schema_version"):
        Storage(other, read_only=True)
    assert other.read_bytes() == before


def test_read_only_runs_no_ddl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "bot.db"
    with Storage(db) as writer:
        writer.upsert_status(make_status())
        writer.insert_trade(make_trade("t1"))
        writer.save_backtest(make_backtest("bt-1", created_at=1))
    statements: list[str] = []
    real_connect = sqlite3.connect

    def tracing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracing_connect)
    with Storage(db, read_only=True) as reader:
        reader.get_status()
        reader.list_trades()
        reader.equity_curve("paper")
        reader.get_candles("BTCUSDT", "1h")
        reader.list_backtests()
        reader.get_backtest("bt-1")
        reader.backtest_equity("bt-1")
        reader.recent_events()
        reader.recent_signals("paper")
        reader.recent_orders("paper")
        reader.get_state("x")
    assert statements, "trace callback saw nothing"
    assert statements[0].upper().startswith("PRAGMA QUERY_ONLY")
    forbidden = re.compile(r"\b(CREATE|INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|BEGIN|JOURNAL_MODE)\b", re.IGNORECASE)
    for stmt in statements:
        assert not forbidden.search(stmt), stmt


def test_reader_sees_writer_commits(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    with Storage(db) as writer, Storage(db, read_only=True) as reader:
        assert reader.get_status() is None
        writer.upsert_status(make_status())
        writer.set_state("k", {"a": 1})
        writer.log_event("warning", "paper", "STALE_DATA", "no fresh candle", ts_ms=5)
        writer.log_event("CRITICAL", "testnet", "KILL_SWITCH", "daily loss", ts_ms=6)
        assert reader.get_status()["pid"] == 4242  # type: ignore[index]
        assert reader.get_state("k") == {"a": 1}
        events = reader.recent_events()
        assert events == [
            {"ts": 6, "level": "CRITICAL", "mode": "testnet", "kind": "KILL_SWITCH", "message": "daily loss"},
            {"ts": 5, "level": "WARNING", "mode": "paper", "kind": "STALE_DATA", "message": "no fresh candle"},
        ]
        assert [e["kind"] for e in reader.recent_events(mode="paper")] == ["STALE_DATA"]
        assert len(reader.recent_events(limit=1)) == 1
        writer.touch_heartbeat(1_790_770_099_000)
        assert reader.get_status()["updated_at"] == 1_790_770_099_000  # type: ignore[index]


def test_close_is_idempotent_and_no_resource_warning(tmp_path: Path) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        st = Storage(tmp_path / "bot.db")
        st.set_state("a", 1)
        st.close()
        st.close()
        assert st.closed
        with pytest.raises(DataError, match="closed"):
            st.get_state("a")
        with Storage(tmp_path / "bot.db", read_only=True) as reader:
            assert reader.get_state("a") == 1
        reader.close()
        del st, reader
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


# ---------------------------------------------------------------------------------------------
# Extra cases
# ---------------------------------------------------------------------------------------------


def test_upsert_order_keeps_created_at(storage: Storage) -> None:
    base = dict(
        client_id="mab1-4314-EN-1790769600-0",
        exchange_id=None,
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        purpose=OrderPurpose.ENTRY,
        status=OrderStatus.NEW,
        requested_qty=0.1,
        executed_qty=0.0,
        avg_price=None,
        trigger_price=None,
        fee=0.0,
        ts=BAR_MS,
        raw={"orderId": 1},
    )
    storage.upsert_order("testnet", OrderResult(**base), 1_000)  # type: ignore[arg-type]
    storage.upsert_order(
        "testnet",
        OrderResult(**(base | {"exchange_id": "42", "status": OrderStatus.FILLED, "executed_qty": 0.1, "avg_price": 50_000.0, "fee": 2.5})),  # type: ignore[arg-type]
        2_000,
    )
    storage.upsert_order("paper", OrderResult(**base), 3_000)  # type: ignore[arg-type]
    rows = storage.recent_orders("testnet")
    assert len(rows) == 1
    row = rows[0]
    assert row["created_at"] == 1_000 and row["updated_at"] == 2_000
    assert row["status"] == "FILLED" and row["exchange_id"] == "42" and row["executed_qty"] == 0.1
    assert row["avg_price"] == 50_000.0 and row["fee"] == 2.5 and row["raw"] == {"orderId": 1}
    assert row["purpose"] == "ENTRY" and row["side"] == "BUY" and row["order_type"] == "MARKET"
    assert len(storage.recent_orders("paper")) == 1


def test_writer_creates_parent_directory(tmp_path: Path) -> None:
    db = tmp_path / "a" / "b" / "bot.db"
    with Storage(db) as st:
        st.log_event("INFO", "paper", "X", "y")
        assert "Storage(" in repr(st)
    assert db.exists()


def test_transaction_rolls_back_on_error(storage: Storage) -> None:
    good = make_trade("ok")
    bad = Trade.from_dict(make_trade("bad").to_dict() | {"gross_pnl": float("nan")})  # NOT NULL -> fails
    with pytest.raises(sqlite3.IntegrityError):
        storage.insert_trades([good, bad])
    assert storage.list_trades() == []  # whole batch rolled back
    storage.insert_trades([good])
    assert len(storage.list_trades()) == 1
