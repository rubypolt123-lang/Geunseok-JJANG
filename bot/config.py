"""Configuration: YAML loading, validation, overrides, credentials and the live-trading gate (SPEC §3).

Config objects contain ``MappingProxyType`` (strategy params), which cannot be pickled: never use
``dataclasses.asdict`` / ``copy.deepcopy`` on them. ``AppConfig.to_dict`` walks fields via ``to_jsonable``.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal

import yaml
from dotenv import dotenv_values

from bot.errors import ConfigError, LiveTradingNotConfirmed
from bot.models import BOT_ID_RE, Mode, to_jsonable
from bot.timeutil import parse_date_ms

logger = logging.getLogger(__name__)

ABSOLUTE_MAX_LEVERAGE: Final[int] = 20
MAINNET_REST_URL: Final[str] = "https://fapi.binance.com"
TESTNET_REST_URL: Final[str] = "https://demo-fapi.binance.com"
SUPPORTED_INTERVALS: Final[tuple[str, ...]] = (
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
)
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
SYMBOL_RE: Final = re.compile(r"^[A-Z0-9]{2,20}USDT$")
LOG_LEVELS: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Environment variable names (never read CONFIRM_LIVE_TRADING from .env).
ENV_CONFIRM_LIVE: Final = "CONFIRM_LIVE_TRADING"
ENV_TESTNET_KEY: Final = "BINANCE_TESTNET_API_KEY"
ENV_TESTNET_SECRET: Final = "BINANCE_TESTNET_API_SECRET"
ENV_LIVE_KEY: Final = "BINANCE_API_KEY"
ENV_LIVE_SECRET: Final = "BINANCE_API_SECRET"

# Module-level defaults: IDENTICAL in values to config.example.yaml (tested).
DEFAULTS: Final[dict[str, Any]] = {
    "mode": "paper",
    "symbol": "BTCUSDT",
    "interval": "1h",
    "strategy": {
        "name": "ma_cross",
        "extra_modules": [],
        "params": {
            "fast_period": 20,
            "slow_period": 50,
            "ma_type": "EMA",
            "allow_short": True,
        },
    },
    "risk": {
        "leverage": 3,
        "max_leverage": 10,
        "risk_per_trade_pct": 1.0,
        "stop_loss": {
            "mode": "atr",
            "percent": 2.0,
            "atr_period": 14,
            "atr_multiple": 2.0,
        },
        "take_profit_r": 2.0,
        "max_position_notional": 20000,
        "max_margin_fraction": 0.9,
        "max_daily_loss_pct": 5.0,
        "kill_switch_flatten": True,
        "cooldown_bars_after_stop": 3,
        "min_liq_distance_multiple": 2.0,
        "maint_margin_rate": 0.004,
        "liq_mmr_buffer": 0.005,
    },
    "execution": {
        "fees": {"maker": 0.0002, "taker": 0.0005},
        "slippage_bps": 5,
        "working_type": "MARK_PRICE",
        "price_protect": False,
        "protective_mode": "close_position",
        "candle_close_delay_sec": 3,
        "kline_limit": 500,
        "recv_window_ms": 5000,
        "heartbeat_sec": 30,
        "bot_id": "mab1",
    },
    "paper": {"initial_balance": 10000, "include_funding": True},
    "backtest": {
        "start": "2024-01-01",
        "end": None,
        "initial_balance": 10000,
        "include_funding": True,
        "results_dir": "data/backtests",
    },
    "data": {"cache_dir": "data"},
    "storage": {"db_path": "data/bot.db"},
    "dashboard": {"host": "127.0.0.1", "port": 8000, "refresh_sec": 10},
    "logging": {"level": "INFO", "dir": "logs", "max_bytes": 5242880, "backup_count": 5},
    "halt_file": "data/STOP",
}

# ---------------------------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    name: str
    extra_modules: tuple[str, ...]
    params: Mapping[str, Any]  # stored as MappingProxyType


@dataclass(frozen=True, slots=True)
class StopLossConfig:
    mode: Literal["percent", "atr"]
    percent: float
    atr_period: int
    atr_multiple: float


@dataclass(frozen=True, slots=True)
class RiskConfig:
    leverage: int
    max_leverage: int
    risk_per_trade_pct: float
    stop_loss: StopLossConfig
    take_profit_r: float | None
    max_position_notional: float
    max_margin_fraction: float
    max_daily_loss_pct: float
    kill_switch_flatten: bool
    cooldown_bars_after_stop: int
    min_liq_distance_multiple: float
    maint_margin_rate: float
    liq_mmr_buffer: float


@dataclass(frozen=True, slots=True)
class FeeConfig:
    maker: float
    taker: float


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    fees: FeeConfig
    slippage_bps: float
    working_type: Literal["MARK_PRICE", "CONTRACT_PRICE"]
    price_protect: bool
    protective_mode: Literal["close_position", "reduce_only"]
    candle_close_delay_sec: float
    kline_limit: int
    recv_window_ms: int
    heartbeat_sec: int
    bot_id: str


@dataclass(frozen=True, slots=True)
class PaperConfig:
    initial_balance: float
    include_funding: bool


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    start: str
    end: str | None
    initial_balance: float
    include_funding: bool
    results_dir: str


@dataclass(frozen=True, slots=True)
class DataConfig:
    cache_dir: str


@dataclass(frozen=True, slots=True)
class StorageConfig:
    db_path: str


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    host: str
    port: int
    refresh_sec: int


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str
    dir: str
    max_bytes: int
    backup_count: int


@dataclass(frozen=True, slots=True)
class Credentials:
    api_key: str = field(repr=False, metadata={"secret": True})
    api_secret: str = field(repr=False, metadata={"secret": True})

    def __repr__(self) -> str:
        return "Credentials(api_key=***, api_secret=***)"


@dataclass(frozen=True, slots=True)
class AppConfig:
    mode: Mode
    symbol: str
    interval: str
    strategy: StrategyConfig
    risk: RiskConfig
    execution: ExecutionConfig
    paper: PaperConfig
    backtest: BacktestConfig
    data: DataConfig
    storage: StorageConfig
    dashboard: DashboardConfig
    logging: LoggingConfig
    halt_file: str
    base_dir: Path  # absolute; directory of the config file (or override)

    def resolve_path(self, p: str | Path) -> Path:
        """Absolute ``p`` unchanged; relative -> ``base_dir / p``."""
        path = Path(p)
        return path if path.is_absolute() else self.base_dir / path

    @property
    def db_path(self) -> Path:
        return self.resolve_path(self.storage.db_path)

    @property
    def cache_dir(self) -> Path:
        return self.resolve_path(self.data.cache_dir)

    @property
    def halt_path(self) -> Path:
        return self.resolve_path(self.halt_file)

    def rest_base_url(self) -> str:
        """paper/live -> MAINNET_REST_URL; testnet -> TESTNET_REST_URL."""
        return TESTNET_REST_URL if self.mode == Mode.TESTNET else MAINNET_REST_URL

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-able dict of every field (base_dir as str). Contains no secrets."""
        return to_jsonable(self)


