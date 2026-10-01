"""Exchange precision helpers and symbol filter parsing (SPEC §6.2).

Prices and quantities sent to Binance are ``decimal.Decimal`` values rounded here and rendered with
``format_decimal``. Indicators and simulations keep using ``float``.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Literal

import numpy as np

from bot.errors import DataError
from bot.models import SymbolFilters

logger = logging.getLogger(__name__)

# Defaults when a filter/field is absent from exchangeInfo (§6.2).
DEFAULT_MULTIPLIER_UP: Final = "1.05"
DEFAULT_MULTIPLIER_DOWN: Final = "0.95"
DEFAULT_TRIGGER_PROTECT: Final = "0.05"
DEFAULT_MARKET_TAKE_BOUND: Final = "0.05"

_ROUNDING: Final[dict[str, str]] = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_UP}


def to_decimal(x: float | int | str | Decimal) -> Decimal:
    """Convert to ``Decimal`` without binary-float artefacts.

    Decimal passes through; float -> ``Decimal(repr(x))`` (so 0.1 -> Decimal("0.1")); int and numeric str are
    exact. NaN/inf are rejected with ``ValueError``; bool and other types with ``TypeError``.
    """
    if isinstance(x, np.generic):  # numpy boundary (§0.2): np.int64 / np.float64 / np.bool_
        x = x.item()
    if isinstance(x, Decimal):
        d = x
    elif isinstance(x, bool):
        raise TypeError("bool is not a valid numeric value")
    elif isinstance(x, int):
        d = Decimal(x)
    elif isinstance(x, float):
        d = Decimal(repr(x))
    elif isinstance(x, str):
        try:
            d = Decimal(x.strip())
        except InvalidOperation:
            raise ValueError(f"not a number: {x!r}") from None
    else:
        raise TypeError(f"cannot convert {type(x).__name__} to Decimal")
    if not d.is_finite():
        raise ValueError(f"not a finite number: {x!r}")
    return d


def _positive_step(step: Any, what: str) -> Decimal:
    s = to_decimal(step)
    if s <= 0:
        raise ValueError(f"{what} must be > 0, got {s}")
    return s


def _to_step(value: Any, step: Any, rounding: str) -> Decimal:
    v = to_decimal(value)
    if v < 0:
        raise ValueError(f"value must be >= 0, got {v}")
    s = _positive_step(step, "step")
    n = (v / s).to_integral_value(rounding=rounding)
    return (n * s).quantize(s)


def floor_to_step(value: float | int | str | Decimal, step: Decimal) -> Decimal:
    """Largest multiple of ``step`` that is <= ``value`` (quantized to the step's exponent). Negative -> ValueError."""
    return _to_step(value, step, ROUND_FLOOR)


def ceil_to_step(value: float | int | str | Decimal, step: Decimal) -> Decimal:
    """Smallest multiple of ``step`` that is >= ``value`` (quantized to the step's exponent). Negative -> ValueError."""
    return _to_step(value, step, ROUND_CEILING)


def round_price(
    value: float | int | str | Decimal,
    tick: Decimal,
    mode: Literal["down", "up", "nearest"] = "nearest",
) -> Decimal:
    """Round a price to a multiple of ``tick``: "down" (floor), "up" (ceiling) or "nearest" (ROUND_HALF_UP)."""
    try:
        rounding = _ROUNDING[mode]
    except KeyError:
        raise ValueError(f"invalid rounding mode {mode!r}; expected down, up or nearest") from None
    v = to_decimal(value)
    t = _positive_step(tick, "tick")
    n = (v / t).to_integral_value(rounding=rounding)
    return (n * t).quantize(t)


def round_protective_price(price: float | int | str | Decimal, tick: Decimal, *, entry: float) -> Decimal:
    """Round a stop/take-profit trigger TOWARD the entry price (the tighter side).

    price < entry -> "up"; price > entry -> "down"; equal -> "nearest".
    """
    p = to_decimal(price)
    e = to_decimal(entry)
    if p < e:
        return round_price(p, tick, "up")
    if p > e:
        return round_price(p, tick, "down")
    return round_price(p, tick, "nearest")


def normalize_market_qty(qty: float | int | str | Decimal, filters: SymbolFilters) -> Decimal:
    """Clamp to MARKET_LOT_SIZE maxQty, floor to its stepSize; ``Decimal("0")`` if below its minQty."""
    q = to_decimal(qty)
    if q < 0:
        raise ValueError(f"quantity must be >= 0, got {q}")
    q = min(q, filters.market_max_qty)
    result = floor_to_step(q, filters.market_step_size)
    if result < filters.market_min_qty or result <= 0:
        return Decimal("0")
    return result


def meets_min_notional(qty: Decimal, price: float | int | str | Decimal, filters: SymbolFilters) -> bool:
    """``qty * price >= MIN_NOTIONAL.notional``."""
    return to_decimal(qty) * to_decimal(price) >= filters.min_notional


def format_decimal(d: Decimal) -> str:
    """Plain notation, no exponent, trailing zeros and a trailing "." stripped.

    0.001 -> "0.001", 84000.10 -> "84000.1", 100 -> "100", 1.000 -> "1", Decimal("1E+2") -> "100".
    (No ``normalize()``: it would round values with more digits than the context precision.)
    """
    value = to_decimal(d)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-", "-0"):
        return "0"
    return text


# ---------------------------------------------------------------------------------------------
# exchangeInfo parsing
# ---------------------------------------------------------------------------------------------


def _field(flt: Mapping[str, Any], key: str, where: str, symbol: str) -> Any:
    if key not in flt or flt[key] in (None, ""):
        raise DataError(f"exchangeInfo {symbol}: {where} lacks {key}")
    return flt[key]


def parse_symbol_filters(symbol_info: dict) -> SymbolFilters:
    """Build ``SymbolFilters`` from one entry of ``exchangeInfo["symbols"]``.

    PRICE_FILTER and LOT_SIZE are mandatory (``DataError``); MARKET_LOT_SIZE falls back to LOT_SIZE;
    MIN_NOTIONAL uses key ``notional`` (fallback ``minNotional``, default "0"); PERCENT_PRICE defaults
    1.05/0.95; ``triggerProtect``/``marketTakeBound`` default 0.05.
    """
    if not isinstance(symbol_info, Mapping):
        raise DataError(f"exchangeInfo symbol entry must be an object, got {type(symbol_info).__name__}")
    symbol = str(symbol_info.get("symbol") or "")
    if not symbol:
        raise DataError("exchangeInfo symbol entry lacks 'symbol'")
    raw_filters = symbol_info.get("filters")
    if not isinstance(raw_filters, list):
        raise DataError(f"exchangeInfo {symbol}: 'filters' missing or not a list")
    f: dict[str, Mapping[str, Any]] = {
        str(x["filterType"]): x for x in raw_filters if isinstance(x, Mapping) and "filterType" in x
    }
    price_filter = f.get("PRICE_FILTER")
    lot = f.get("LOT_SIZE")
    if price_filter is None or lot is None:
        missing = [name for name, v in (("PRICE_FILTER", price_filter), ("LOT_SIZE", lot)) if v is None]
        raise DataError(f"exchangeInfo {symbol}: missing filter(s) {', '.join(missing)}")

    market_lot = f.get("MARKET_LOT_SIZE") or lot

    def market_value(key: str) -> Any:
        value = market_lot.get(key)
        return value if value not in (None, "") else _field(lot, key, "LOT_SIZE", symbol)

    min_notional_filter = f.get("MIN_NOTIONAL") or {}
    notional = min_notional_filter.get("notional", min_notional_filter.get("minNotional", "0"))
    percent_price = f.get("PERCENT_PRICE") or {}

    try:
        filters = SymbolFilters(
            symbol=symbol,
            status=str(symbol_info.get("status") or ""),
            contract_type=str(symbol_info.get("contractType") or ""),
            tick_size=_field(price_filter, "tickSize", "PRICE_FILTER", symbol),
            min_price=_field(price_filter, "minPrice", "PRICE_FILTER", symbol),
            max_price=_field(price_filter, "maxPrice", "PRICE_FILTER", symbol),
            step_size=_field(lot, "stepSize", "LOT_SIZE", symbol),
            min_qty=_field(lot, "minQty", "LOT_SIZE", symbol),
            max_qty=_field(lot, "maxQty", "LOT_SIZE", symbol),
            market_step_size=market_value("stepSize"),
            market_min_qty=market_value("minQty"),
            market_max_qty=market_value("maxQty"),
            min_notional=notional if notional not in (None, "") else "0",
            multiplier_up=percent_price.get("multiplierUp") or DEFAULT_MULTIPLIER_UP,
            multiplier_down=percent_price.get("multiplierDown") or DEFAULT_MULTIPLIER_DOWN,
            trigger_protect=symbol_info.get("triggerProtect") or DEFAULT_TRIGGER_PROTECT,
            market_take_bound=symbol_info.get("marketTakeBound") or DEFAULT_MARKET_TAKE_BOUND,
        )
    except (TypeError, ValueError) as exc:
        raise DataError(f"exchangeInfo {symbol}: invalid filter value ({exc})") from None

    if filters.tick_size <= 0 or filters.step_size <= 0:
        raise DataError(f"exchangeInfo {symbol}: tickSize/stepSize must be > 0")
    if filters.market_step_size <= 0:
        # SPEC-GAP: a zero MARKET_LOT_SIZE step would make quantity rounding impossible; use the LOT_SIZE step.
        logger.warning("%s: MARKET_LOT_SIZE stepSize %s is not positive; using LOT_SIZE stepSize %s",
                       symbol, filters.market_step_size, filters.step_size)
        filters = dataclasses.replace(filters, market_step_size=filters.step_size)
    return filters
