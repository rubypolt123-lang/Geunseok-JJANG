"""U4 — bot/backtest/report.py (SPEC §11.3, §14.2)."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bot import models
from bot.backtest import report
from bot.backtest.metrics import compute_metrics
from bot.backtest.report import METRIC_LABELS_KO, PERCENT_METRICS, format_metrics_table, save_backtest_result
from bot.models import TRADE_COLUMNS, BacktestResult, Direction, ExitReason, Trade
from bot.storage import Storage

T0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z
HOUR = 3_600_000
RUN_ID = "bt-20240101-000000-abc123"


def _trades() -> list[Trade]:
    return [
        Trade(
            trade_id=f"{RUN_ID}-00001", source="backtest", run_id=RUN_ID, symbol="BTCUSDT", direction=Direction.LONG,
            qty=0.09, entry_time=T0 + HOUR, entry_price=50_025.0, exit_time=T0 + 3 * HOUR - 1, exit_price=48_975.5,
            exit_reason=ExitReason.STOP_LOSS, gross_pnl=-94.455, fees=4.455, funding=0.0, net_pnl=-98.91,
            r_multiple=-1.0, initial_stop=49_000.0, take_profit=52_000.0, leverage=3,
        ),
        Trade(
            trade_id=f"{RUN_ID}-00002", source="backtest", run_id=RUN_ID, symbol="BTCUSDT", direction=Direction.SHORT,
            qty=0.1, entry_time=T0 + 4 * HOUR, entry_price=48_975.5, exit_time=T0 + 6 * HOUR, exit_price=48_000.0,
            exit_reason=ExitReason.SIGNAL, gross_pnl=97.55, fees=4.85, funding=-0.3, net_pnl=93.0,
            r_multiple=None, initial_stop=49_955.0, take_profit=None, leverage=3,
        ),
    ]


def _result(*, metrics: dict | None = None, params: dict | None = None) -> BacktestResult:
    n = 8
    equity = pd.DataFrame(
        {
            "time": np.asarray([T0 + i * HOUR for i in range(n)], dtype=np.int64),
            "equity": np.asarray([10_000.0, 9_998.0, 9_950.0, 9_901.0, 9_899.0, 9_950.0, 9_994.0, 9_994.09]),
            "in_position": np.asarray([False, True, True, False, True, True, True, False]),
            "position_qty": np.asarray([0.0, 0.09, 0.09, 0.0, -0.1, -0.1, 0.0, 0.0]),
        }
    )
    trades = _trades()
    m = metrics if metrics is not None else compute_metrics(equity, trades, initial_balance=10_000.0, interval="1h")
    return BacktestResult(
        run_id=RUN_ID, created_at=1_790_769_600_000, symbol="BTCUSDT", interval="1h", strategy="ma_cross",
        params=params if params is not None else {"fast_period": 20, "slow_period": 50, "ma_type": "EMA"},
        config={"mode": "paper", "risk": {"leverage": 3}, "funding_coverage": {"included": True, "rows": 3}},
        start_time=T0, end_time=T0 + n * HOUR - 1, initial_balance=10_000.0, metrics=m, equity=equity,
        trades=trades,
    )


def _read_csv(path: Path) -> list[list[str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh))


def test_save_writes_json_csv_and_db(tmp_path: Path, storage: Storage) -> None:
    result = _result()
    out = save_backtest_result(result, tmp_path / "backtests", storage)
    assert out == tmp_path / "backtests" / RUN_ID
    assert result.result_dir == str(out)
    assert {p.name for p in out.iterdir()} == {"result.json", "trades.csv", "equity.csv"}  # no .tmp left over

    with open(out / "result.json", encoding="utf-8") as fh:
        payload = json.load(fh)
    assert list(payload) == [
        "run_id", "created_at", "symbol", "interval", "strategy", "params", "config", "start_time", "end_time",
        "initial_balance", "metrics",
    ]
    assert payload["run_id"] == RUN_ID
    assert payload["params"]["slow_period"] == 50
    assert payload["config"]["funding_coverage"]["included"] is True
    assert payload["metrics"]["n_trades"] == 2

    trades_rows = _read_csv(out / "trades.csv")
    assert trades_rows[0] == list(TRADE_COLUMNS)
    assert len(trades_rows) == 3
    first = dict(zip(trades_rows[0], trades_rows[1]))
    assert first["direction"] == "LONG"
    assert first["exit_reason"] == "STOP_LOSS"
    assert float(first["entry_price"]) == 50_025.0
    second = dict(zip(trades_rows[0], trades_rows[2]))
    assert second["r_multiple"] == ""  # None -> empty cell
    assert second["take_profit"] == ""

    equity_rows = _read_csv(out / "equity.csv")
    assert equity_rows[0] == ["time", "time_iso", "equity", "in_position", "position_qty"]
    assert len(equity_rows) == 1 + 8
    assert equity_rows[1] == ["1704067200000", "2024-01-01T00:00:00Z", "10000.0", "false", "0.0"]
    assert equity_rows[5][3] == "true" and float(equity_rows[5][4]) == -0.1
    # pandas can read it back with native dtypes
    df = pd.read_csv(out / "equity.csv")
    assert str(df["time"].dtype) == "int64"
    assert df["in_position"].tolist() == result.equity["in_position"].tolist()

    # DB rows (one transaction in Storage.save_backtest)
    run = storage.get_backtest(RUN_ID)
    assert run is not None
    assert run["result_dir"] == str(out)
    assert run["metrics"]["n_trades"] == 2
    assert run["config"]["risk"]["leverage"] == 3
    assert len(storage.list_trades(source="backtest", run_id=RUN_ID)) == 2
    assert [p["time"] for p in storage.backtest_equity(RUN_ID)] == result.equity["time"].tolist()


def test_save_without_storage_and_overwrite(tmp_path: Path) -> None:
    result = _result()
    out1 = save_backtest_result(result, tmp_path)
    out2 = save_backtest_result(result, tmp_path)  # same run id: files are replaced atomically
    assert out1 == out2
    assert len(_read_csv(out2 / "trades.csv")) == 3


def test_save_rejects_unsafe_run_id(tmp_path: Path) -> None:
    result = _result()
    result.run_id = "../escape"
    with pytest.raises(ValueError):
        save_backtest_result(result, tmp_path / "backtests")
    assert not (tmp_path / "escape").exists()


def test_result_json_has_no_nan(tmp_path: Path) -> None:
    metrics = {
        "total_return": math.nan, "sharpe": math.inf, "cagr": None, "n_trades": np.int64(2),
        "final_equity": np.float64(10_100.5),
    }
    result = _result(metrics=metrics, params={"threshold": float("nan"), "fast_period": np.int64(5)})
    out = save_backtest_result(result, tmp_path)
    text = (out / "result.json").read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text

    def reject(token: str) -> None:
        raise AssertionError(f"non-standard JSON constant {token}")

    payload = json.loads(text, parse_constant=reject)
    assert payload["metrics"]["total_return"] is None
    assert payload["metrics"]["sharpe"] is None
    assert payload["metrics"]["n_trades"] == 2
    assert payload["metrics"]["final_equity"] == 10_100.5
    assert payload["params"] == {"threshold": None, "fast_period": 5}


def test_format_metrics_table_korean_labels_and_percent() -> None:
    metrics = {
        "total_return": 0.1234,
        "max_drawdown": 0.05,
        "win_rate": None,
        "sharpe": 1.23456,
        "expectancy_r": -0.123456,
        "n_trades": 7,
        "final_equity": 11_234.5678,
        "funding_events": 12,
        "entries_capped_by_notional": 0,
        "n_wins": 4,  # no Korean label -> not printed
    }
    table = format_metrics_table(metrics)
    lines = table.splitlines()
    assert "총 수익률: 12.34%" in lines
    assert "최대 낙폭(MDD): 5.00%" in lines
    assert "승률: -" in lines
    assert "샤프 지수: 1.23" in lines
    assert "기대값(R): -0.1235" in lines
    assert "거래 수: 7" in lines
    assert "최종 자산: 11234.57" in lines
    assert "펀딩 적용 횟수: 12" in lines
    assert "명목가 상한 적용 진입: 0" in lines
    assert len(lines) == 9
    # lines follow the METRIC_LABELS_KO order
    order = [label for key, label in METRIC_LABELS_KO.items() if key in metrics]
    assert [line.split(": ")[0] for line in lines] == order
    # every percent metric is rendered with a % sign
    all_pct = format_metrics_table({k: 0.5 for k in PERCENT_METRICS})
    assert all(line.endswith("50.00%") for line in all_pct.splitlines())
    assert format_metrics_table({}) == ""


def test_format_metrics_table_numpy_and_nan() -> None:
    table = format_metrics_table({"sharpe": np.float64("nan"), "bars": np.int64(10), "cagr": np.float64(0.25)})
    assert table.splitlines() == ["연환산 수익률(CAGR): 25.00%", "샤프 지수: -", "봉 개수: 10"]


def test_report_reexports_labels_from_models() -> None:
    assert report.METRIC_LABELS_KO is models.METRIC_LABELS_KO
    assert report.PERCENT_METRICS is models.PERCENT_METRICS
    import bot.backtest as backtest_pkg

    assert backtest_pkg.METRIC_LABELS_KO is models.METRIC_LABELS_KO
    assert backtest_pkg.PERCENT_METRICS is models.PERCENT_METRICS
    assert backtest_pkg.format_metrics_table is format_metrics_table