# ---------------------------------------------------------------------------------------------
# Type coercion helpers (every error names the key path)
# ---------------------------------------------------------------------------------------------


def _as_int(v: Any, key: str) -> int:
    if isinstance(v, bool):
        raise ConfigError(f"{key} must be an integer, not a boolean ({v!r})")
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return int(v)
    raise ConfigError(f"{key} must be an integer (got {v!r})")


def _as_float(v: Any, key: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{key} must be a number (got {v!r})")
    f = float(v)
    if not math.isfinite(f):
        raise ConfigError(f"{key} must be a finite number (got {v!r})")
    return f


def _as_opt_float(v: Any, key: str) -> float | None:
    return None if v is None else _as_float(v, key)


def _as_bool(v: Any, key: str) -> bool:
    if not isinstance(v, bool):
        raise ConfigError(f"{key} must be true or false (got {v!r})")
    return v


def _as_str(v: Any, key: str) -> str:
    if not isinstance(v, str):
        raise ConfigError(f"{key} must be a string (got {v!r}; use quotes)")
    return v.strip()


def _as_date_str(v: Any, key: str) -> str:
    # An unquoted YAML date (2024-01-01) is parsed as datetime.date; accept it as its ISO text.
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return _as_str(v, key)


def _as_mode(v: Any, key: str = "mode") -> Mode:
    text = _as_str(v, key).lower()
    try:
        return Mode(text)
    except ValueError:
        raise ConfigError(f"{key} must be one of paper, testnet, live (got {v!r})") from None


# ---------------------------------------------------------------------------------------------
# Merge / build / validate
# ---------------------------------------------------------------------------------------------

_PARAMS_PATH: Final = "strategy.params"


def _copy_plain(value: Any) -> Any:
    """Copy plain YAML-like data (dict/list) so DEFAULTS is never mutated."""
    if isinstance(value, dict):
        return {k: _copy_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_plain(v) for v in value]
    return value


def _merge(base: Mapping[str, Any], override: Any, path: str) -> dict[str, Any]:
    """Deep-merge ``override`` over ``base``; unknown keys (outside strategy.params) -> ConfigError."""
    if override is None:
        override = {}
    if not isinstance(override, Mapping):
        raise ConfigError(f"{path or 'config'} must be a mapping (got {override!r})")
    out = {k: _copy_plain(v) for k, v in base.items()}
    for key, value in override.items():
        full = f"{path}.{key}" if path else str(key)
        if not isinstance(key, str) or key not in base:
            raise ConfigError(f"unknown config key: {full}")
        if full == _PARAMS_PATH:
            if value is None:
                value = {}
            if not isinstance(value, Mapping):
                raise ConfigError(f"{_PARAMS_PATH} must be a mapping (got {value!r})")
            # Strategy params are opaque to the config layer: the file's mapping replaces the defaults
            # (the strategy fills in its own defaults and rejects unknown keys).
            out[key] = dict(value)
        elif isinstance(base[key], dict):
            out[key] = _merge(base[key], value, full)
        else:
            out[key] = value
    return out


def _build(raw: Mapping[str, Any], base_dir: Path) -> AppConfig:
    """Typed AppConfig from a fully merged raw dict (type rules of §3.6), then validated."""
    s = raw["strategy"]
    r = raw["risk"]
    sl = r["stop_loss"]
    ex = raw["execution"]
    fees = ex["fees"]
    pa = raw["paper"]
    bt = raw["backtest"]
    db = raw["dashboard"]
    lg = raw["logging"]

    extra_modules = s["extra_modules"]
    if extra_modules is None:
        extra_modules = []
    if not isinstance(extra_modules, (list, tuple)):
        raise ConfigError(f"strategy.extra_modules must be a list of strings (got {extra_modules!r})")
    modules = tuple(_as_str(m, f"strategy.extra_modules[{i}]") for i, m in enumerate(extra_modules))

    params = s["params"]
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise ConfigError(f"{_PARAMS_PATH} must be a mapping (got {params!r})")

    cfg = AppConfig(
        mode=_as_mode(raw["mode"]),
        symbol=_as_str(raw["symbol"], "symbol").upper(),
        interval=_as_str(raw["interval"], "interval"),
        strategy=StrategyConfig(
            name=_as_str(s["name"], "strategy.name"),
            extra_modules=modules,
            params=MappingProxyType(dict(params)),
        ),
        risk=RiskConfig(
            leverage=_as_int(r["leverage"], "risk.leverage"),
            max_leverage=_as_int(r["max_leverage"], "risk.max_leverage"),
            risk_per_trade_pct=_as_float(r["risk_per_trade_pct"], "risk.risk_per_trade_pct"),
            stop_loss=StopLossConfig(
                mode=_as_str(sl["mode"], "risk.stop_loss.mode"),  # type: ignore[arg-type]
                percent=_as_float(sl["percent"], "risk.stop_loss.percent"),
                atr_period=_as_int(sl["atr_period"], "risk.stop_loss.atr_period"),
                atr_multiple=_as_float(sl["atr_multiple"], "risk.stop_loss.atr_multiple"),
            ),
            take_profit_r=_as_opt_float(r["take_profit_r"], "risk.take_profit_r"),
            max_position_notional=_as_float(r["max_position_notional"], "risk.max_position_notional"),
            max_margin_fraction=_as_float(r["max_margin_fraction"], "risk.max_margin_fraction"),
            max_daily_loss_pct=_as_float(r["max_daily_loss_pct"], "risk.max_daily_loss_pct"),
            kill_switch_flatten=_as_bool(r["kill_switch_flatten"], "risk.kill_switch_flatten"),
            cooldown_bars_after_stop=_as_int(r["cooldown_bars_after_stop"], "risk.cooldown_bars_after_stop"),
            min_liq_distance_multiple=_as_float(r["min_liq_distance_multiple"], "risk.min_liq_distance_multiple"),
            maint_margin_rate=_as_float(r["maint_margin_rate"], "risk.maint_margin_rate"),
            liq_mmr_buffer=_as_float(r["liq_mmr_buffer"], "risk.liq_mmr_buffer"),
        ),
        execution=ExecutionConfig(
            fees=FeeConfig(
                maker=_as_float(fees["maker"], "execution.fees.maker"),
                taker=_as_float(fees["taker"], "execution.fees.taker"),
            ),
            slippage_bps=_as_float(ex["slippage_bps"], "execution.slippage_bps"),
            working_type=_as_str(ex["working_type"], "execution.working_type"),  # type: ignore[arg-type]
            price_protect=_as_bool(ex["price_protect"], "execution.price_protect"),
            protective_mode=_as_str(ex["protective_mode"], "execution.protective_mode"),  # type: ignore[arg-type]
            candle_close_delay_sec=_as_float(ex["candle_close_delay_sec"], "execution.candle_close_delay_sec"),
            kline_limit=_as_int(ex["kline_limit"], "execution.kline_limit"),
            recv_window_ms=_as_int(ex["recv_window_ms"], "execution.recv_window_ms"),
            heartbeat_sec=_as_int(ex["heartbeat_sec"], "execution.heartbeat_sec"),
            bot_id=_as_str(ex["bot_id"], "execution.bot_id"),
        ),
        paper=PaperConfig(
            initial_balance=_as_float(pa["initial_balance"], "paper.initial_balance"),
            include_funding=_as_bool(pa["include_funding"], "paper.include_funding"),
        ),
        backtest=BacktestConfig(
            start=_as_date_str(bt["start"], "backtest.start"),
            end=None if bt["end"] is None else _as_date_str(bt["end"], "backtest.end"),
            initial_balance=_as_float(bt["initial_balance"], "backtest.initial_balance"),
            include_funding=_as_bool(bt["include_funding"], "backtest.include_funding"),
            results_dir=_as_str(bt["results_dir"], "backtest.results_dir"),
        ),
        data=DataConfig(cache_dir=_as_str(raw["data"]["cache_dir"], "data.cache_dir")),
        storage=StorageConfig(db_path=_as_str(raw["storage"]["db_path"], "storage.db_path")),
        dashboard=DashboardConfig(
            host=_as_str(db["host"], "dashboard.host"),
            port=_as_int(db["port"], "dashboard.port"),
            refresh_sec=_as_int(db["refresh_sec"], "dashboard.refresh_sec"),
        ),
        logging=LoggingConfig(
            level=_as_str(lg["level"], "logging.level").upper(),
            dir=_as_str(lg["dir"], "logging.dir"),
            max_bytes=_as_int(lg["max_bytes"], "logging.max_bytes"),
            backup_count=_as_int(lg["backup_count"], "logging.backup_count"),
        ),
        halt_file=_as_str(raw["halt_file"], "halt_file"),
        base_dir=base_dir,
    )
    _validate(cfg)
    return cfg


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ConfigError(message)


def _validate(cfg: AppConfig) -> None:
    """Value rules of §3.6 (each failure -> ConfigError naming the key path)."""
    _require(isinstance(cfg.mode, Mode), f"mode must be one of paper, testnet, live (got {cfg.mode!r})")
    _require(
        bool(SYMBOL_RE.fullmatch(cfg.symbol)),
        f"symbol must be a USDT-M perpetual matching ^[A-Z0-9]{{2,20}}USDT$ (got {cfg.symbol!r})",
    )
    _require(
        cfg.interval in SUPPORTED_INTERVALS,
        f"interval {cfg.interval!r} is not supported; use one of {' '.join(SUPPORTED_INTERVALS)}",
    )

    s = cfg.strategy
    _require(bool(s.name), "strategy.name must be a non-empty string")
    _require(
        all(isinstance(m, str) and m for m in s.extra_modules),
        "strategy.extra_modules must be a list of non-empty strings",
    )
    _require(isinstance(s.params, Mapping), f"{_PARAMS_PATH} must be a mapping")

    r = cfg.risk
    _require(
        r.max_leverage <= ABSOLUTE_MAX_LEVERAGE,
        f"risk.max_leverage exceeds hard cap {ABSOLUTE_MAX_LEVERAGE} (got {r.max_leverage})",
    )
    _require(r.max_leverage >= 1, f"risk.max_leverage must be >= 1 (got {r.max_leverage})")
    _require(r.leverage >= 1, f"risk.leverage must be >= 1 (got {r.leverage})")
    _require(
        r.leverage <= r.max_leverage,
        f"risk.leverage exceeds max_leverage ({r.leverage} > {r.max_leverage})",
    )
    _require(0 < r.risk_per_trade_pct <= 5, f"risk.risk_per_trade_pct must be in (0, 5] (got {r.risk_per_trade_pct})")
    sl = r.stop_loss
    _require(sl.mode in ("percent", "atr"), f"risk.stop_loss.mode must be percent or atr (got {sl.mode!r})")
    _require(0 < sl.percent < 50, f"risk.stop_loss.percent must be in (0, 50) (got {sl.percent})")
    _require(sl.atr_period >= 1, f"risk.stop_loss.atr_period must be >= 1 (got {sl.atr_period})")
    _require(sl.atr_multiple > 0, f"risk.stop_loss.atr_multiple must be > 0 (got {sl.atr_multiple})")
    _require(
        r.take_profit_r is None or r.take_profit_r > 0,
        f"risk.take_profit_r must be null or > 0 (got {r.take_profit_r})",
    )
    _require(r.max_position_notional > 0, f"risk.max_position_notional must be > 0 (got {r.max_position_notional})")
    _require(
        0 < r.max_margin_fraction <= 1,
        f"risk.max_margin_fraction must be in (0, 1] (got {r.max_margin_fraction})",
    )
    _require(
        0 <= r.max_daily_loss_pct < 100,
        f"risk.max_daily_loss_pct must be in [0, 100) (got {r.max_daily_loss_pct})",
    )
    _require(
        r.cooldown_bars_after_stop >= 0,
        f"risk.cooldown_bars_after_stop must be >= 0 (got {r.cooldown_bars_after_stop})",
    )
    _require(
        r.min_liq_distance_multiple >= 1,
        f"risk.min_liq_distance_multiple must be >= 1 (got {r.min_liq_distance_multiple})",
    )
    _require(0 <= r.maint_margin_rate < 0.5, f"risk.maint_margin_rate must be in [0, 0.5) (got {r.maint_margin_rate})")
    _require(0 <= r.liq_mmr_buffer < 0.5, f"risk.liq_mmr_buffer must be in [0, 0.5) (got {r.liq_mmr_buffer})")

    ex = cfg.execution
    _require(0 <= ex.fees.maker <= 0.01, f"execution.fees.maker must be in [0, 0.01] (got {ex.fees.maker})")
    _require(0 <= ex.fees.taker <= 0.01, f"execution.fees.taker must be in [0, 0.01] (got {ex.fees.taker})")
    _require(0 <= ex.slippage_bps <= 500, f"execution.slippage_bps must be in [0, 500] (got {ex.slippage_bps})")
    _require(
        ex.working_type in ("MARK_PRICE", "CONTRACT_PRICE"),
        f"execution.working_type must be MARK_PRICE or CONTRACT_PRICE (got {ex.working_type!r})",
    )
    _require(
        ex.protective_mode in ("close_position", "reduce_only"),
        f"execution.protective_mode must be close_position or reduce_only (got {ex.protective_mode!r})",
    )
    _require(
        0 <= ex.candle_close_delay_sec <= 60,
        f"execution.candle_close_delay_sec must be in [0, 60] (got {ex.candle_close_delay_sec})",
    )
    _require(50 <= ex.kline_limit <= 1500, f"execution.kline_limit must be in [50, 1500] (got {ex.kline_limit})")
    _require(
        1 <= ex.recv_window_ms <= 60000,
        f"execution.recv_window_ms must be in [1, 60000] (got {ex.recv_window_ms})",
    )
    _require(5 <= ex.heartbeat_sec <= 600, f"execution.heartbeat_sec must be in [5, 600] (got {ex.heartbeat_sec})")
    _require(
        bool(BOT_ID_RE.fullmatch(ex.bot_id)),
        f"execution.bot_id must be 1-8 ASCII letters/digits (got {ex.bot_id!r})",
    )

    _require(cfg.paper.initial_balance > 0, f"paper.initial_balance must be > 0 (got {cfg.paper.initial_balance})")
    bt = cfg.backtest
    _require(bt.initial_balance > 0, f"backtest.initial_balance must be > 0 (got {bt.initial_balance})")
    try:
        start_ms = parse_date_ms(bt.start)
    except ConfigError as exc:
        raise ConfigError(f"backtest.start: {exc}") from None
    if bt.end is not None:
        try:
            end_ms = parse_date_ms(bt.end)
        except ConfigError as exc:
            raise ConfigError(f"backtest.end: {exc}") from None
        _require(end_ms > start_ms, f"backtest.end must be after backtest.start ({bt.end!r} <= {bt.start!r})")
    _require(bool(bt.results_dir), "backtest.results_dir must be a non-empty path")

    _require(bool(cfg.data.cache_dir), "data.cache_dir must be a non-empty path")
    _require(bool(cfg.storage.db_path), "storage.db_path must be a non-empty path")
    _require(bool(cfg.halt_file), "halt_file must be a non-empty path")

    d = cfg.dashboard
    _require(
        d.host in LOOPBACK_HOSTS,
        f"dashboard.host: dashboard must bind to localhost only (127.0.0.1, localhost, ::1; got {d.host!r})",
    )
    _require(1 <= d.port <= 65535, f"dashboard.port must be in [1, 65535] (got {d.port})")
    _require(2 <= d.refresh_sec <= 3600, f"dashboard.refresh_sec must be in [2, 3600] (got {d.refresh_sec})")

    lg = cfg.logging
    _require(lg.level in LOG_LEVELS, f"logging.level must be one of {', '.join(LOG_LEVELS)} (got {lg.level!r})")
    _require(bool(lg.dir), "logging.dir must be a non-empty path")
    _require(lg.max_bytes >= 10000, f"logging.max_bytes must be >= 10000 (got {lg.max_bytes})")
    _require(0 <= lg.backup_count <= 50, f"logging.backup_count must be in [0, 50] (got {lg.backup_count})")

    _require(cfg.base_dir.is_absolute(), f"base_dir must be absolute (got {cfg.base_dir})")


# ---------------------------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------------------------


def load_config(path: str | Path = "config.yaml", *, base_dir: str | Path | None = None) -> AppConfig:
    """Load, merge over DEFAULTS, type-check and validate a YAML config file."""
    cfg_path = Path(path)
    if not cfg_path.is_file():
        raise ConfigError(f"config file not found: {cfg_path}; copy config.example.yaml to config.yaml")
    try:
        # utf-8-sig: identical to utf-8 but tolerates a BOM written by Windows editors.
        with open(cfg_path, encoding="utf-8-sig") as fh:
            loaded = yaml.safe_load(fh)
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config file {cfg_path} is not valid UTF-8: {exc}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {cfg_path}: {exc}") from None

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, Mapping):
        raise ConfigError(f"config file {cfg_path} must contain a mapping at the top level")

    merged = _merge(DEFAULTS, loaded, "")
    file_strategy = loaded.get("strategy")
    if (
        isinstance(file_strategy, Mapping)
        and "params" not in file_strategy
        and merged["strategy"]["name"] != DEFAULTS["strategy"]["name"]
    ):
        # The default params belong to the default strategy; another strategy starts from its own defaults.
        merged["strategy"]["params"] = {}

    # abspath (not resolve): absolute and normalised, but links/app-redirected folders are kept as given.
    if base_dir is not None:
        resolved_base = Path(os.path.abspath(base_dir))
    else:
        resolved_base = Path(os.path.abspath(cfg_path)).parent
    return _build(merged, resolved_base)


