"""Logging setup with secret redaction (SPEC §12.2).

Every handler gets a ``RedactingFormatter`` (covers message, args, traceback and stack) and a
``RedactingFilter``. Registered secrets (API key/secret values, length >= 6), ``signature=<hex>`` and
``X-MBX-APIKEY`` header values are replaced with ``***``.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from bot.config import LoggingConfig

LOG_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"
MIN_SECRET_LEN: Final = 6
REDACTED: Final = "***"
BOT_LOGGER: Final = "bot"
UVICORN_LOGGERS: Final[tuple[str, ...]] = ("uvicorn", "uvicorn.error", "uvicorn.access")
MANAGED_LOGGERS: Final[tuple[str, ...]] = (BOT_LOGGER, *UVICORN_LOGGERS)

_SIGNATURE_RE: Final = re.compile(r"(signature=)[0-9a-fA-F]+", re.IGNORECASE)
_APIKEY_RE: Final = re.compile(r"(X-MBX-APIKEY['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9]+", re.IGNORECASE)
_LOG_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_secrets_lock = threading.Lock()
_secrets: set[str] = set()
_secrets_ordered: tuple[str, ...] = ()  # longest first, so overlapping secrets are fully masked


def add_secrets(secrets: Iterable[str]) -> None:
    """Register values (len >= 6) to redact from every log line, globally."""
    global _secrets_ordered
    with _secrets_lock:
        for value in secrets:
            if not isinstance(value, str):
                continue
            for candidate in {value, value.strip()}:
                if len(candidate) >= MIN_SECRET_LEN:
                    _secrets.add(candidate)
        _secrets_ordered = tuple(sorted(_secrets, key=len, reverse=True))


def clear_secrets() -> None:
    """Empty the secret registry (tests)."""
    global _secrets_ordered
    with _secrets_lock:
        _secrets.clear()
        _secrets_ordered = ()


def redact(text: str) -> str:
    """Replace registered secrets, ``signature=<hex>`` and ``X-MBX-APIKEY`` values with ``***``."""
    if not isinstance(text, str):
        text = str(text)
    for secret in _secrets_ordered:
        if secret in text:
            text = text.replace(secret, REDACTED)
    text = _SIGNATURE_RE.sub(r"\g<1>" + REDACTED, text)
    text = _APIKEY_RE.sub(r"\g<1>" + REDACTED, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter whose whole output (message, args, traceback, stack) is redacted."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))

    def formatException(self, ei: Any) -> str:  # noqa: N802 (logging API name)
        return redact(super().formatException(ei))

    def formatStack(self, stack_info: str) -> str:  # noqa: N802 (logging API name)
        return redact(super().formatStack(stack_info))


class RedactingFilter(logging.Filter):
    """Redacts the rendered message in place (``record.msg``); ``record.args`` becomes None."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # bad format args must not crash the caller
            message = f"{record.msg!s} (unformattable args: {record.args!r})"
        record.msg = redact(message)
        record.args = None
        return True


def _level_number(name: str) -> int:
    level = logging.getLevelNamesMapping().get(str(name).strip().upper())
    if level is None:
        raise ValueError(f"unknown log level {name!r}")
    return level


def _reconfigure_errors_replace(stream: Any) -> None:
    """The console must never crash on cp949 (``errors="replace"``)."""
    try:
        stream.reconfigure(errors="replace")
    except Exception:
        pass


def _detach_handlers(logger: logging.Logger, closed: set[int]) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        if id(handler) in closed:
            continue
        closed.add(id(handler))
        try:
            handler.close()
        except Exception:
            pass


def shutdown_logging() -> None:
    """Remove and close every handler on "bot" and the uvicorn loggers (idempotent).

    Also restores their default ``propagate=True`` / ``NOTSET`` level so that later code (e.g. pytest's
    caplog) sees records again.
    """
    closed: set[int] = set()
    for name in MANAGED_LOGGERS:
        logger = logging.getLogger(name)
        _detach_handlers(logger, closed)
        logger.propagate = True
        logger.setLevel(logging.NOTSET)


def setup_logging(
    cfg: LoggingConfig,
    *,
    base_dir: Path,
    log_name: str,
    secrets: Iterable[str] = (),
    console: bool = True,
) -> logging.Logger:
    """Configure the "bot" logger: UTC console + rotating UTF-8 file ``<base_dir>/<cfg.dir>/<log_name>.log``.

    Idempotent (old handlers are removed and closed). With ``log_name == "dashboard"`` the same handlers are
    attached to the uvicorn loggers (uvicorn must then run with ``log_config=None``).
    """
    if not isinstance(log_name, str) or not _LOG_NAME_RE.fullmatch(log_name):
        raise ValueError(f"invalid log_name {log_name!r}")
    level = _level_number(cfg.level)
    add_secrets(secrets)
    shutdown_logging()

    formatter = RedactingFormatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    formatter.converter = time.gmtime

    handlers: list[logging.Handler] = []
    if console:
        _reconfigure_errors_replace(sys.stderr)
        handlers.append(logging.StreamHandler(sys.stderr))
    log_dir = Path(base_dir) / cfg.dir
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers.append(
        logging.handlers.RotatingFileHandler(
            log_dir / f"{log_name}.log",
            maxBytes=cfg.max_bytes,
            backupCount=cfg.backup_count,
            encoding="utf-8",
            delay=True,
        )
    )
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(RedactingFilter())

    targets = [BOT_LOGGER, *(UVICORN_LOGGERS if log_name == "dashboard" else ())]
    for name in targets:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = False
        for handler in handlers:
            logger.addHandler(handler)
    return logging.getLogger(BOT_LOGGER)
