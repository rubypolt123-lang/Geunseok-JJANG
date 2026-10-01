"""SQLite storage shared by the bot (writer) and the dashboard (reader) (SPEC §5).

Lifecycle: always ``with Storage(...) as st:`` (Python 3.14 emits ``ResourceWarning: unclosed database``).
The reader (``read_only=True``) never creates the file, never runs DDL and never changes the journal mode.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import numpy as np

from bot.errors import DataError
from bot.models import (
    TRADE_COLUMNS,
    Action,
    BacktestResult,
    BotStatus,
    OrderResult,
    Signal,
    Trade,
    to_jsonable,
)
from bot.timeutil import now_ms

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)

# numpy boundary (§0.2): without adapters sqlite3 stores numpy.int64 as an 8-byte BLOB.
sqlite3.register_adapter(np.int64, int)
sqlite3.register_adapter(np.int32, int)
sqlite3.register_adapter(np.float64, float)
sqlite3.register_adapter(np.float32, float)
sqlite3.register_adapter(np.bool_, bool)

SCHEMA_VERSION: Final = 1

SCHEMA_SQL: Final = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,              -- JSON
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_status (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at              INTEGER NOT NULL,   -- heartbeat, ms
    started_at              INTEGER NOT NULL,
    mode                    TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    interval                TEXT NOT NULL,
    strategy                TEXT NOT NULL,
    state                   TEXT NOT NULL,
    message                 TEXT NOT NULL DEFAULT '',
    account_json            TEXT,               -- to_jsonable(AccountSnapshot) incl. position + protective_orders
    last_signal_json        TEXT,               -- to_jsonable(Signal)
    last_bar_open_time      INTEGER,
    entries_blocked_reason  TEXT,
    pid                     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    interval        TEXT NOT NULL,
    bar_open_time   INTEGER NOT NULL,
    action          TEXT NOT NULL,              -- SignalAction
    decided_action  TEXT NOT NULL,              -- Action
    price           REAL NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    meta_json       TEXT,
    created_at      INTEGER NOT NULL,
    UNIQUE (mode, symbol, interval, bar_open_time)
);
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    client_id       TEXT NOT NULL,
    exchange_id     TEXT,
    purpose         TEXT NOT NULL,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    status          TEXT NOT NULL,
    requested_qty   REAL,
    executed_qty    REAL NOT NULL DEFAULT 0,
    avg_price       REAL,
    trigger_price   REAL,
    fee             REAL NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    raw_json        TEXT,
    UNIQUE (mode, client_id)
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT PRIMARY KEY,
    source          TEXT NOT NULL,              -- backtest | paper | testnet | live
    run_id          TEXT,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    qty             REAL NOT NULL,
    entry_time      INTEGER NOT NULL,
    entry_price     REAL NOT NULL,
    exit_time       INTEGER NOT NULL,
    exit_price      REAL NOT NULL,
    exit_reason     TEXT NOT NULL,
    gross_pnl       REAL NOT NULL,
    fees            REAL NOT NULL,
    funding         REAL NOT NULL,
    net_pnl         REAL NOT NULL,
    r_multiple      REAL,
    initial_stop    REAL,
    take_profit     REAL,
    leverage        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_source_exit ON trades (source, run_id, exit_time);
CREATE TABLE IF NOT EXISTS equity (
    mode        TEXT NOT NULL,
    time        INTEGER NOT NULL,               -- open_time of the last closed bar at the iteration
    equity      REAL NOT NULL,
    wallet      REAL NOT NULL,
    PRIMARY KEY (mode, time)
);
CREATE TABLE IF NOT EXISTS candles (
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    open_time   INTEGER NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL,
    PRIMARY KEY (symbol, interval, open_time)
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id          TEXT PRIMARY KEY,
    created_at      INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    interval        TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    params_json     TEXT NOT NULL,
    config_json     TEXT NOT NULL,
    start_time      INTEGER NOT NULL,
    end_time        INTEGER NOT NULL,
    initial_balance REAL NOT NULL,
    metrics_json    TEXT NOT NULL,
    result_dir      TEXT
);
CREATE TABLE IF NOT EXISTS backtest_equity (
    run_id      TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    time        INTEGER NOT NULL,
    equity      REAL NOT NULL,
    PRIMARY KEY (run_id, time)
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    level       TEXT NOT NULL,                  -- INFO | WARNING | ERROR | CRITICAL
    mode        TEXT NOT NULL,
    kind        TEXT NOT NULL,                  -- e.g. KILL_SWITCH, PROTECTION_FAILED, STALE_DATA, ADOPTED_POSITION
    message     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
"""