def with_overrides(
    cfg: AppConfig,
    *,
    mode: str | None = None,
    symbol: str | None = None,
    interval: str | None = None,
    strategy_name: str | None = None,
    strategy_params: Mapping[str, Any] | None = None,
    initial_balance: float | None = None,
) -> AppConfig:
    """Re-validated copy with CLI overrides applied (``dataclasses.replace``)."""
    changes: dict[str, Any] = {}
    if mode is not None:
        new_mode = _as_mode(mode)
        if new_mode == Mode.LIVE:
            raise ConfigError("live mode can only be enabled in the config file")
        changes["mode"] = new_mode
    if symbol is not None:
        changes["symbol"] = _as_str(symbol, "symbol").upper()
    if interval is not None:
        changes["interval"] = _as_str(interval, "interval")
    if strategy_name is not None or strategy_params is not None:
        name = cfg.strategy.name if strategy_name is None else _as_str(strategy_name, "strategy.name")
        # Params of a different strategy are not carried over (they would be unknown keys there).
        base_params: dict[str, Any] = dict(cfg.strategy.params) if name == cfg.strategy.name else {}
        if strategy_params is not None:
            if not isinstance(strategy_params, Mapping):
                raise ConfigError(f"{_PARAMS_PATH} overrides must be a mapping (got {strategy_params!r})")
            base_params = base_params | dict(strategy_params)
        changes["strategy"] = dataclasses.replace(cfg.strategy, name=name, params=MappingProxyType(base_params))
    if initial_balance is not None:
        balance = _as_float(initial_balance, "initial_balance")
        changes["paper"] = dataclasses.replace(cfg.paper, initial_balance=balance)
        changes["backtest"] = dataclasses.replace(cfg.backtest, initial_balance=balance)
    new_cfg = dataclasses.replace(cfg, **changes)
    _validate(new_cfg)
    return new_cfg


