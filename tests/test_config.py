"""Tests for bot/config.py (SPEC §3, §14.2 U1)."""

from __future__ import annotations

import dataclasses
import json
import textwrap
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from bot import config
from bot.config import (
    DEFAULTS,
    MAINNET_REST_URL,
    TESTNET_REST_URL,
    AppConfig,
    Credentials,
    assert_live_allowed,
    load_config,
    load_credentials,
    with_overrides,
)
from bot.errors import ConfigError, LiveTradingNotConfirmed
from bot.models import Mode
from tests.conftest import EXAMPLE_CONFIG, REPO_ROOT


def write_config(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def load_text(tmp_path: Path, text: str) -> AppConfig:
    return load_config(write_config(tmp_path, text))


# ---------------------------------------------------------------------------------------------
# Required cases
# ---------------------------------------------------------------------------------------------


def test_example_config_loads_defaults(app_config: AppConfig, tmp_path: Path) -> None:
    cfg = app_config
    assert cfg.mode is Mode.PAPER
    assert cfg.risk.leverage == 3
    assert cfg.interval == "1h"
    assert cfg.execution.price_protect is False
    assert cfg.risk.max_position_notional == 20000
    assert isinstance(cfg.risk.max_position_notional, float)
    assert cfg.symbol == "BTCUSDT"
    assert cfg.strategy.name == "ma_cross"
    assert dict(cfg.strategy.params) == {"fast_period": 20, "slow_period": 50, "ma_type": "EMA", "allow_short": True}
    assert isinstance(cfg.strategy.params, MappingProxyType)
    assert cfg.strategy.extra_modules == ()
    assert cfg.risk.stop_loss.mode == "atr"
    assert cfg.risk.take_profit_r == 2.0
    assert cfg.execution.bot_id == "mab1"
    assert cfg.execution.fees.taker == 0.0005
    assert cfg.backtest.start == "2024-01-01" and cfg.backtest.end is None
    assert cfg.dashboard.host == "127.0.0.1" and cfg.dashboard.port == 8000
    assert cfg.logging.level == "INFO"
    assert cfg.base_dir == tmp_path
    assert cfg.rest_base_url() == MAINNET_REST_URL


def test_defaults_match_example() -> None:
    with open(EXAMPLE_CONFIG, encoding="utf-8") as fh:
        assert yaml.safe_load(fh) == config.DEFAULTS


def test_config_yaml_is_identical_copy_of_example() -> None:
    assert (REPO_ROOT / "config.yaml").read_bytes() == EXAMPLE_CONFIG.read_bytes()


def test_unknown_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unknown config key: risk\.levrage"):
        load_text(tmp_path, "risk:\n  levrage: 5\n")
    with pytest.raises(ConfigError, match=r"unknown config key: foo"):
        load_text(tmp_path, "foo: 1\n")
    with pytest.raises(ConfigError, match=r"unknown config key: execution\.fees\.makr"):
        load_text(tmp_path, "execution:\n  fees:\n    makr: 0.001\n")


def test_unknown_key_inside_strategy_params_allowed(tmp_path: Path) -> None:
    cfg = load_text(tmp_path, "strategy:\n  params:\n    fast_period: 10\n    custom_knob: 3\n")
    assert dict(cfg.strategy.params) == {"fast_period": 10, "custom_knob": 3}


def test_leverage_above_max_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="leverage exceeds max_leverage"):
        load_text(tmp_path, "risk:\n  leverage: 11\n  max_leverage: 10\n")
    with pytest.raises(ConfigError, match=r"risk\.leverage"):
        load_text(tmp_path, "risk:\n  leverage: 0\n")


def test_max_leverage_above_hard_cap_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="max_leverage exceeds hard cap 20"):
        load_text(tmp_path, "risk:\n  max_leverage: 21\n")
    cfg = load_text(tmp_path, "risk:\n  leverage: 20\n  max_leverage: 20\n")
    assert cfg.risk.leverage == 20


def test_bool_not_accepted_as_int(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"risk\.leverage"):
        load_text(tmp_path, "risk:\n  leverage: true\n")
    with pytest.raises(ConfigError, match=r"execution\.kline_limit"):
        load_text(tmp_path, "execution:\n  kline_limit: false\n")
    # float fields reject bool too
    with pytest.raises(ConfigError, match=r"risk\.risk_per_trade_pct"):
        load_text(tmp_path, "risk:\n  risk_per_trade_pct: true\n")
    # bool fields accept only bool
    with pytest.raises(ConfigError, match=r"execution\.price_protect"):
        load_text(tmp_path, "execution:\n  price_protect: 1\n")


