"""Exception hierarchy of the bot (SPEC §4.2).

This module is the bottom of the import graph: it imports nothing from ``bot`` at runtime.
``OpenOutcome`` / ``PositionClosure`` are referenced for type annotations only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot.models import OpenOutcome, PositionClosure


def _restore_error(cls: type[BaseException], args: tuple[Any, ...], state: dict[str, Any]) -> BaseException:
    """Unpickle helper for exceptions whose ``__init__`` takes keyword-only arguments."""
    err = cls.__new__(cls)
    BaseException.__init__(err, *args)
    err.__dict__.update(state)
    return err


class BotError(Exception):
    """Base class of every error raised by the bot."""


class ConfigError(BotError):
    """Invalid or missing configuration (CLI exit code 2)."""


class LiveTradingNotConfirmed(ConfigError):
    """Live mode requested without the CONFIRM_LIVE_TRADING=YES process environment opt-in (exit code 3)."""


class DataError(BotError):
    """Invalid, missing or unreadable data (candles, cache files, database)."""


class StaleDataError(DataError):
    """Market data is older than expected."""


# ---------------------------------------------------------------------------------------------
# Exchange errors. NEVER put query strings, API keys or signatures into any attribute.
# ---------------------------------------------------------------------------------------------


class ExchangeError(BotError):
    """An error reported by (or while talking to) the exchange."""

    def __init__(
        self,
        msg: str,
        *,
        code: int | None = None,
        http_status: int | None = None,
        path: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(msg)
        self.msg = msg
        self.code = code
        self.http_status = http_status
        self.path = path
        self.retry_after = retry_after

    def __str__(self) -> str:
        return f"[{self.http_status} {self.code}] {self.path}: {self.msg}"

    def __reduce__(self) -> tuple[Any, ...]:
        return (_restore_error, (type(self), self.args, dict(self.__dict__)))


class TransientError(ExchangeError):
    """Temporary failure.

    GET/DELETE/PUT: safe to retry. POST: only the 503 "Service Unavailable" / -1008 cases are known
    "not executed" (``not_executed=True``); every other POST TransientError is resolved by lookup, never
    by a blind resend (§9.4).
    """

    def __init__(
        self,
        msg: str,
        *,
        code: int | None = None,
        http_status: int | None = None,
        path: str | None = None,
        retry_after: float | None = None,
        not_executed: bool = False,
    ) -> None:
        super().__init__(msg, code=code, http_status=http_status, path=path, retry_after=retry_after)
        self.not_executed = bool(not_executed)


class RateLimitError(ExchangeError):
    """HTTP 429, -1003, -1015."""


class IpBannedError(RateLimitError):
    """HTTP 418 (IP auto-banned)."""


class TimestampError(ExchangeError):
    """-1021, -5028 (timestamp outside recvWindow)."""


class AuthError(ExchangeError):
    """-1022, -2014, -2015, or no credentials configured."""


class UnknownOrderStatusError(ExchangeError):
    """POST only: the outcome is unknown (timeout, 503 "Unknown error", non-JSON 5xx, -1007, -1000)."""


class NoChangeNeededError(ExchangeError):
    """-4046, -4059, -4171: the requested setting is already in place (treat as success)."""


class OrderRejectedError(ExchangeError):
    """Generic order-level 4xx rejection."""


class InsufficientMarginError(OrderRejectedError):
    """-2018, -2019."""


class ImmediateTriggerError(OrderRejectedError):
    """-2021, -4142: the trigger price would fire immediately."""


class ReduceOnlyRejectedError(OrderRejectedError):
    """-2022, -4118."""


class MinNotionalError(OrderRejectedError):
    """-4164."""


class DuplicateClientIdError(OrderRejectedError):
    """-4116."""


class AlgoLimitError(OrderRejectedError):
    """-4045: too many open conditional (algo) orders."""


class ReduceOnlyModeError(OrderRejectedError):
    """-4400, -4401."""


class NoSuchOrderError(ExchangeError):
    """-2013, -2011: the order does not exist (e.g. cancel of an unknown order)."""


# ---------------------------------------------------------------------------------------------
# Protection / emergency errors
# ---------------------------------------------------------------------------------------------


class ProtectionFailedError(BotError):
    """The mandatory stop-loss could not be placed; the position was flattened (``flattened``)."""

    def __init__(
        self,
        msg: str,
        *,
        flattened: bool,
        closure: PositionClosure | None = None,
        entry: OpenOutcome | None = None,
    ) -> None:
        super().__init__(msg)
        self.msg = msg
        self.flattened = flattened
        self.closure = closure
        self.entry = entry

    def __reduce__(self) -> tuple[Any, ...]:
        return (_restore_error, (type(self), self.args, dict(self.__dict__)))


class EmergencyError(BotError):
    """A position may be unprotected and could not be closed. ``entry`` is set when raised from open_position."""

    def __init__(self, msg: str, *, entry: OpenOutcome | None = None) -> None:
        super().__init__(msg)
        self.msg = msg
        self.entry = entry

    def __reduce__(self) -> tuple[Any, ...]:
        return (_restore_error, (type(self), self.args, dict(self.__dict__)))