_CONNECT_TIMEOUT_SEC: Final = 10.0
_CANDLE_COLUMNS: Final = ("open_time", "open", "high", "low", "close", "volume")
_STATUS_KEYS: Final = (
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
)
_BACKTEST_LIST_COLUMNS: Final = (
    "run_id, created_at, symbol, interval, strategy, params_json, start_time, end_time, "
    "initial_balance, metrics_json, result_dir"
)
_TRADE_INSERT_SQL: Final = (
    f"INSERT OR REPLACE INTO trades ({', '.join(TRADE_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in TRADE_COLUMNS)})"
)


# ---------------------------------------------------------------------------------------------
# Value helpers (explicit int()/float() at the bind boundary)
# ---------------------------------------------------------------------------------------------


def _text(value: Any) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def _opt_text(value: Any) -> str | None:
    return None if value is None else _text(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _opt_real(value: Any) -> float | None:
    """Nullable REAL: None and NaN -> NULL."""
    if value is None:
        return None
    f = float(value)
    return None if math.isnan(f) else f


def _dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _opt_dumps(value: Any) -> str | None:
    return None if value is None else _dumps(value)


def _loads(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise DataError(f"corrupt JSON in database: {exc}") from None


def _limit(limit: int) -> int:
    return max(0, int(limit))


def _trade_row(trade: Trade, *, source: str | None = None, run_id: Any = ...) -> tuple[Any, ...]:
    """Row for TRADE_COLUMNS with explicit type coercion (optionally overriding source/run_id)."""
    return (
        str(trade.trade_id),
        _text(trade.source if source is None else source),
        _opt_text(trade.run_id if run_id is ... else run_id),
        str(trade.symbol),
        _text(trade.direction),
        float(trade.qty),
        int(trade.entry_time),
        float(trade.entry_price),
        int(trade.exit_time),
        float(trade.exit_price),
        _text(trade.exit_reason),
        float(trade.gross_pnl),
        float(trade.fees),
        float(trade.funding),
        float(trade.net_pnl),
        _opt_real(trade.r_multiple),
        _opt_real(trade.initial_stop),
        _opt_real(trade.take_profit),
        int(trade.leverage),
    )


class Storage:
    """One SQLite connection (writer or query-only reader) with explicit write transactions."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
        self.path: Path = Path(db_path)
        self.read_only: bool = bool(read_only)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        if self.read_only:
            self._open_reader()
        else:
            self._open_writer()

    # ----------------------------------------------------------------------------- lifecycle

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            self.path,
            timeout=_CONNECT_TIMEOUT_SEC,
            isolation_level=None,
            check_same_thread=False,
        )

    def _open_writer(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            mode = str(row[0]).lower() if row else ""
            if mode != "wal":
                raise DataError(f"could not enable WAL journal mode on {self.path} (got {mode!r})")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._conn = conn
            self.init_schema()
        except BaseException:
            self._conn = None
            conn.close()
            raise

    def _open_reader(self) -> None:
        if not self.path.exists():
            raise DataError(f"database not found: {self.path}")
        conn = self._connect()
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
            ).fetchone()
            if has_table is None:
                raise DataError(f"database {self.path} has no schema_version table (not a bot database?)")
            version = self._read_version(conn)
            if version != SCHEMA_VERSION:
                raise DataError(
                    f"database {self.path} has schema version {version}, expected {SCHEMA_VERSION}"
                )
            self._conn = conn
        except BaseException:
            self._conn = None
            conn.close()
            raise

    @staticmethod
    def _read_version(conn: sqlite3.Connection) -> int | None:
        versions = {int(r[0]) for r in conn.execute("SELECT version FROM schema_version").fetchall()}
        if not versions:
            return None
        if len(versions) > 1:
            raise DataError(f"schema_version table has multiple versions: {sorted(versions)}")
        return versions.pop()

    def init_schema(self) -> None:
        """Create tables (idempotent) and record ``SCHEMA_VERSION``. Writer only."""
        if self.read_only:
            raise DataError("cannot initialise the schema on a read-only storage")
        conn = self._require_conn()
        with self._lock:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
            ).fetchone()
            if has_table is not None:
                version = self._read_version(conn)
                if version is not None and version != SCHEMA_VERSION:
                    raise DataError(
                        f"database {self.path} has schema version {version}, expected {SCHEMA_VERSION}"
                    )
            conn.executescript(SCHEMA_SQL)
            with self._tx() as tx:
                version = self._read_version(tx)
                if version is None:
                    tx.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
                elif version != SCHEMA_VERSION:
                    raise DataError(
                        f"database {self.path} has schema version {version}, expected {SCHEMA_VERSION}"
                    )

    def close(self) -> None:
        """Close the connection (idempotent)."""
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                conn.close()

    @property
    def closed(self) -> bool:
        return self._conn is None

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Storage(path={str(self.path)!r}, read_only={self.read_only})"

    def _require_conn(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is None:
            raise DataError(f"storage is closed: {self.path}")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE ... COMMIT``; ``ROLLBACK`` on any exception."""
        conn = self._require_conn()
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    logger.exception("rollback failed on %s", self.path)
                raise
            else:
                conn.execute("COMMIT")

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        conn = self._require_conn()
        with self._lock:
            return conn.execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        conn = self._require_conn()
        with self._lock:
            return conn.execute(sql, params).fetchone()

    # ----------------------------------------------------------------------------- key/value state

    def get_state(self, key: str) -> Any | None:
        row = self._fetchone("SELECT value FROM kv_state WHERE key = ?", (str(key),))
        return None if row is None else _loads(row["value"])

    def set_state(self, key: str, value: Any) -> None:
        """Store ``value`` as JSON (through ``to_jsonable``)."""
        payload = _dumps(value)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO kv_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (str(key), payload, now_ms()),
            )

    def delete_state(self, key: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM kv_state WHERE key = ?", (str(key),))

    # ----------------------------------------------------------------------------- status / heartbeat

    def upsert_status(self, status: BotStatus) -> None:
        """Replace the single status row (id = 1). ``updated_at``/``started_at`` are LOCAL ms."""
        row = (
            int(status.updated_at),
            int(status.started_at),
            _text(status.mode),
            str(status.symbol),
            str(status.interval),
            str(status.strategy),
            _text(status.state),
            "" if status.message is None else str(status.message),
            _opt_dumps(status.account),
            _opt_dumps(status.last_signal),
            _opt_int(status.last_bar_open_time),
            _opt_text(status.entries_blocked_reason),
            int(status.pid),
        )
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_status (id, updated_at, started_at, mode, symbol, interval, strategy, "
                "state, message, account_json, last_signal_json, last_bar_open_time, entries_blocked_reason, pid) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )

    def touch_heartbeat(self, ts_ms: int) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE bot_status SET updated_at = ? WHERE id = 1", (int(ts_ms),))

    def get_status(self) -> dict[str, Any] | None:
        row = self._fetchone("SELECT * FROM bot_status WHERE id = 1")
        if row is None:
            return None
        data = {
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "mode": row["mode"],
            "symbol": row["symbol"],
            "interval": row["interval"],
            "strategy": row["strategy"],
            "state": row["state"],
            "message": row["message"],
            "account": _loads(row["account_json"]),
            "last_signal": _loads(row["last_signal_json"]),
            "last_bar_open_time": row["last_bar_open_time"],
            "entries_blocked_reason": row["entries_blocked_reason"],
            "pid": row["pid"],
        }
        return {k: data[k] for k in _STATUS_KEYS}

    # ----------------------------------------------------------------------------- signals

    def record_signal(
        self,
        mode: str,
        symbol: str,
        interval: str,
        signal: Signal,
        decided_action: Action,
        ts_ms: int,
    ) -> None:
        """INSERT OR IGNORE on UNIQUE (mode, symbol, interval, bar_open_time)."""
        row = (
            _text(mode),
            str(symbol),
            str(interval),
            int(signal.bar_open_time),
            _text(signal.action),
            _text(decided_action),
            float(signal.price),
            "" if signal.reason is None else str(signal.reason),
            _dumps(signal.meta),
            int(ts_ms),
        )
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO signals (mode, symbol, interval, bar_open_time, action, decided_action, "
                "price, reason, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )

    def recent_signals(self, mode: str, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first. Keys: id, mode, symbol, interval, bar_open_time, action, decided_action, price, reason,
        meta (dict), created_at."""
        rows = self._fetchall(
            "SELECT id, mode, symbol, interval, bar_open_time, action, decided_action, price, reason, meta_json, "
            "created_at FROM signals WHERE mode = ? ORDER BY bar_open_time DESC, id DESC LIMIT ?",
            (_text(mode), _limit(limit)),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            meta = _loads(d.pop("meta_json"))
            d["meta"] = meta if meta is not None else {}
            out.append(d)
        return out

    # ----------------------------------------------------------------------------- orders

    def upsert_order(self, mode: str, result: OrderResult, ts_ms: int) -> None:
        """Upsert on (mode, client_id); ``created_at`` of an existing row is kept."""
        ts = int(ts_ms)
        row = (
            _text(mode),
            str(result.symbol),
            str(result.client_id),
            _opt_text(result.exchange_id),
            _text(result.purpose),
            _text(result.side),
            _text(result.order_type),
            _text(result.status),
            _opt_real(result.requested_qty),
            float(result.executed_qty),
            _opt_real(result.avg_price),
            _opt_real(result.trigger_price),
            float(result.fee),
            ts,
            ts,
            _dumps(dict(result.raw)) if result.raw else None,
        )
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO orders (mode, symbol, client_id, exchange_id, purpose, side, order_type, status, "
                "requested_qty, executed_qty, avg_price, trigger_price, fee, created_at, updated_at, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(mode, client_id) DO UPDATE SET "
                "symbol = excluded.symbol, "
                "exchange_id = COALESCE(excluded.exchange_id, orders.exchange_id), "
                "purpose = excluded.purpose, side = excluded.side, order_type = excluded.order_type, "
                "status = excluded.status, requested_qty = excluded.requested_qty, "
                "executed_qty = excluded.executed_qty, avg_price = excluded.avg_price, "
                "trigger_price = excluded.trigger_price, fee = excluded.fee, updated_at = excluded.updated_at, "
                "raw_json = COALESCE(excluded.raw_json, orders.raw_json)",
                row,
            )

    def recent_orders(self, mode: str, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first (by updated_at). Keys: table columns with ``raw_json`` replaced by ``raw`` (dict)."""
        rows = self._fetchall(
            "SELECT id, mode, symbol, client_id, exchange_id, purpose, side, order_type, status, requested_qty, "
            "executed_qty, avg_price, trigger_price, fee, created_at, updated_at, raw_json FROM orders "
            "WHERE mode = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
            (_text(mode), _limit(limit)),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            raw = _loads(d.pop("raw_json"))
            d["raw"] = raw if raw is not None else {}
            out.append(d)
        return out

    # ----------------------------------------------------------------------------- trades

    def insert_trade(self, trade: Trade) -> None:
        """INSERT OR REPLACE by trade_id."""
        row = _trade_row(trade)
        with self._tx() as conn:
            conn.execute(_TRADE_INSERT_SQL, row)

    def insert_trades(self, trades: Iterable[Trade]) -> None:
        """Insert many trades in one transaction."""
        rows = [_trade_row(t) for t in trades]
        if not rows:
            return
        with self._tx() as conn:
            conn.executemany(_TRADE_INSERT_SQL, rows)

    def list_trades(
        self,
        *,
        source: str | None = None,
        run_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Newest exit_time first; dict keys == TRADE_COLUMNS. ``None`` filters are not applied."""
        clauses: list[str] = []
        params: list[Any] = []
        if source is not None:
            clauses.append("source = ?")
            params.append(_text(source))
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(str(run_id))
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = self._fetchall(
            f"SELECT {', '.join(TRADE_COLUMNS)} FROM trades {where}"
            "ORDER BY exit_time DESC, entry_time DESC, trade_id DESC LIMIT ?",
            (*params, _limit(limit)),
        )
        return [{c: r[c] for c in TRADE_COLUMNS} for r in rows]

    # ----------------------------------------------------------------------------- live/paper equity

    def append_equity(self, mode: str, time_ms: int, equity: float, wallet: float) -> None:
        """INSERT OR REPLACE on (mode, time)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO equity (mode, time, equity, wallet) VALUES (?, ?, ?, ?)",
                (_text(mode), int(time_ms), float(equity), float(wallet)),
            )

    def equity_curve(self, mode: str, limit: int = 5000) -> list[dict[str, Any]]:
        """The last ``limit`` points, ascending by time. Keys: time, equity, wallet."""
        rows = self._fetchall(
            "SELECT time, equity, wallet FROM (SELECT time, equity, wallet FROM equity WHERE mode = ? "
            "ORDER BY time DESC LIMIT ?) ORDER BY time ASC",
            (_text(mode), _limit(limit)),
        )
        return [{"time": r["time"], "equity": r["equity"], "wallet": r["wallet"]} for r in rows]

    # ----------------------------------------------------------------------------- candles

    def upsert_candles(self, symbol: str, interval: str, df: pd.DataFrame) -> int:
        """INSERT OR REPLACE candles (dashboard chart). Returns the number of rows written."""
        if df is None or len(df) == 0:
            return 0
        missing = [c for c in _CANDLE_COLUMNS if c not in df.columns]
        if missing:
            raise DataError(f"candle frame is missing columns: {', '.join(missing)}")
        sym, itv = str(symbol), str(interval)
        rows = [
            (sym, itv, int(ot), float(o), float(h), float(lo), float(c), float(v))
            for ot, o, h, lo, c, v in df.loc[:, list(_CANDLE_COLUMNS)].itertuples(index=False, name=None)
        ]
        with self._tx() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO candles (symbol, interval, open_time, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def get_candles(self, symbol: str, interval: str, limit: int = 500) -> list[dict[str, Any]]:
        """The last ``limit`` candles, ascending. Keys: open_time, open, high, low, close, volume."""
        rows = self._fetchall(
            "SELECT open_time, open, high, low, close, volume FROM (SELECT open_time, open, high, low, close, "
            "volume FROM candles WHERE symbol = ? AND interval = ? ORDER BY open_time DESC LIMIT ?) "
            "ORDER BY open_time ASC",
            (str(symbol), str(interval), _limit(limit)),
        )
        return [{c: r[c] for c in _CANDLE_COLUMNS} for r in rows]

    # ----------------------------------------------------------------------------- backtests

    def save_backtest(self, result: BacktestResult) -> None:
        """One transaction: backtest_runs row, trades (source="backtest", run_id), backtest_equity (time, equity).

        Saving the same run_id again replaces the previous rows of that run.
        """
        run_id = str(result.run_id)
        run_row = (
            run_id,
            int(result.created_at),
            str(result.symbol),
            str(result.interval),
            str(result.strategy),
            _dumps(result.params),
            _dumps(result.config),
            int(result.start_time),
            int(result.end_time),
            float(result.initial_balance),
            _dumps(result.metrics),
            _opt_text(result.result_dir),
        )
        trade_rows = [_trade_row(t, source="backtest", run_id=run_id) for t in result.trades]
        equity_rows: list[tuple[str, int, float]] = []
        equity = result.equity
        if equity is not None and len(equity) > 0:
            if "time" not in equity.columns or "equity" not in equity.columns:
                raise DataError("backtest equity frame needs 'time' and 'equity' columns")
            equity_rows = [
                (run_id, int(t), float(e))
                for t, e in equity.loc[:, ["time", "equity"]].itertuples(index=False, name=None)
            ]
        with self._tx() as conn:
            conn.execute("DELETE FROM backtest_equity WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM trades WHERE source = 'backtest' AND run_id = ?", (run_id,))
            conn.execute("DELETE FROM backtest_runs WHERE run_id = ?", (run_id,))
            conn.execute(
                "INSERT INTO backtest_runs (run_id, created_at, symbol, interval, strategy, params_json, "
                "config_json, start_time, end_time, initial_balance, metrics_json, result_dir) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                run_row,
            )
            if trade_rows:
                conn.executemany(_TRADE_INSERT_SQL, trade_rows)
            if equity_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO backtest_equity (run_id, time, equity) VALUES (?, ?, ?)",
                    equity_rows,
                )

    @staticmethod
    def _backtest_dict(row: sqlite3.Row) -> dict[str, Any]:
        params = _loads(row["params_json"])
        metrics = _loads(row["metrics_json"])
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "symbol": row["symbol"],
            "interval": row["interval"],
            "strategy": row["strategy"],
            "params": params if params is not None else {},
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "initial_balance": row["initial_balance"],
            "metrics": metrics if metrics is not None else {},
            "result_dir": row["result_dir"],
        }

    def list_backtests(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first. Keys: run_id, created_at, symbol, interval, strategy, params (dict), start_time,
        end_time, initial_balance, metrics (dict), result_dir."""
        rows = self._fetchall(
            f"SELECT {_BACKTEST_LIST_COLUMNS} FROM backtest_runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
            (_limit(limit),),
        )
        return [self._backtest_dict(r) for r in rows]

    def get_backtest(self, run_id: str) -> dict[str, Any] | None:
        """``list_backtests`` keys + ``config`` (dict), or None."""
        row = self._fetchone(
            f"SELECT {_BACKTEST_LIST_COLUMNS}, config_json FROM backtest_runs WHERE run_id = ?",
            (str(run_id),),
        )
        if row is None:
            return None
        out = self._backtest_dict(row)
        config = _loads(row["config_json"])
        out["config"] = config if config is not None else {}
        return out

    def backtest_equity(self, run_id: str) -> list[dict[str, Any]]:
        """Ascending. Keys: time, equity."""
        rows = self._fetchall(
            "SELECT time, equity FROM backtest_equity WHERE run_id = ? ORDER BY time ASC",
            (str(run_id),),
        )
        return [{"time": r["time"], "equity": r["equity"]} for r in rows]

    # ----------------------------------------------------------------------------- events

    def log_event(self, level: str, mode: str, kind: str, message: str, ts_ms: int | None = None) -> None:
        """Append an event (``ts_ms`` default = LOCAL now)."""
        ts = now_ms() if ts_ms is None else int(ts_ms)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO events (ts, level, mode, kind, message) VALUES (?, ?, ?, ?, ?)",
                (ts, _text(level).upper(), _text(mode), _text(kind), str(message)),
            )

    def recent_events(self, limit: int = 50, mode: str | None = None) -> list[dict[str, Any]]:
        """Newest first. Keys: ts, level, mode, kind, message."""
        if mode is None:
            rows = self._fetchall(
                "SELECT ts, level, mode, kind, message FROM events ORDER BY ts DESC, id DESC LIMIT ?",
                (_limit(limit),),
            )
        else:
            rows = self._fetchall(
                "SELECT ts, level, mode, kind, message FROM events WHERE mode = ? ORDER BY ts DESC, id DESC LIMIT ?",
                (_text(mode), _limit(limit)),
            )
        return [
            {"ts": r["ts"], "level": r["level"], "mode": r["mode"], "kind": r["kind"], "message": r["message"]}
            for r in rows
        ]
