"""CLI (SPEC §12.1, §14.2 U6): parser, exit codes, offline backtest flow. No network, no real config.yaml."""

from __future__ import annotations

import argparse
import gc
import io
import json
import math
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from bot import cli
from bot.config import MAINNET_REST_URL
from bot.data.downloader import filters_cache_path, funding_cache_path, klines_cache_path
from bot.exchange.market import FUNDING_COLUMNS, FUNDING_DTYPES
from bot.models import SymbolFilters
from bot.storage import Storage
from bot.timeutil import now_ms
from tests.conftest import EXAMPLE_CONFIG

H = 3_600_000
START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
N_BARS = 30 * 24  # 30 days of 1h candles
FUNDING_STEP_MS = 8 * H
SMA_PARAMS = ["--param", "fast_period=5", "--param", "slow_period=20", "--param", "ma_type=SMA"]
BACKTEST_RANGE = ["--start", "2024-01-03", "--end", "2024-01-30"]


@pytest.fixture(autouse=True)
def _restore_sys_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # the CLI prepends the config folder to sys.path (user strategies); keep it out of other tests
    monkeypatch.setattr(sys, "path", list(sys.path))


def write_config(folder: Path, **changes: Any) -> Path:
    """config.example.yaml with top-level ``changes``, written to ``folder/config.yaml``."""
    with open(EXAMPLE_CONFIG, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    data.update(changes)
    path = folder / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def wave_closes(n: int) -> list[float]:
    """Oscillating prices: plenty of SMA crosses, so positions are held across funding times."""
    return [40_000.0 + 2_000.0 * math.sin(i / 15.0) + 3.0 * i for i in range(n)]


def seed_cache(
    folder: Path,
    candle_factory: Callable[..., pd.DataFrame],
    btc_filters: SymbolFilters,
    *,
    funding: bool = True,
) -> pd.DataFrame:
    """Kline CSV (+ funding CSV) + a fresh mainnet filters JSON under ``folder/data`` (the example cache_dir)."""
    cache = folder / "data"
    candles = candle_factory(wave_closes(N_BARS), start_ms=START_MS)
    kpath = klines_cache_path(cache, "BTCUSDT", "1h")
    kpath.parent.mkdir(parents=True, exist_ok=True)
    candles.to_csv(kpath, index=False, encoding="utf-8")
    if funding:
        times = list(range(START_MS, START_MS + N_BARS * H, FUNDING_STEP_MS))
        closes = candles.set_index("open_time")["close"]
        rows = pd.DataFrame(
            {
                "funding_time": times,
                "funding_rate": [0.0001] * len(times),
                "mark_price": [float(closes[t]) for t in times],
            },
            columns=list(FUNDING_COLUMNS),
        ).astype(FUNDING_DTYPES)
        fpath = funding_cache_path(cache, "BTCUSDT")
        fpath.parent.mkdir(parents=True, exist_ok=True)
        rows.to_csv(fpath, index=False, encoding="utf-8")
    jpath = filters_cache_path(cache, "BTCUSDT")
    jpath.parent.mkdir(parents=True, exist_ok=True)
    payload = btc_filters.to_dict() | {"fetched_at": now_ms(), "host": MAINNET_REST_URL}
    jpath.write_text(json.dumps(payload), encoding="utf-8")
    return candles


def test_parser_commands() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["strategies"])
    assert args.command == "strategies" and args.config is None and args.log_level is None

    # global options are accepted before AND after the command
    args = parser.parse_args(["-c", "a.yaml", "backtest", "--offline", "--no-save", "--no-funding"])
    assert (args.command, args.config, args.offline, args.no_save, args.no_funding) == ("backtest", "a.yaml", True, True, True)
    args = parser.parse_args(["backtest", "-c", "b.yaml", "--log-level", "DEBUG", "--symbol", "ETHUSDT"])
    assert (args.config, args.log_level, args.symbol) == ("b.yaml", "DEBUG", "ETHUSDT")

    args = parser.parse_args(["download", "--interval", "15m", "--start", "2024-01-01", "--no-funding"])
    assert (args.command, args.interval, args.start, args.no_funding) == ("download", "15m", "2024-01-01", True)
    args = parser.parse_args(["trade", "--once", "--mode", "testnet"])
    assert (args.command, args.once, args.mode, args.reset_paper) == ("trade", True, "testnet", False)
    args = parser.parse_args(["dashboard", "--host", "localhost", "--port", "8080"])
    assert (args.command, args.host, args.port) == ("dashboard", "localhost", 8080)

    for bad in ([], ["frobnicate"], ["backtest", "--interval", "1w"]):
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(bad)
        assert exc_info.value.code == 2
    assert cli.main(["frobnicate"]) == cli.EXIT_CONFIG
    assert cli.main(["--help"]) == cli.EXIT_OK


