"""Command line interface (SPEC §12.1): ``python -m bot <command>``.

Commands: ``download``, ``backtest``, ``trade``, ``dashboard``, ``strategies``.
Exit codes: 0 success (also after a user stop of ``trade``); 1 runtime error; 2 ConfigError / usage;
3 live trading not confirmed; 130 interrupted (or ``trade`` aborted before startup finished).
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import threading
import traceback
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Final, TextIO

import pandas as pd
import yaml

from bot import __version__, logging_setup
from bot.backtest.engine import run_backtest
from bot.backtest.report import format_metrics_table, save_backtest_result
from bot.broker.paper import paper_state_key
from bot.config import (
    LOOPBACK_HOSTS,
    MAINNET_REST_URL,
    SUPPORTED_INTERVALS,
    AppConfig,
    assert_live_allowed,
    load_config,
    load_credentials,
    with_overrides,
)
from bot.data.downloader import (
    download_funding,
    download_klines,
    filters_cache_path,
    funding_cache_path,
    klines_cache_path,
    load_funding,
    load_klines,
    load_or_fetch_filters,
)
from bot.errors import BotError, ConfigError, DataError, LiveTradingNotConfirmed
from bot.exchange.market import MarketData
from bot.exchange.rest import BinanceRestClient
from bot.models import Mode
from bot.storage import Storage
from bot.strategy import available_strategies, create_strategy, get_strategy_class, load_strategy_modules
from bot.timeutil import interval_to_ms, ms_to_iso, now_ms, parse_date_ms
from bot.trader import (
    LOCK_FILE,
    SingleInstanceLock,
    Trader,
    active_trade_key,
    build_trader,
    cooldown_key,
    emergency_key,
    halted_key,
    kill_switch_key,
    last_bar_key,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG: Final = "config.yaml"
LOG_LEVEL_CHOICES: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_FUNDING_INTERVAL_MS: Final = 28_800_000  # 8 h
STDIN_STOP_COMMAND: Final = "stop"

EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_CONFIG: Final = 2
EXIT_LIVE_NOT_CONFIRMED: Final = 3
EXIT_INTERRUPTED: Final = 130


# ---------------------------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------------------------


def parse_param(text: str) -> tuple[str, Any]:
    """``KEY=VALUE`` -> ``(key, yaml.safe_load(value))`` (``10`` -> int, ``false`` -> bool, ``SMA`` -> str)."""
    key, sep, raw = str(text).partition("=")
    key = key.strip()
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise argparse.ArgumentTypeError(f"cannot parse the value of {key!r}: {exc}") from None
    return key, value


def _add_global_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    default: Any = argparse.SUPPRESS if suppress else None
    parser.add_argument(
        "-c",
        "--config",
        default=default,
        metavar="PATH",
        help=f"설정 파일 경로 / config file (기본값 {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--log-level",
        choices=LOG_LEVEL_CHOICES,
        default=default,
        help="로그 레벨 (설정 파일 값을 덮어씀) / overrides logging.level",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bot",
        description="바이낸스 USDT-M 무기한 선물 자동매매 봇 / Binance USDT-M futures trading bot",
    )
    _add_global_options(parser, suppress=False)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # The global options are also accepted AFTER the command (SUPPRESS keeps the main parser's value otherwise).
    common = argparse.ArgumentParser(add_help=False)
    _add_global_options(common, suppress=True)

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser(
        "download", parents=[common], help="과거 캔들/펀딩비/거래 규칙 다운로드 (메인넷 공개 데이터)"
    )
    p.add_argument("--symbol", help="심볼 (예: BTCUSDT)")
    p.add_argument("--interval", choices=SUPPORTED_INTERVALS, help="캔들 간격")
    p.add_argument("--start", metavar="DATE", help="시작일 YYYY-MM-DD (기본값: backtest.start)")
    p.add_argument("--end", metavar="DATE", help="종료일 YYYY-MM-DD (기본값: 현재)")
    p.add_argument("--no-funding", action="store_true", help="펀딩비 다운로드 생략")

    p = sub.add_parser("backtest", parents=[common], help="백테스트 실행")
    p.add_argument("--symbol", help="심볼 (예: BTCUSDT)")
    p.add_argument("--interval", choices=SUPPORTED_INTERVALS, help="캔들 간격")
    p.add_argument("--start", metavar="DATE", help="시작일 (기본값: backtest.start)")
    p.add_argument("--end", metavar="DATE", help="종료일 (기본값: backtest.end 또는 현재)")
    p.add_argument("--strategy", metavar="NAME", help="전략 이름 (python -m bot strategies)")
    p.add_argument(
        "--param",
        action="append",
        type=parse_param,
        default=[],
        metavar="KEY=VALUE",
        help="전략 파라미터 (여러 번 사용 가능, 값은 YAML 로 해석: 10, 1.5, true, SMA)",
    )
    p.add_argument("--initial-balance", type=float, metavar="USDT", help="초기 자산 (USDT)")
    p.add_argument("--no-funding", action="store_true", help="펀딩비 미반영")
    p.add_argument("--offline", action="store_true", help="네트워크 없이 캐시 데이터만 사용")
    p.add_argument("--no-save", action="store_true", help="결과 파일/DB 저장 안 함")

    p = sub.add_parser("trade", parents=[common], help="자동매매 실행 (기본: paper)")
    p.add_argument("--once", action="store_true", help="한 번만 실행하고 종료")
    p.add_argument(
        "--mode",
        choices=(Mode.PAPER.value, Mode.TESTNET.value),
        help="실행 모드 덮어쓰기 (live 는 설정 파일에서만 가능)",
    )
    p.add_argument("--reset-paper", action="store_true", help="페이퍼 모드 상태(잔고/포지션) 초기화 후 실행")
    p.add_argument(
        "--stop-on-stdin",
        action="store_true",
        help="표준입력의 'stop' 줄 또는 입력 종료(EOF) 시 Ctrl+C 처럼 안전하게 멈춤 (실행 창/런처용)",
    )

    p = sub.add_parser("dashboard", parents=[common], help="읽기 전용 웹 대시보드 (로컬 전용)")
    p.add_argument("--host", help="바인드 주소 (127.0.0.1 / localhost / ::1 만 허용)")
    p.add_argument("--port", type=int, help="포트")

    sub.add_parser("strategies", parents=[common], help="등록된 전략과 기본 파라미터 목록")
    return parser


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _out(text: str = "") -> None:
    print(text, flush=True)


def _config_path(args: argparse.Namespace) -> str:
    return getattr(args, "config", None) or DEFAULT_CONFIG


def _load_cfg(args: argparse.Namespace) -> AppConfig:
    cfg = load_config(_config_path(args))
    level = getattr(args, "log_level", None)
    if level:
        cfg = dataclasses.replace(cfg, logging=dataclasses.replace(cfg.logging, level=level))
    return cfg


def _setup_logging(cfg: AppConfig, command: str, secrets: Sequence[str] = ()) -> None:
    logging_setup.setup_logging(cfg.logging, base_dir=cfg.base_dir, log_name=command, secrets=secrets)


def _add_strategy_path(cfg: AppConfig) -> None:
    """User strategy modules (``strategy.extra_modules``) live next to config.yaml."""
    path = str(cfg.base_dir)
    if path not in sys.path:
        sys.path.insert(0, path)


def _parse_date(text: str, what: str) -> int:
    try:
        return parse_date_ms(text)
    except ConfigError as exc:
        raise ConfigError(f"{what}: {exc}") from None


def _params(pairs: Sequence[tuple[str, Any]] | None) -> dict[str, Any] | None:
    if not pairs:
        return None
    return {key: value for key, value in pairs}


def reset_paper_state(storage: Storage, symbol: str) -> list[str]:
    """``--reset-paper``: delete every paper-mode state key of ``symbol`` (balance, position, cursors, gates)."""
    mode = Mode.PAPER
    keys = [
        paper_state_key(symbol),
        active_trade_key(mode, symbol),
        kill_switch_key(mode, symbol),
        cooldown_key(mode, symbol),
        halted_key(mode, symbol),
        emergency_key(mode, symbol),
        *(last_bar_key(mode, symbol, interval) for interval in SUPPORTED_INTERVALS),
    ]
    for key in keys:
        storage.delete_state(key)
    storage.log_event("WARNING", mode.value, "PAPER_RESET", f"paper state of {symbol} reset (--reset-paper)")
    logger.warning("paper state of %s reset (--reset-paper)", symbol)
    return keys


def watch_stdin_for_stop(stream: Iterable[str], trader: Trader) -> None:
    """``--stop-on-stdin``: a ``stop`` line, or the end of input (the launcher closed or crashed), requests a stop.

    Same semantics as Ctrl+C: only sets the trader's stop flag, so an order sequence in progress is completed.
    """
    try:
        for line in stream:
            if line.strip().lower() == STDIN_STOP_COMMAND:
                logger.warning("stop requested on stdin")
                break
        else:
            logger.warning("stdin closed; stopping")
    except (OSError, ValueError) as exc:  # closed/unreadable stdin: stopping is the safe side
        logger.warning("stdin unreadable (%s); stopping", exc)
    trader.request_stop()


def _start_stdin_watcher(stream: TextIO | None, trader: Trader) -> None:
    if stream is None:  # pythonw without a stdin handle: nothing to watch, stop at once (never run unsupervised)
        trader.request_stop()
        return
    threading.Thread(target=watch_stdin_for_stop, args=(stream, trader), name="stdin-stop", daemon=True).start()


def funding_coverage_problem(funding: pd.DataFrame | None, start_ms: int, candles: pd.DataFrame) -> str | None:
    """None when the funding rows cover ``[start_ms, last candle close]`` (within one funding interval)."""
    if funding is None or len(funding) == 0:
        return "no funding rows"
    times = funding["funding_time"].astype("int64")
    diffs = times.diff().dropna()
    interval = int(diffs.median()) if len(diffs) else DEFAULT_FUNDING_INTERVAL_MS
    if interval <= 0:
        interval = DEFAULT_FUNDING_INTERVAL_MS
    first = int(times.iloc[0])
    last = int(times.iloc[-1])
    if first > int(start_ms) + interval:
        return f"first funding record {ms_to_iso(first)} is later than the start {ms_to_iso(int(start_ms))}"
    if len(candles) and last < int(candles["close_time"].iloc[-1]) - interval:
        return (
            f"last funding record {ms_to_iso(last)} is earlier than the last candle "
            f"{ms_to_iso(int(candles['close_time'].iloc[-1]))}"
        )
    return None


# ---------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------


def _cmd_download(args: argparse.Namespace) -> int:
    cfg = with_overrides(_load_cfg(args), symbol=args.symbol, interval=args.interval)
    _setup_logging(cfg, "download")
    start_ms = _parse_date(args.start or cfg.backtest.start, "--start")
    end_ms = _parse_date(args.end, "--end") if args.end else None
    if end_ms is not None and end_ms <= start_ms:
        raise ConfigError("--end must be after --start / 종료일은 시작일 이후여야 합니다")
    cache = cfg.cache_dir
    _out(
        f"다운로드 / download: {cfg.symbol} {cfg.interval} {ms_to_iso(start_ms)} ~ "
        f"{ms_to_iso(end_ms) if end_ms is not None else '현재 / now'} (메인넷 공개 데이터 / mainnet public data)"
    )
    with BinanceRestClient(MAINNET_REST_URL, recv_window_ms=cfg.execution.recv_window_ms) as client:
        market = MarketData(client)
        klines = download_klines(market, cache, cfg.symbol, cfg.interval, start_ms, end_ms)
        _out(f"캔들 / klines: {len(klines):,}행 -> {klines_cache_path(cache, cfg.symbol, cfg.interval)}")
        if args.no_funding:
            _out("펀딩비 / funding: 건너뜀 (--no-funding)")
        else:
            funding = download_funding(market, cache, cfg.symbol, start_ms, end_ms)
            _out(f"펀딩비 / funding: {len(funding):,}행 -> {funding_cache_path(cache, cfg.symbol)}")
        load_or_fetch_filters(market, cache, cfg.symbol)
        _out(f"거래 규칙 / exchange filters: {filters_cache_path(cache, cfg.symbol)}")
    return EXIT_OK


def _cmd_backtest(args: argparse.Namespace) -> int:
    # 1. config + overrides, strategy
    cfg = with_overrides(
        _load_cfg(args),
        symbol=args.symbol,
        interval=args.interval,
        strategy_name=args.strategy,
        strategy_params=_params(args.param),
        initial_balance=args.initial_balance,
    )
    _setup_logging(cfg, "backtest")
    _add_strategy_path(cfg)
    load_strategy_modules(cfg.strategy.extra_modules)
    strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
    interval_ms = interval_to_ms(cfg.interval)
    start_ms = _parse_date(args.start or cfg.backtest.start, "--start")
    end_text = args.end or cfg.backtest.end
    cache = cfg.cache_dir
    # 2. funding
    use_funding = bool(cfg.backtest.include_funding and not args.no_funding)

    client: BinanceRestClient | None = None
    try:
        market: MarketData | None = None
        if not args.offline:
            client = BinanceRestClient(MAINNET_REST_URL, recv_window_ms=cfg.execution.recv_window_ms)
            market = MarketData(client)
        if end_text:
            end_ms = _parse_date(end_text, "--end")
        else:
            end_ms = market.server_time() if market is not None else now_ms()
        if end_ms <= start_ms:
            raise ConfigError("backtest end must be after start / 종료일은 시작일 이후여야 합니다")
        warmup_ms = (int(strategy.warmup_bars) + int(cfg.risk.stop_loss.atr_period) + 5) * interval_ms
        data_start = start_ms - warmup_ms

        # 3. data (mainnet public data; never demo klines)
        if market is not None:
            download_klines(market, cache, cfg.symbol, cfg.interval, data_start, end_ms)
            if use_funding:
                download_funding(market, cache, cfg.symbol, data_start, end_ms)
            filters = load_or_fetch_filters(market, cache, cfg.symbol)
        else:
            filters = load_or_fetch_filters(None, cache, cfg.symbol)
    finally:
        if client is not None:
            client.close()

    # 4. candles
    df = load_klines(cache, cfg.symbol, cfg.interval, data_start, end_ms)
    if df.empty:
        raise DataError(
            f"no cached klines for {cfg.symbol} {cfg.interval} in the requested range; run: python -m bot download "
            f"--symbol {cfg.symbol} --interval {cfg.interval} (캐시된 캔들이 없습니다)"
        )

    # 5. funding (online and offline)
    funding: pd.DataFrame | None = None
    if use_funding:
        funding = load_funding(cache, cfg.symbol, data_start, end_ms)
        problem = funding_coverage_problem(funding, start_ms, df)
        if problem is not None:
            if args.offline and len(funding) == 0:
                raise DataError(
                    f"no cached funding for {cfg.symbol}; run: python -m bot download --symbol {cfg.symbol} "
                    "... or use --no-funding (캐시된 펀딩비가 없습니다. download 를 실행하거나 --no-funding 을 쓰세요)"
                )
            logger.warning(
                "펀딩비 데이터가 기간 전체를 덮지 않습니다 / funding data does not cover the whole period: %s", problem
            )

    # 6. snapshot
    rows = int(len(funding)) if funding is not None else 0
    config_snapshot = cfg.to_dict() | {
        "funding_coverage": {
            "included": use_funding,
            "rows": rows,
            "first": int(funding["funding_time"].iloc[0]) if funding is not None and rows else None,
            "last": int(funding["funding_time"].iloc[-1]) if funding is not None and rows else None,
        }
    }

    # 7. run
    _out(
        f"백테스트 / backtest: {cfg.symbol} {cfg.interval} {strategy.describe()} "
        f"{ms_to_iso(start_ms)} ~ {ms_to_iso(end_ms)} (캔들 {len(df):,}개, 펀딩비 {'반영' if use_funding else '미반영'})"
    )
    result = run_backtest(
        df,
        strategy,
        symbol=cfg.symbol,
        interval=cfg.interval,
        filters=filters,
        risk=cfg.risk,
        execution=cfg.execution,
        initial_balance=cfg.backtest.initial_balance,
        funding=funding,
        trade_start_ms=start_ms,
        config_snapshot=config_snapshot,
    )

    # 8. save
    out_dir: Path | None = None
    if not args.no_save:
        with Storage(cfg.db_path) as st:
            out_dir = save_backtest_result(result, cfg.resolve_path(cfg.backtest.results_dir), st)

    # 9. print
    _out(f"실행 ID / run id: {result.run_id}")
    _out(format_metrics_table(result.metrics))
    if out_dir is not None:
        _out(f"결과 폴더 / result directory: {out_dir}")
    else:
        _out("결과 저장 안 함 / not saved (--no-save)")
    return EXIT_OK


def _cmd_trade(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    if args.mode:
        cfg = with_overrides(cfg, mode=args.mode)  # "live" is rejected (config file only)
    assert_live_allowed(cfg)  # exit 3 before anything touches the network
    creds = load_credentials(cfg)  # None in paper mode
    secrets = [creds.api_key, creds.api_secret] if creds is not None else []
    _setup_logging(cfg, "trade", secrets)
    if args.reset_paper and Mode(cfg.mode) is not Mode.PAPER:
        raise ConfigError("--reset-paper is only valid in paper mode / --reset-paper 는 paper 모드에서만 사용할 수 있습니다")
    _add_strategy_path(cfg)
    with SingleInstanceLock(cfg.resolve_path(LOCK_FILE)):
        with Storage(cfg.db_path) as st:
            if args.reset_paper:
                reset_paper_state(st, cfg.symbol)
                _out(f"페이퍼 상태 초기화 / paper state reset: {cfg.symbol}")
            trader = build_trader(cfg, st)
            if args.stop_on_stdin:
                _start_stdin_watcher(sys.stdin, trader)
            try:
                trader.run_forever(max_iterations=1 if args.once else None)
            finally:
                trader.close()
            report = trader.last_report
            if args.once and report is not None:
                bar = ms_to_iso(report.bar_open_time) if report.bar_open_time is not None else "-"
                _out(
                    f"봉 {bar}: 신호 {report.signal.value if report.signal else '-'} -> {report.action.value}"
                    f"{' (실행됨)' if report.executed else ''}"
                    f"{f' [{report.skipped_reason}]' if report.skipped_reason else ''}"
                )
            if trader.stopped_before_start:
                _out("시작 전에 중단됨 / aborted before startup finished")
                return EXIT_INTERRUPTED
    return EXIT_OK


def _cmd_dashboard(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    host = args.host or cfg.dashboard.host
    port = int(args.port) if args.port is not None else int(cfg.dashboard.port)
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"dashboard must bind to localhost only ({', '.join(sorted(LOOPBACK_HOSTS))}); got {host!r} "
            "/ 대시보드는 로컬 주소에서만 실행할 수 있습니다"
        )
    if not 1 <= port <= 65535:
        raise ConfigError(f"dashboard port must be in 1..65535 (got {port})")
    _setup_logging(cfg, "dashboard")
    from bot.dashboard.app import run_dashboard  # FastAPI/uvicorn only when needed

    shown = f"[{host}]" if ":" in host else host
    _out(f"대시보드 (읽기 전용) / dashboard (read-only): http://{shown}:{port}/  (종료: Ctrl+C)")
    run_dashboard(cfg, host, port)
    return EXIT_OK


def _cmd_strategies(args: argparse.Namespace) -> int:
    cfg: AppConfig | None = None
    explicit = getattr(args, "config", None)
    if explicit or Path(DEFAULT_CONFIG).is_file():
        cfg = _load_cfg(args)
        _setup_logging(cfg, "strategies")
        _add_strategy_path(cfg)
        load_strategy_modules(cfg.strategy.extra_modules)
    else:
        _out(f"({DEFAULT_CONFIG} 이 없어 기본 전략만 표시합니다 / no {DEFAULT_CONFIG}: built-in strategies only)")
    _out("등록된 전략 / available strategies:")
    for name in available_strategies():
        defaults = get_strategy_class(name).default_params()
        shown = ", ".join(f"{key}={value}" for key, value in defaults.items()) or "-"
        _out(f"  - {name}: {shown}")
    if cfg is not None:
        params = ", ".join(f"{key}={value}" for key, value in cfg.strategy.params.items()) or "-"
        _out(f"현재 설정 / configured: {cfg.strategy.name} ({params})")
    return EXIT_OK


_COMMANDS: Final[dict[str, Callable[[argparse.Namespace], int]]] = {
    "download": _cmd_download,
    "backtest": _cmd_backtest,
    "trade": _cmd_trade,
    "dashboard": _cmd_dashboard,
    "strategies": _cmd_strategies,
}


# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------


def _reconfigure_streams() -> None:
    # Redirected stdout on this machine is cp949: the Korean output must never raise UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def _log_to_files(level: int, message: str, exc: BaseException | None) -> None:
    """Write ``message`` (and the traceback) to the log FILES only; the console gets the one-line message."""
    bot_logger = logging.getLogger(logging_setup.BOT_LOGGER)
    handlers = [h for h in bot_logger.handlers if isinstance(h, logging.FileHandler)]
    if not handlers:
        return
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    record = logger.makeRecord(logger.name, level, __file__, 0, message, None, exc_info)
    for handler in handlers:
        if record.levelno >= handler.level:
            handler.handle(record)


def _report_error(label: str, exc: BaseException, *, debug: bool, with_traceback_in_log: bool = False) -> None:
    text = logging_setup.redact(" ".join(str(exc).split()) or type(exc).__name__)
    line = f"{label}: {text}"
    print(line, file=sys.stderr, flush=True)
    _log_to_files(logging.ERROR, line, exc if (debug or with_traceback_in_log) else None)
    if debug:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        print(logging_setup.redact(tb), file=sys.stderr, flush=True)


def _exit_code(code: object) -> int:
    if code is None:
        return EXIT_OK
    if isinstance(code, int):
        return code
    return EXIT_CONFIG


def main(argv: Sequence[str] | None = None) -> int:
    _reconfigure_streams()  # first thing, before any output
    parser = build_parser()
    try:
        args = parser.parse_args(None if argv is None else list(argv))
    except SystemExit as exc:  # --help (0) or a usage error (2)
        return _exit_code(exc.code)
    debug = getattr(args, "log_level", None) == "DEBUG"
    try:
        return int(_COMMANDS[args.command](args))
    except LiveTradingNotConfirmed as exc:
        _report_error("실거래 확인 필요 / live trading not confirmed", exc, debug=debug)
        return EXIT_LIVE_NOT_CONFIRMED
    except ConfigError as exc:
        _report_error("설정 오류 / configuration error", exc, debug=debug)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("중단됨 / interrupted", file=sys.stderr, flush=True)
        return EXIT_INTERRUPTED
    except BotError as exc:
        _report_error("오류 / error", exc, debug=debug)
        return EXIT_ERROR
    except Exception as exc:
        _report_error(
            f"예기치 않은 오류 / unexpected error ({type(exc).__name__})", exc, debug=debug, with_traceback_in_log=True
        )
        return EXIT_ERROR
    finally:
        logging_setup.shutdown_logging()