_CREDENTIAL_ENV: Final[dict[Mode, tuple[str, str]]] = {
    Mode.TESTNET: (ENV_TESTNET_KEY, ENV_TESTNET_SECRET),
    Mode.LIVE: (ENV_LIVE_KEY, ENV_LIVE_SECRET),
}


def _read_dotenv(path: Path) -> dict[str, str]:
    """Values of ``path`` (never mutates os.environ; no ${VAR} interpolation)."""
    if not path.is_file():
        return {}
    values = dotenv_values(path, interpolate=False, encoding="utf-8-sig")
    return {k: v for k, v in values.items() if isinstance(k, str) and isinstance(v, str)}


def load_credentials(cfg: AppConfig, *, environ: Mapping[str, str] | None = None) -> Credentials | None:
    """API credentials for testnet/live (process environment wins over ``<base_dir>/.env``); paper -> None."""
    if cfg.mode == Mode.PAPER:
        return None
    env: Mapping[str, str] = os.environ if environ is None else environ
    key_name, secret_name = _CREDENTIAL_ENV[Mode(cfg.mode)]
    file_values = _read_dotenv(cfg.base_dir / ".env")

    def pick(name: str) -> str:
        value = env.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return file_values.get(name, "").strip()

    api_key = pick(key_name)
    api_secret = pick(secret_name)
    if not api_key or not api_secret:
        raise ConfigError(f"{Mode(cfg.mode).value} mode requires {key_name} and {secret_name} in .env")
    return Credentials(api_key=api_key, api_secret=api_secret)


def assert_live_allowed(cfg: AppConfig, *, environ: Mapping[str, str] | None = None) -> None:
    """Live needs BOTH ``mode: live`` in the config AND ``CONFIRM_LIVE_TRADING=YES`` in the process env.

    The .env file is deliberately NOT consulted.
    """
    if cfg.mode != Mode.LIVE:
        return
    env: Mapping[str, str] = os.environ if environ is None else environ
    if env.get(ENV_CONFIRM_LIVE) == "YES":
        return
    raise LiveTradingNotConfirmed(
        "실거래(live) 모드는 이중 확인이 필요합니다: 설정 파일의 mode: live 와 함께, 실행하는 PowerShell 창에서 "
        '$env:CONFIRM_LIVE_TRADING = "YES" 를 직접 설정해야 합니다 (.env 파일에서는 읽지 않습니다). / '
        "Live trading requires two opt-ins: mode: live in the config file AND the process environment variable "
        "CONFIRM_LIVE_TRADING=YES (exactly, case-sensitive; the .env file is not consulted)."
    )
