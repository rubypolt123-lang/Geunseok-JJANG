"""Tests for bot.exchange.filters (SPEC §6.2, §14.2 U2)."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from bot.errors import DataError
from bot.exchange.filters import (
    ceil_to_step,
    floor_to_step,
    format_decimal,
    meets_min_notional,
    normalize_market_qty,
    parse_symbol_filters,
    round_price,
    round_protective_price,
    to_decimal,
)
from bot.models import SymbolFilters


def _symbol_entry(exchange_info: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(exchange_info["symbols"][0])


def _replace_filter(entry: dict[str, Any], filter_type: str, new: dict[str, Any] | None) -> dict[str, Any]:
    filters = [f for f in entry["filters"] if f["filterType"] != filter_type]
    if new is not None:
        filters.append({"filterType": filter_type, **new})
    entry["filters"] = filters
    return entry


# ---------------------------------------------------------------------------------------------
# parse_symbol_filters
# ---------------------------------------------------------------------------------------------


def test_parse_symbol_filters_from_exchange_info(exchange_info_btc: dict[str, Any], btc_filters: SymbolFilters) -> None:
    parsed = parse_symbol_filters(_symbol_entry(exchange_info_btc))
    assert parsed == btc_filters
    assert parsed.symbol == "BTCUSDT"
    assert parsed.status == "TRADING"
    assert parsed.contract_type == "PERPETUAL"
    assert parsed.tick_size == Decimal("0.1")
    assert str(parsed.tick_size) == "0.10"  # textual form kept (to_dict / cache files)
    assert parsed.step_size == Decimal("0.001")
    assert parsed.max_qty == Decimal("1000")
    assert parsed.market_max_qty == Decimal("120")  # MARKET_LOT_SIZE, not LOT_SIZE
    assert parsed.min_notional == Decimal("50")
    assert parsed.multiplier_up == Decimal("1.05")
    assert parsed.multiplier_down == Decimal("0.95")
    assert parsed.trigger_protect == Decimal("0.05")
    assert parsed.market_take_bound == Decimal("0.05")
    for name in ("tick_size", "min_price", "max_price", "step_size", "min_qty", "max_qty", "market_step_size",
                 "market_min_qty", "market_max_qty", "min_notional", "multiplier_up", "multiplier_down",
                 "trigger_protect", "market_take_bound"):
        assert isinstance(getattr(parsed, name), Decimal), name
    # The cache round trip used by the downloader.
    assert SymbolFilters.from_dict(parsed.to_dict()) == parsed


def test_parse_min_notional_key_is_notional(exchange_info_btc: dict[str, Any]) -> None:
    entry = _replace_filter(_symbol_entry(exchange_info_btc), "MIN_NOTIONAL", {"notional": "100"})
    assert parse_symbol_filters(entry).min_notional == Decimal("100")

    # "notional" wins over a legacy "minNotional" key
    entry = _replace_filter(_symbol_entry(exchange_info_btc), "MIN_NOTIONAL", {"notional": "20", "minNotional": "5"})
    assert parse_symbol_filters(entry).min_notional == Decimal("20")

    # fallback to "minNotional", default "0" when the filter is absent
    entry = _replace_filter(_symbol_entry(exchange_info_btc), "MIN_NOTIONAL", {"minNotional": "5"})
    assert parse_symbol_filters(entry).min_notional == Decimal("5")
    entry = _replace_filter(_symbol_entry(exchange_info_btc), "MIN_NOTIONAL", None)
    assert parse_symbol_filters(entry).min_notional == Decimal("0")


def test_parse_market_lot_size_falls_back_to_lot_size(exchange_info_btc: dict[str, Any]) -> None:
    entry = _replace_filter(_symbol_entry(exchange_info_btc), "MARKET_LOT_SIZE", None)
    parsed = parse_symbol_filters(entry)
    assert parsed.market_step_size == Decimal("0.001")
    assert parsed.market_min_qty == Decimal("0.001")
    assert parsed.market_max_qty == Decimal("1000")


def test_parse_defaults_for_optional_fields(exchange_info_btc: dict[str, Any]) -> None:
    entry = _replace_filter(_symbol_entry(exchange_info_btc), "PERCENT_PRICE", None)
    del entry["triggerProtect"]
    del entry["marketTakeBound"]
    parsed = parse_symbol_filters(entry)
    assert parsed.multiplier_up == Decimal("1.05")
    assert parsed.multiplier_down == Decimal("0.95")
    assert parsed.trigger_protect == Decimal("0.05")
    assert parsed.market_take_bound == Decimal("0.05")


@pytest.mark.parametrize("missing", ["PRICE_FILTER", "LOT_SIZE"])
def test_parse_missing_mandatory_filter_raises(exchange_info_btc: dict[str, Any], missing: str) -> None:
    entry = _replace_filter(_symbol_entry(exchange_info_btc), missing, None)
    with pytest.raises(DataError, match=missing):
        parse_symbol_filters(entry)


def test_parse_invalid_filter_value_raises(exchange_info_btc: dict[str, Any]) -> None:
    entry = _replace_filter(
        _symbol_entry(exchange_info_btc), "PRICE_FILTER", {"tickSize": "abc", "minPrice": "1", "maxPrice": "2"}
    )
    with pytest.raises(DataError):
        parse_symbol_filters(entry)


# ---------------------------------------------------------------------------------------------
# Rounding helpers
# ---------------------------------------------------------------------------------------------


def test_to_decimal_conversions() -> None:
    assert to_decimal(0.1) == Decimal("0.1")  # no binary artefacts
    assert to_decimal(Decimal("1.230")) == Decimal("1.230")
    assert to_decimal(3) == Decimal(3)
    assert to_decimal(" 84000.10 ") == Decimal("84000.10")
    assert to_decimal(np.float64(0.25)) == Decimal("0.25")
    assert to_decimal(np.int64(7)) == Decimal(7)
    for bad in (float("nan"), float("inf"), float("-inf"), "nan", Decimal("NaN")):
        with pytest.raises(ValueError):
            to_decimal(bad)
    with pytest.raises(ValueError):
        to_decimal("not a number")
    with pytest.raises(TypeError):
        to_decimal(True)


def test_floor_to_step_edge_cases() -> None:
    step = Decimal("0.001")
    assert floor_to_step(0.0019999, step) == Decimal("0.001")
    assert floor_to_step(0.1 + 0.2, Decimal("0.1")) == Decimal("0.3")  # 0.30000000000000004
    assert format_decimal(floor_to_step(1.0, step)) == "1"
    assert floor_to_step(1.0, step) == Decimal("1")
    assert floor_to_step(0.0009, step) == Decimal("0")
    assert floor_to_step(0.12345, Decimal("0.0001")) == Decimal("0.1234")  # demo step
    # quantized to the step's exponent
    assert floor_to_step(Decimal("0.0123456"), step).as_tuple().exponent == -3
    assert floor_to_step(5, Decimal("1")) == Decimal("5")
    assert floor_to_step(0, step) == Decimal("0")
    # ceil counterpart
    assert ceil_to_step(0.0011, step) == Decimal("0.002")
    assert ceil_to_step(0.002, step) == Decimal("0.002")


def test_floor_negative_raises() -> None:
    with pytest.raises(ValueError):
        floor_to_step(-0.001, Decimal("0.001"))
    with pytest.raises(ValueError):
        ceil_to_step(-1, Decimal("0.001"))
    with pytest.raises(ValueError):
        floor_to_step(1, Decimal("0"))  # step must be positive


def test_round_price_modes() -> None:
    tick = Decimal("0.10")
    assert round_price(84000.15, tick, "down") == Decimal("84000.1")
    assert round_price(84000.15, tick, "up") == Decimal("84000.2")
    assert round_price(84000.15, tick, "nearest") == Decimal("84000.2")  # ROUND_HALF_UP
    assert round_price(84000.14, tick) == Decimal("84000.1")
    assert round_price(84000.10, tick, "up") == Decimal("84000.1")  # already on a tick
    assert round_price(84000.10, tick, "down") == Decimal("84000.1")
    assert round_price(84000.15, tick, "down").as_tuple().exponent == -2  # tick exponent
    assert format_decimal(round_price(84000.15, tick, "down")) == "84000.1"
    with pytest.raises(ValueError):
        round_price(1.0, tick, "sideways")  # type: ignore[arg-type]


def test_round_protective_toward_entry() -> None:
    tick = Decimal("0.1")
    entry = 50_000.0
    assert round_protective_price(49000.07, tick, entry=entry) == Decimal("49000.1")  # long SL -> up
    assert round_protective_price(51000.07, tick, entry=entry) == Decimal("51000.0")  # short SL -> down
    assert round_protective_price(52000.07, tick, entry=entry) == Decimal("52000.0")  # long TP -> down
    assert round_protective_price(48000.03, tick, entry=entry) == Decimal("48000.1")  # short TP -> up
    assert round_protective_price(50000.04, tick, entry=50000.04) == Decimal("50000.0")  # equal -> nearest


def test_normalize_market_qty_clamps_to_market_max(btc_filters: SymbolFilters) -> None:
    assert normalize_market_qty(200, btc_filters) == Decimal("120")
    assert normalize_market_qty(Decimal("0.0567"), btc_filters) == Decimal("0.056")
    assert normalize_market_qty(0.1 + 0.2, btc_filters) == Decimal("0.3")


def test_normalize_below_min_returns_zero(btc_filters: SymbolFilters) -> None:
    assert normalize_market_qty(0.0009, btc_filters) == Decimal("0")
    assert normalize_market_qty(0, btc_filters) == Decimal("0")
    assert normalize_market_qty(0.001, btc_filters) == Decimal("0.001")


def test_meets_min_notional(btc_filters: SymbolFilters) -> None:
    assert meets_min_notional(Decimal("0.001"), 50_000, btc_filters)  # exactly 50 -> ok
    assert not meets_min_notional(Decimal("0.001"), 49_999.9, btc_filters)
    assert meets_min_notional(Decimal("0.002"), 30_000.0, btc_filters)
    assert not meets_min_notional(Decimal("0"), 100_000.0, btc_filters)


def test_format_decimal_no_exponent() -> None:
    assert format_decimal(Decimal("1E+2")) == "100"
    assert format_decimal(Decimal("0.00010")) == "0.0001"
    assert format_decimal(Decimal("0.001")) == "0.001"
    assert format_decimal(Decimal("84000.10")) == "84000.1"
    assert format_decimal(Decimal("100")) == "100"
    assert format_decimal(Decimal("1.000")) == "1"
    assert format_decimal(Decimal("1E-7")) == "0.0000001"
    assert format_decimal(Decimal("0")) == "0"
    assert format_decimal(Decimal("0.000")) == "0"
    assert format_decimal(Decimal("-0.0")) == "0"
    assert format_decimal(Decimal("-1.50")) == "-1.5"
    assert format_decimal(Decimal("123456789012345678901234567890.123")) == "123456789012345678901234567890.123"
    for d in (Decimal("1E+2"), Decimal("0.00010"), Decimal("84000.10")):
        assert "E" not in format_decimal(d) and "e" not in format_decimal(d)