def test_int_fields_accept_integral_floats_only(tmp_path: Path) -> None:
    cfg = load_text(tmp_path, "risk:\n  leverage: 3.0\n")
    assert cfg.risk.leverage == 3 and isinstance(cfg.risk.leverage, int)
    with pytest.raises(ConfigError, match=r"risk\.leverage"):
        load_text(tmp_path, "risk:\n  leverage: 2.5\n")


@pytest.mark.parametrize("interval", ["1w", "1s", "3d", "1M", "2m", "1H"])
def test_bad_interval_rejected(tmp_path: Path, interval: str) -> None:
    with pytest.raises(ConfigError, match="interval"):
        load_text(tmp_path, f"interval: {interval}\n")


@pytest.mark.parametrize("symbol", ["BTCUSD", "BTC-USDT", "USDT", "BTCUSDC"])
def test_bad_symbol_rejected(tmp_path: Path, symbol: str) -> None:
    with pytest.raises(ConfigError, match="symbol"):
        load_text(tmp_path, f"symbol: {symbol}\n")


def test_mode_and_symbol_normalized(tmp_path: Path) -> None:
    cfg = load_text(tmp_path, "mode: PAPER\nsymbol: ' ethusdt '\nlogging:\n  level: debug\n")
    assert cfg.mode is Mode.PAPER
    assert cfg.symbol == "ETHUSDT"
    assert cfg.logging.level == "DEBUG"
    with pytest.raises(ConfigError, match="mode"):
        load_text(tmp_path, "mode: demo\n")


def test_dashboard_non_loopback_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="dashboard must bind to localhost only"):
        load_text(tmp_path, "dashboard:\n  host: 0.0.0.0\n")
    for host in ("localhost", "'::1'"):
        assert load_text(tmp_path, f"dashboard:\n  host: {host}\n").dashboard.host in config.LOOPBACK_HOSTS


def test_live_requires_env_confirmation(tmp_path: Path) -> None:
    cfg = load_text(tmp_path, "mode: live\n")
    assert cfg.mode is Mode.LIVE
    with pytest.raises(LiveTradingNotConfirmed):
        assert_live_allowed(cfg, environ={})
    with pytest.raises(LiveTradingNotConfirmed):
        assert_live_allowed(cfg, environ={"CONFIRM_LIVE_TRADING": "yes"})
    with pytest.raises(LiveTradingNotConfirmed):
        assert_live_allowed(cfg, environ={"CONFIRM_LIVE_TRADING": "NO"})
    with pytest.raises(LiveTradingNotConfirmed):
        assert_live_allowed(cfg, environ={"CONFIRM_LIVE_TRADING": " YES"})
    assert_live_allowed(cfg, environ={"CONFIRM_LIVE_TRADING": "YES"})
    # default environ = os.environ (isolated by the autouse fixture)
    with pytest.raises(LiveTradingNotConfirmed):
        assert_live_allowed(cfg)
    # LiveTradingNotConfirmed is a ConfigError; message explains both opt-ins in Korean and English
    with pytest.raises(ConfigError) as excinfo:
        assert_live_allowed(cfg, environ={})
    assert "CONFIRM_LIVE_TRADING" in str(excinfo.value) and "실거래" in str(excinfo.value)


def test_live_gate_is_noop_for_paper_and_testnet(app_config: AppConfig, tmp_path: Path) -> None:
    assert_live_allowed(app_config, environ={})
    assert_live_allowed(load_text(tmp_path, "mode: testnet\n"), environ={})


def test_confirm_live_not_read_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("CONFIRM_LIVE_TRADING=YES\n", encoding="utf-8")
    cfg = load_text(tmp_path, "mode: live\n")
    with pytest.raises(LiveTradingNotConfirmed):
        assert_live_allowed(cfg, environ={})
    with pytest.raises(LiveTradingNotConfirmed):
        assert_live_allowed(cfg)
    # the .env file never leaks into the process environment
    import os

    assert "CONFIRM_LIVE_TRADING" not in os.environ


def test_credentials_paper_returns_none(app_config: AppConfig) -> None:
    env = {"BINANCE_API_KEY": "k" * 64, "BINANCE_API_SECRET": "s" * 64}
    assert load_credentials(app_config, environ=env) is None
    assert load_credentials(app_config) is None


