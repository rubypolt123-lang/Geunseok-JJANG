"""Backtest result files and the Korean metrics table (SPEC §11.3).

``METRIC_LABELS_KO`` / ``PERCENT_METRICS`` live in ``bot.models`` (single source) and are re-exported here.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pandas as pd

from bot.fsutil import atomic_write_text
from bot.models import (  # re-exported (single source in models, §4.1)
    METRIC_LABELS_KO,
    PERCENT_METRICS,
    TRADE_COLUMNS,
    BacktestResult,
    Trade,
    to_jsonable,
)
from bot.timeutil import ms_to_iso

if TYPE_CHECKING:
    from bot.storage import Storage

logger = logging.getLogger(__name__)

__all__ = [
    "EQUITY_CSV_COLUMNS",
    "METRIC_LABELS_KO",
    "PERCENT_METRICS",
    "RESULT_JSON_KEYS",
    "format_metrics_table",
    "save_backtest_result",
]

EQUITY_CSV_COLUMNS: Final[tuple[str, ...]] = ("time", "time_iso", "equity", "in_position", "position_qty")
RESULT_JSON_KEYS: Final[tuple[str, ...]] = (
    "run_id",
    "created_at",
    "symbol",
    "interval",
    "strategy",
    "params",
    "config",
    "start_time",
    "end_time",
    "initial_balance",
    "metrics",
)
# run_id becomes a directory name: never allow separators or "..".
_RUN_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _csv_value(value: Any) -> str:
    """Plain CSV cell: None/NaN -> "", bool -> true/false, float -> shortest round-trip repr."""
    v = to_jsonable(value)
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def _csv_text(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)  # RFC 4180 "\r\n" line endings (the file is written with newline="")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_csv_value(v) for v in row])
    return buf.getvalue()


def _trades_csv(trades: Sequence[Trade]) -> str:
    rows = []
    for trade in trades:
        d = trade.to_dict()
        rows.append([d[c] for c in TRADE_COLUMNS])
    return _csv_text(TRADE_COLUMNS, rows)


def _equity_csv(equity: pd.DataFrame | None) -> str:
    if equity is None or len(equity) == 0:
        return _csv_text(EQUITY_CSV_COLUMNS, [])
    missing = [c for c in ("time", "equity") if c not in equity.columns]
    if missing:
        raise ValueError(f"equity frame is missing columns: {', '.join(missing)}")
    n = len(equity)
    times = [int(t) for t in equity["time"].tolist()]
    values = [float(e) for e in equity["equity"].tolist()]
    in_pos = (
        [bool(x) for x in equity["in_position"].tolist()] if "in_position" in equity.columns else [None] * n
    )
    qty = [float(q) for q in equity["position_qty"].tolist()] if "position_qty" in equity.columns else [None] * n
    rows = [[t, ms_to_iso(t), e, p, q] for t, e, p, q in zip(times, values, in_pos, qty)]
    return _csv_text(EQUITY_CSV_COLUMNS, rows)


def _result_payload(result: BacktestResult) -> dict[str, Any]:
    return {key: getattr(result, key) for key in RESULT_JSON_KEYS}


def save_backtest_result(result: BacktestResult, results_dir: Path, storage: Storage | None = None) -> Path:
    """Write ``result.json``, ``trades.csv`` and ``equity.csv`` under ``results_dir / run_id`` (utf-8, atomic).

    Sets ``result.result_dir`` and, if ``storage`` is given, saves the run (runs/trades/equity rows) in one
    transaction. Returns the result directory.
    """
    run_id = str(result.run_id)
    if not _RUN_ID_RE.fullmatch(run_id) or ".." in run_id:
        raise ValueError(f"invalid run_id for a result directory: {run_id!r}")
    run_dir = Path(results_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    text = json.dumps(to_jsonable(_result_payload(result)), ensure_ascii=False, indent=2, allow_nan=False)
    atomic_write_text(run_dir / "result.json", text + "\n")
    atomic_write_text(run_dir / "trades.csv", _trades_csv(result.trades))
    atomic_write_text(run_dir / "equity.csv", _equity_csv(result.equity))

    result.result_dir = str(run_dir)
    if storage is not None:
        storage.save_backtest(result)
    logger.info("backtest %s saved to %s (%d trades)", run_id, run_dir, len(result.trades))
    return run_dir


def _format_number(key: str, value: Any) -> str:
    if value is None:
        return "-"
    v = to_jsonable(value)  # numpy scalars -> native, NaN/inf -> None
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "예" if v else "아니오"
    if key in PERCENT_METRICS and isinstance(v, (int, float)):
        return f"{float(v) * 100:.2f}%"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return "-"
        return f"{v:.2f}" if abs(v) >= 1 else f"{v:.4f}"
    return str(v)


def format_metrics_table(metrics: Mapping[str, Any]) -> str:
    """One line per ``METRIC_LABELS_KO`` key present (label order): ``"<label>: <value>"``.

    Percent metrics as ``"12.34%"``; floats with 2 decimals (4 when ``|x| < 1``); ints as-is; None -> ``"-"``.
    """
    lines = [
        f"{label}: {_format_number(key, metrics[key])}" for key, label in METRIC_LABELS_KO.items() if key in metrics
    ]
    return "\n".join(lines)