def test_param_parsing_types() -> None:
    assert cli.parse_param("fast_period=10") == ("fast_period", 10)
    assert cli.parse_param("x=1.5") == ("x", 1.5)
    assert cli.parse_param("allow_short=false") == ("allow_short", False)
    assert cli.parse_param("ma_type=SMA") == ("ma_type", "SMA")
    assert cli.parse_param(" key =a=b") == ("key", "a=b")  # only the first "=" splits
    for bad in ("novalue", "=5"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli.parse_param(bad)

    args = cli.build_parser().parse_args(["backtest", "--param", "fast_period=7", "--param", "ma_type=EMA"])
    assert args.param == [("fast_period", 7), ("ma_type", "EMA")]
    assert cli.main(["backtest", "--param", "oops"]) == cli.EXIT_CONFIG  # usage error


def test_trade_mode_live_rejected_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_config(tmp_path)
    assert cli.main(["-c", str(cfg), "trade", "--once", "--mode", "live"]) == cli.EXIT_CONFIG
    assert "--mode" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()  # nothing ran


def test_live_config_without_env_exit_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_config(tmp_path, mode="live")
    assert cli.main(["-c", str(cfg), "trade", "--once"]) == cli.EXIT_LIVE_NOT_CONFIRMED
    err = capsys.readouterr().err
    assert "CONFIRM_LIVE_TRADING" in err
    assert not (tmp_path / "data").exists()  # no lock, no DB: stopped before anything else


def test_dashboard_non_loopback_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot.dashboard.app

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(bot.dashboard.app, "run_dashboard", lambda cfg, host, port: calls.append((host, port)))
    cfg = write_config(tmp_path)
    assert cli.main(["-c", str(cfg), "dashboard", "--host", "0.0.0.0"]) == cli.EXIT_CONFIG
    assert "localhost only" in capsys.readouterr().err
    assert cli.main(["-c", str(cfg), "dashboard", "--port", "70000"]) == cli.EXIT_CONFIG
    assert calls == []

    assert cli.main(["-c", str(cfg), "dashboard", "--host", "::1", "--port", "8080"]) == cli.EXIT_OK
    assert calls == [("::1", 8080)]
    assert "http://[::1]:8080/" in capsys.readouterr().out


def test_backtest_offline_end_to_end(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    candle_factory: Callable[..., pd.DataFrame],
    btc_filters: SymbolFilters,
) -> None:
    seed_cache(tmp_path, candle_factory, btc_filters)
    cfg = write_config(tmp_path)
    code = cli.main(["-c", str(cfg), "backtest", "--offline", *BACKTEST_RANGE, *SMA_PARAMS])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK, out
    assert "펀딩비 반영" in out

    runs = sorted((tmp_path / "data" / "backtests").iterdir())
    assert len(runs) == 1
    run_dir = runs[0]
    assert {p.name for p in run_dir.iterdir()} >= {"result.json", "trades.csv", "equity.csv"}
    assert str(run_dir) in out
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))  # strict JSON: no NaN
    assert result["run_id"] == run_dir.name
    assert result["params"] == {"fast_period": 5, "slow_period": 20, "ma_type": "SMA", "allow_short": True}
    assert result["metrics"]["n_trades"] > 0
    assert result["metrics"]["total_funding"] != 0
    coverage = result["config"]["funding_coverage"]
    assert coverage["included"] is True and coverage["rows"] > 0

    with Storage(tmp_path / "data" / "bot.db", read_only=True) as st:
        rows = st.list_backtests()
    assert [r["run_id"] for r in rows] == [run_dir.name]
    assert (tmp_path / "logs" / "backtest.log").is_file()