def test_credentials_testnet_missing_raises(tmp_path: Path) -> None:
    cfg = load_text(tmp_path, "mode: testnet\n")
    with pytest.raises(ConfigError, match="BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET"):
        load_credentials(cfg, environ={})
    with pytest.raises(ConfigError, match="testnet mode requires"):
        load_credentials(cfg, environ={"BINANCE_TESTNET_API_KEY": "abc", "BINANCE_TESTNET_API_SECRET": "  "})
    # live keys do not satisfy testnet
    with pytest.raises(ConfigError):
        load_credentials(cfg, environ={"BINANCE_API_KEY": "abc", "BINANCE_API_SECRET": "def"})
    assert cfg.rest_base_url() == TESTNET_REST_URL


def test_credentials_from_dotenv_and_env_precedence(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "BINANCE_TESTNET_API_KEY=file_key_123\nBINANCE_TESTNET_API_SECRET=file_secret_$HOME_456\n",
        encoding="utf-8",
    )
    cfg = load_text(tmp_path, "mode: testnet\n")
    creds = load_credentials(cfg, environ={})
    assert creds == Credentials(api_key="file_key_123", api_secret="file_secret_$HOME_456")
    creds = load_credentials(cfg, environ={"BINANCE_TESTNET_API_KEY": "env_key_789"})
    assert creds is not None
    assert creds.api_key == "env_key_789"  # process environment wins
    assert creds.api_secret == "file_secret_$HOME_456"  # no interpolation of $VARS

    live_cfg = load_text(tmp_path, "mode: live\n")
    with pytest.raises(ConfigError, match="live mode requires BINANCE_API_KEY and BINANCE_API_SECRET"):
        load_credentials(live_cfg, environ={})
    creds = load_credentials(live_cfg, environ={"BINANCE_API_KEY": "lk_123456", "BINANCE_API_SECRET": "ls_123456"})
    assert creds == Credentials(api_key="lk_123456", api_secret="ls_123456")


def test_credentials_repr_hides_secret() -> None:
    creds = Credentials(api_key="my-api-key-value", api_secret="my-api-secret-value")
    for text in (repr(creds), str(creds), f"{creds}", repr([creds])):
        assert "my-api-key-value" not in text
        assert "my-api-secret-value" not in text
    assert repr(creds) == "Credentials(api_key=***, api_secret=***)"
    from bot.models import to_jsonable

    assert to_jsonable(creds) == {"api_key": "***", "api_secret": "***"}


def test_with_overrides_rejects_live(app_config: AppConfig) -> None:
    with pytest.raises(ConfigError, match="live mode can only be enabled in the config file"):
        with_overrides(app_config, mode="live")
    with pytest.raises(ConfigError, match="live mode can only be enabled in the config file"):
        with_overrides(app_config, mode="LIVE")
    cfg = with_overrides(app_config, mode="testnet")
    assert cfg.mode is Mode.TESTNET
    assert app_config.mode is Mode.PAPER  # original untouched


def test_with_overrides_merges_params(app_config: AppConfig) -> None:
    cfg = with_overrides(app_config, strategy_params={"fast_period": 10, "ma_type": "SMA"})
    assert dict(cfg.strategy.params) == {"fast_period": 10, "slow_period": 50, "ma_type": "SMA", "allow_short": True}
    assert isinstance(cfg.strategy.params, MappingProxyType)
    assert dict(app_config.strategy.params)["fast_period"] == 20
    # a different strategy does not inherit the other strategy's params
    other = with_overrides(app_config, strategy_name="my_rsi", strategy_params={"period": 14})
    assert other.strategy.name == "my_rsi"
    assert dict(other.strategy.params) == {"period": 14}
    same = with_overrides(app_config, strategy_name="ma_cross", strategy_params={"slow_period": 60})
    assert dict(same.strategy.params)["fast_period"] == 20 and dict(same.strategy.params)["slow_period"] == 60


def test_with_overrides_revalidates(app_config: AppConfig) -> None:
    cfg = with_overrides(app_config, symbol="ethusdt", interval="4h", initial_balance=5000)
    assert cfg.symbol == "ETHUSDT"
    assert cfg.interval == "4h"
    assert cfg.backtest.initial_balance == 5000.0
    assert cfg.paper.initial_balance == 5000.0
    with pytest.raises(ConfigError, match="interval"):
        with_overrides(app_config, interval="1w")
    with pytest.raises(ConfigError, match="symbol"):
        with_overrides(app_config, symbol="BTCUSD")
    with pytest.raises(ConfigError, match="initial_balance"):
        with_overrides(app_config, initial_balance=0)
    with pytest.raises(ConfigError, match="initial_balance"):
        with_overrides(app_config, initial_balance=True)  # type: ignore[arg-type]


