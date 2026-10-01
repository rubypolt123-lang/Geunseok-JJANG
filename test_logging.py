"""Tests for bot/logging_setup.py (SPEC §12.2, §14.2 U1)."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

import pytest

from bot import logging_setup
from bot.config import AppConfig, LoggingConfig
from bot.logging_setup import (
    RedactingFilter,
    RedactingFormatter,
    add_secrets,
    clear_secrets,
    redact,
    setup_logging,
    shutdown_logging,
)

SECRET = "supersecretvalue1234567890"
API_KEY = "vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A"


def log_file(cfg: AppConfig, name: str = "test") -> Path:
    return cfg.base_dir / cfg.logging.dir / f"{name}.log"


def read_log(cfg: AppConfig, name: str = "test") -> str:
    for handler in logging.getLogger("bot").handlers:
        handler.flush()
    return log_file(cfg, name).read_text(encoding="utf-8")


def test_secret_redacted_in_message_and_args(app_config: AppConfig) -> None:
    setup_logging(app_config.logging, base_dir=app_config.base_dir, log_name="test", secrets=[SECRET, "short"], console=False)
    log = logging.getLogger("bot.test")
    log.info("inline secret %s", SECRET)
    log.info("in message " + SECRET)
    log.warning("dict %r", {"api_secret": SECRET})
    log.info("short values are not registered: short")
    text = read_log(app_config)
    assert SECRET not in text
    assert text.count("***") >= 3
    assert "short values are not registered: short" in text  # len < 6 is never redacted


def test_signature_redacted(app_config: AppConfig) -> None:
    setup_logging(app_config.logging, base_dir=app_config.base_dir, log_name="test", console=False)
    log = logging.getLogger("bot.rest")
    sig = "3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9"
    log.info("GET https://fapi.binance.com/fapi/v1/order?symbol=BTCUSDT&timestamp=1&signature=%s", sig)
    log.info("headers %s", {"X-MBX-APIKEY": API_KEY})
    log.info("X-MBX-APIKEY: %s", API_KEY)
    text = read_log(app_config)
    assert sig not in text
    assert "signature=***" in text
    assert API_KEY not in text
    assert "'X-MBX-APIKEY': '***'" in text
    assert "X-MBX-APIKEY: ***" in text


def test_traceback_redacted(app_config: AppConfig) -> None:
    setup_logging(app_config.logging, base_dir=app_config.base_dir, log_name="test", secrets=[SECRET], console=False)
    log = logging.getLogger("bot.trader")
    try:
        raise RuntimeError(f"request failed: /fapi/v1/order?signature=abc123 key={SECRET}")
    except RuntimeError:
        log.exception("iteration failed")
    log.error("with stack", stack_info=True, extra={"k": SECRET})
    text = read_log(app_config)
    assert "Traceback (most recent call last)" in text
    assert "RuntimeError" in text
    assert "abc123" not in text
    assert SECRET not in text
    assert "signature=***" in text


def test_file_handler_utf8_korean(app_config: AppConfig) -> None:
    setup_logging(app_config.logging, base_dir=app_config.base_dir, log_name="trade", console=False)
    logging.getLogger("bot.risk").warning("킬스위치 발동: 일일 손실 %.2f%%", 5.0)
    text = read_log(app_config, "trade")
    assert "킬스위치 발동: 일일 손실 5.00%" in text
    raw = log_file(app_config, "trade").read_bytes()
    assert "킬스위치".encode("utf-8") in raw
    line = text.strip().splitlines()[-1]
    # UTC ISO timestamp, padded level, logger name
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z WARNING  bot\.risk: ", line), line


def test_setup_logging_idempotent(app_config: AppConfig) -> None:
    first = setup_logging(app_config.logging, base_dir=app_config.base_dir, log_name="test")
    old_handlers = list(first.handlers)
    assert len(old_handlers) == 2
    logging.getLogger("bot.x").info("first")
    second = setup_logging(app_config.logging, base_dir=app_config.base_dir, log_name="test")
    assert second is logging.getLogger("bot")
    assert len(second.handlers) == 2
    assert not set(map(id, old_handlers)) & set(map(id, second.handlers))
    old_file = next(h for h in old_handlers if isinstance(h, logging.handlers.RotatingFileHandler))
    assert old_file.stream is None  # closed
    assert second.propagate is False
    assert second.level == logging.INFO
    for handler in second.handlers:
        assert isinstance(handler.formatter, RedactingFormatter)
        assert any(isinstance(f, RedactingFilter) for f in handler.filters)
    logging.getLogger("bot.x").info("second")
    text = read_log(app_config)
    assert text.count("first") == 1 and text.count("second") == 1  # no duplicate lines
    only_file = setup_logging(app_config.logging, base_dir=app_config.base_dir, log_name="test", console=False)
    assert len(only_file.handlers) == 1


def test_shutdown_logging_closes_handlers(app_config: AppConfig) -> None:
    setup_logging(app_config.logging, base_dir=app_config.base_dir, log_name="dashboard", console=False)
    bot_logger = logging.getLogger("bot")
    file_handler = bot_logger.handlers[0]
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        assert lg.handlers == [file_handler] and lg.propagate is False
    logging.getLogger("uvicorn.error").info("Started server process")
    bot_logger.info("dashboard ready")
    shutdown_logging()
    for name in ("bot", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        assert lg.handlers == []
        assert lg.propagate is True
    assert isinstance(file_handler, logging.handlers.RotatingFileHandler)
    assert file_handler.stream is None
    path = log_file(app_config, "dashboard")
    text = path.read_text(encoding="utf-8")
    assert "Started server process" in text and "dashboard ready" in text
    path.unlink()  # file is not locked any more (Windows)
    shutdown_logging()  # idempotent


def test_non_dashboard_does_not_touch_uvicorn(app_config: AppConfig) -> None:
    setup_logging(app_config.logging, base_dir=app_config.base_dir, log_name="trade", console=False)
    assert logging.getLogger("uvicorn").handlers == []
    assert logging.getLogger("uvicorn.access").handlers == []


def test_console_handler_writes_stderr(app_config: AppConfig, capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(app_config.logging, base_dir=app_config.base_dir, log_name="test", secrets=[SECRET])
    logging.getLogger("bot.cli").info("console line %s 한글", SECRET)
    logging.getLogger("bot.cli").debug("hidden debug line")
    err = capsys.readouterr().err
    assert "console line *** 한글" in err
    assert SECRET not in err
    assert "hidden debug line" not in err


def test_level_from_config(tmp_path: Path) -> None:
    cfg = LoggingConfig(level="warning", dir="logs", max_bytes=10_000, backup_count=1)
    logger = setup_logging(cfg, base_dir=tmp_path, log_name="lvl", console=False)
    assert logger.level == logging.WARNING
    logging.getLogger("bot.a").info("not written")
    logging.getLogger("bot.a").warning("written")
    for h in logger.handlers:
        h.flush()
    text = (tmp_path / "logs" / "lvl.log").read_text(encoding="utf-8")
    assert "written" in text and "not written" not in text
    with pytest.raises(ValueError):
        setup_logging(cfg, base_dir=tmp_path, log_name="../evil", console=False)


def test_redact_function_and_registry() -> None:
    assert redact("nothing to hide") == "nothing to hide"
    assert redact("a&signature=DEADbeef01&b=1") == "a&signature=***&b=1"
    add_secrets(["abcdef", "abc", "", "  padded-secret  "])
    assert redact("x abcdef y abc") == "x *** y abc"
    assert redact("padded-secret") == "***"
    # longest secret first so overlapping values are fully masked
    add_secrets(["abcdefghij"])
    assert redact("abcdefghij") == "***"
    clear_secrets()
    assert redact("abcdef") == "abcdef"
    assert redact(12345) == "12345"  # type: ignore[arg-type]
    assert logging_setup.MIN_SECRET_LEN == 6


def test_filter_survives_bad_format_args(app_config: AppConfig) -> None:
    setup_logging(app_config.logging, base_dir=app_config.base_dir, log_name="test", console=False)
    logging.getLogger("bot.bad").info("two args %s %s", "only-one")
    text = read_log(app_config)
    assert "unformattable args" in text