def test_backtest_offline_without_funding_cache_raises(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    candle_factory: Callable[..., pd.DataFrame],
    btc_filters: SymbolFilters,
) -> None:
    seed_cache(tmp_path, candle_factory, btc_filters, funding=False)
    cfg = write_config(tmp_path)
    assert cli.main(["-c", str(cfg), "backtest", "--offline", *BACKTEST_RANGE, *SMA_PARAMS]) == cli.EXIT_ERROR
    assert "--no-funding" in capsys.readouterr().err
    assert not (tmp_path / "data" / "backtests").exists()

    # the hint works: the same run without funding succeeds and records that funding was not included
    args = ["-c", str(cfg), "backtest", "--offline", "--no-funding", *BACKTEST_RANGE, *SMA_PARAMS]
    assert cli.main(args) == cli.EXIT_OK
    (run_dir,) = (tmp_path / "data" / "backtests").iterdir()
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert result["config"]["funding_coverage"]["included"] is False
    assert result["metrics"]["total_funding"] == 0


def test_no_resource_warnings(
    tmp_path: Path, candle_factory: Callable[..., pd.DataFrame], btc_filters: SymbolFilters
) -> None:
    seed_cache(tmp_path, candle_factory, btc_filters)
    cfg = write_config(tmp_path)
    # recorded rather than raised: a ResourceWarning from __del__ cannot propagate as an exception
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        assert cli.main(["-c", str(cfg), "backtest", "--offline", *BACKTEST_RANGE, *SMA_PARAMS]) == cli.EXIT_OK
        gc.collect()
    leaks = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
    assert leaks == []


def test_stdout_reconfigured_errors_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a redirected console that cannot encode Korean (cp949 on the target machine; ascii is stricter)
    out_buf, err_buf = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out_buf, encoding="ascii", errors="strict"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(err_buf, encoding="ascii", errors="strict"))
    cfg = write_config(tmp_path)
    assert cli.main(["-c", str(cfg), "strategies"]) == cli.EXIT_OK
    assert cli.main(["-c", str(tmp_path / "missing.yaml"), "strategies"]) == cli.EXIT_CONFIG
    assert sys.stdout.errors == "replace" and sys.stderr.errors == "replace"
    sys.stdout.flush()
    sys.stderr.flush()
    out = out_buf.getvalue().decode("ascii")
    assert "ma_cross" in out and "?" in out  # Korean text replaced instead of raising UnicodeEncodeError
    assert "config file not found" in err_buf.getvalue().decode("ascii")


def test_strategies_lists_ma_cross(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_config(tmp_path)
    assert cli.main(["-c", str(cfg), "strategies"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "  - ma_cross: fast_period=20, slow_period=50, ma_type=EMA, allow_short=True" in out
    assert "ma_cross (fast_period=20, slow_period=50, ma_type=EMA, allow_short=True)" in out  # configured


class _StopRecorder:
    def __init__(self) -> None:
        self.stops = 0

    def request_stop(self) -> None:
        self.stops += 1


@pytest.mark.parametrize("text", ["hello\n STOP \nignored\n", "", "anything\n"])
def test_stdin_stop_watcher(text: str) -> None:
    # a "stop" line or the end of input (the launcher closed) both request a stop, exactly once
    trader = _StopRecorder()
    cli.watch_stdin_for_stop(io.StringIO(text), trader)  # type: ignore[arg-type]
    assert trader.stops == 1

    trader = _StopRecorder()
    cli._start_stdin_watcher(None, trader)  # type: ignore[arg-type]
    assert trader.stops == 1  # no stdin at all: never run unsupervised