def test_paths_resolved_relative_to_base_dir(tmp_path: Path, app_config: AppConfig) -> None:
    assert app_config.base_dir == tmp_path
    assert app_config.db_path == tmp_path / "data" / "bot.db"
    assert app_config.cache_dir == tmp_path / "data"
    assert app_config.halt_path == tmp_path / "data" / "STOP"
    assert app_config.resolve_path("data/backtests") == tmp_path / "data" / "backtests"
    absolute = (tmp_path / "elsewhere" / "x.db").absolute()
    assert app_config.resolve_path(absolute) == absolute
    # default base_dir = directory of the config file
    sub = tmp_path / "proj"
    sub.mkdir()
    cfg = load_config(write_config(sub, "storage:\n  db_path: db/my.db\n"))
    assert cfg.base_dir == sub
    assert cfg.db_path == sub / "db" / "my.db"


def test_to_dict_has_no_secrets(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "BINANCE_TESTNET_API_KEY=secret_key_abcdef\nBINANCE_TESTNET_API_SECRET=secret_value_ghijkl\n",
        encoding="utf-8",
    )
    cfg = load_text(tmp_path, "mode: testnet\n")
    assert load_credentials(cfg, environ={}) is not None
    text = json.dumps(cfg.to_dict())
    assert "secret_key_abcdef" not in text
    assert "secret_value_ghijkl" not in text
    lowered = {k.lower() for k in cfg.to_dict()}
    assert not any("key" in k or "secret" in k for k in lowered)


def test_to_dict_is_json_serializable(app_config: AppConfig) -> None:
    d = app_config.to_dict()
    text = json.dumps(d, allow_nan=False)
    back = json.loads(text)
    assert back["mode"] == "paper"
    assert back["strategy"]["params"] == {"fast_period": 20, "slow_period": 50, "ma_type": "EMA", "allow_short": True}
    assert isinstance(d["strategy"]["params"], dict)
    assert d["strategy"]["extra_modules"] == []
    assert d["base_dir"] == str(app_config.base_dir)
    assert d["risk"]["stop_loss"]["mode"] == "atr"
    # the whole config keeps every section
    assert set(d) == {f.name for f in dataclasses.fields(AppConfig)}


# ---------------------------------------------------------------------------------------------
# Extra cases
# ---------------------------------------------------------------------------------------------


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found: .*copy config.example.yaml to config.yaml"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_and_non_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_text(tmp_path, "risk: [unclosed\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_text(tmp_path, "- a\n- b\n")
    with pytest.raises(ConfigError, match="risk must be a mapping"):
        load_text(tmp_path, "risk: 5\n")


def test_empty_file_and_bom_load_defaults(tmp_path: Path) -> None:
    cfg = load_text(tmp_path, "")
    assert cfg.mode is Mode.PAPER and cfg.risk.leverage == 3
    path = tmp_path / "bom.yaml"
    path.write_bytes("﻿mode: testnet\n".encode("utf-8"))
    assert load_config(path).mode is Mode.TESTNET


def test_defaults_not_mutated_by_loading(tmp_path: Path) -> None:
    snapshot = json.dumps(DEFAULTS, sort_keys=True)
    load_text(tmp_path, "strategy:\n  extra_modules: ['x.y']\n  params:\n    fast_period: 5\n")
    assert json.dumps(DEFAULTS, sort_keys=True) == snapshot


def test_other_strategy_without_params_starts_empty(tmp_path: Path) -> None:
    cfg = load_text(tmp_path, "strategy:\n  name: my_rsi\n  extra_modules: [user_strategies.my_rsi]\n")
    assert cfg.strategy.name == "my_rsi"
    assert dict(cfg.strategy.params) == {}
    assert cfg.strategy.extra_modules == ("user_strategies.my_rsi",)
    with pytest.raises(ConfigError, match="extra_modules"):
        load_text(tmp_path, "strategy:\n  extra_modules: user_strategies.my_rsi\n")


@pytest.mark.parametrize(
    ("snippet", "key"),
    [
        ("risk:\n  risk_per_trade_pct: 0\n", "risk.risk_per_trade_pct"),
        ("risk:\n  risk_per_trade_pct: 5.5\n", "risk.risk_per_trade_pct"),
        ("risk:\n  stop_loss:\n    mode: trailing\n", "risk.stop_loss.mode"),
        ("risk:\n  stop_loss:\n    percent: 50\n", "risk.stop_loss.percent"),
        ("risk:\n  stop_loss:\n    atr_period: 0\n", "risk.stop_loss.atr_period"),
        ("risk:\n  stop_loss:\n    atr_multiple: 0\n", "risk.stop_loss.atr_multiple"),
        ("risk:\n  take_profit_r: 0\n", "risk.take_profit_r"),
        ("risk:\n  max_position_notional: 0\n", "risk.max_position_notional"),
        ("risk:\n  max_position_notional: .inf\n", "risk.max_position_notional"),
        ("risk:\n  max_margin_fraction: 1.1\n", "risk.max_margin_fraction"),
        ("risk:\n  max_daily_loss_pct: 100\n", "risk.max_daily_loss_pct"),
        ("risk:\n  cooldown_bars_after_stop: -1\n", "risk.cooldown_bars_after_stop"),
        ("risk:\n  min_liq_distance_multiple: 0.5\n", "risk.min_liq_distance_multiple"),
        ("risk:\n  maint_margin_rate: 0.5\n", "risk.maint_margin_rate"),
        ("risk:\n  liq_mmr_buffer: -0.1\n", "risk.liq_mmr_buffer"),
        ("execution:\n  fees:\n    maker: 0.02\n", "execution.fees.maker"),
        ("execution:\n  fees:\n    taker: -0.001\n", "execution.fees.taker"),
        ("execution:\n  slippage_bps: 501\n", "execution.slippage_bps"),
        ("execution:\n  working_type: LAST_PRICE\n", "execution.working_type"),
        ("execution:\n  protective_mode: both\n", "execution.protective_mode"),
        ("execution:\n  candle_close_delay_sec: 61\n", "execution.candle_close_delay_sec"),
        ("execution:\n  kline_limit: 49\n", "execution.kline_limit"),
        ("execution:\n  kline_limit: 1501\n", "execution.kline_limit"),
        ("execution:\n  recv_window_ms: 60001\n", "execution.recv_window_ms"),
        ("execution:\n  heartbeat_sec: 4\n", "execution.heartbeat_sec"),
        ("execution:\n  bot_id: toolongid9\n", "execution.bot_id"),
        ("execution:\n  bot_id: 'ab-1'\n", "execution.bot_id"),
        ("execution:\n  bot_id: 1234\n", "execution.bot_id"),
        ("paper:\n  initial_balance: 0\n", "paper.initial_balance"),
        ("backtest:\n  initial_balance: -1\n", "backtest.initial_balance"),
        ("backtest:\n  start: yesterday\n", "backtest.start"),
        ("backtest:\n  end: '2023-12-31'\n", "backtest.end"),
        ("backtest:\n  end: not-a-date\n", "backtest.end"),
        ("dashboard:\n  port: 0\n", "dashboard.port"),
        ("dashboard:\n  refresh_sec: 1\n", "dashboard.refresh_sec"),
        ("logging:\n  level: VERBOSE\n", "logging.level"),
        ("logging:\n  max_bytes: 9999\n", "logging.max_bytes"),
        ("logging:\n  backup_count: 51\n", "logging.backup_count"),
        ("strategy:\n  name: ''\n", "strategy.name"),
        ("strategy:\n  params: [1, 2]\n", "strategy.params"),
    ],
)
def test_validation_rules(tmp_path: Path, snippet: str, key: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_text(tmp_path, snippet)
    assert key in str(excinfo.value)


def test_valid_edge_values_accepted(tmp_path: Path) -> None:
    cfg = load_text(
        tmp_path,
        """
        risk:
          take_profit_r: null
          max_daily_loss_pct: 0
          cooldown_bars_after_stop: 0
          stop_loss:
            mode: percent
        execution:
          slippage_bps: 0
          candle_close_delay_sec: 0.5
          protective_mode: reduce_only
          working_type: CONTRACT_PRICE
          bot_id: ABCdef12
        backtest:
          start: 2023-06-01
          end: "2024-01-01T12:00:00+09:00"
        logging:
          backup_count: 0
        """,
    )
    assert cfg.risk.take_profit_r is None
    assert cfg.risk.stop_loss.mode == "percent"
    assert cfg.execution.candle_close_delay_sec == 0.5
    assert cfg.backtest.start == "2023-06-01"  # unquoted YAML date accepted as ISO text
    assert cfg.execution.bot_id == "ABCdef12"
