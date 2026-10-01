"""Strategy base, registry and MA crossover (SPEC §8.2-§8.4, §14.2 U3)."""

from __future__ import annotations

import math
import sys
import textwrap
import uuid
from types import MappingProxyType
from typing import Any

import numpy as np
import pandas as pd
import pytest

from bot.errors import ConfigError, DataError
from bot.models import Signal, SignalAction
from bot.strategy import (
    MACrossStrategy,
    Strategy,
    available_strategies,
    create_strategy,
    get_strategy_class,
    load_strategy_modules,
    register,
)
from bot.strategy import registry
from bot.strategy.indicators import ema, sma

# SMA(2) vs SMA(4): d = ma_fast - ma_slow is -1,-1,-1,-1,-1,-0.5,+0.75,... -> golden cross exactly at bar 9.
GOLDEN_CLOSES = [20, 19, 18, 17, 16, 15, 14, 13, 14, 16, 18, 20, 22]
# Mirror image: d = +1,...,+0.5,-0.75,... -> dead cross exactly at bar 9.
DEAD_CLOSES = [10, 11, 12, 13, 14, 15, 16, 17, 16, 14, 12, 10, 8]
CROSS_BAR = 9
SMALL_SMA = {"fast_period": 2, "slow_period": 4, "ma_type": "SMA"}


def _synthetic_closes(n: int, seed: int) -> list[float]:
    """Random walk + slow sine: many crosses, no exact ties."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    log_path = np.cumsum(rng.normal(0.0, 0.004, n)) + 0.05 * np.sin(2 * np.pi * t / 120)
    return (30_000.0 * np.exp(log_path)).tolist()


def _all_signals(strategy: Strategy, df: pd.DataFrame) -> list[Signal]:
    prepared = strategy.prepare(df)
    return [strategy.signal_at(prepared, i) for i in range(len(prepared))]


def _make_concrete(name: Any) -> type[Strategy]:
    """A minimal concrete Strategy subclass (not registered)."""

    class _Tmp(Strategy):
        @classmethod
        def default_params(cls) -> dict[str, Any]:
            return {}

        @property
        def warmup_bars(self) -> int:
            return 1

        def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
            return df.copy()

        def signal_at(self, prepared: pd.DataFrame, i: int) -> Signal:
            return Signal(SignalAction.NONE, int(prepared["open_time"].iloc[i]), float(prepared["close"].iloc[i]))

    _Tmp.name = name
    return _Tmp


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, type[Strategy]]:
    """Registry copy for tests that register classes (restored afterwards)."""
    reg = dict(registry._REGISTRY)
    monkeypatch.setattr(registry, "_REGISTRY", reg)
    return reg


# ---------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------


def test_package_exports() -> None:
    import bot.strategy as pkg
    from bot.strategy import indicators

    for name in ("Strategy", "register", "create_strategy", "available_strategies", "load_strategy_modules"):
        assert hasattr(pkg, name), name
    assert indicators.sma is sma


def test_registry_has_ma_cross() -> None:
    assert "ma_cross" in available_strategies()
    assert available_strategies() == sorted(available_strategies())
    assert get_strategy_class("ma_cross") is MACrossStrategy
    strat = create_strategy("ma_cross")
    assert isinstance(strat, MACrossStrategy)
    assert strat.params == {"fast_period": 20, "slow_period": 50, "ma_type": "EMA", "allow_short": True}
    assert strat.describe() == "ma_cross(fast_period=20, slow_period=50, ma_type=EMA, allow_short=True)"
    # config params arrive as MappingProxyType
    s2 = create_strategy("ma_cross", MappingProxyType({"fast_period": 10, "slow_period": 30}))
    assert s2.params["fast_period"] == 10 and s2.params["slow_period"] == 30


def test_unknown_strategy_raises_config_error() -> None:
    with pytest.raises(ConfigError) as ei:
        get_strategy_class("does_not_exist")
    msg = str(ei.value)
    assert "unknown strategy 'does_not_exist'" in msg
    assert "available" in msg and "ma_cross" in msg
    with pytest.raises(ConfigError):
        create_strategy("does_not_exist", {"fast_period": 1})


def test_register_rejects_duplicate_and_empty_names(isolated_registry: dict[str, type[Strategy]]) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        register(_make_concrete("ma_cross"))
    with pytest.raises(ValueError):
        register(_make_concrete(""))
    with pytest.raises(ValueError):
        register(_make_concrete("   "))
    with pytest.raises(ValueError):
        register(_make_concrete(None))
    # Re-registering the very same class object is harmless.
    assert register(MACrossStrategy) is MACrossStrategy
    fresh = register(_make_concrete("u3_fresh"))
    assert get_strategy_class("u3_fresh") is fresh
    assert "u3_fresh" in available_strategies()


def test_register_rejects_non_strategy_and_abstract() -> None:
    with pytest.raises(TypeError):
        register(object)  # type: ignore[arg-type]

    class StillAbstract(Strategy):
        name = "u3_abstract"

    with pytest.raises(TypeError):
        register(StillAbstract)


# ---------------------------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"fast_period": 50, "slow_period": 50},  # fast == slow
        {"fast_period": 60, "slow_period": 50},  # fast > slow
        {"ma_type": "WMA"},
        {"ma_type": 1},
        {"foo": 1},  # unknown key
        {"fast_period": 0},
        {"fast_period": -3},
        {"fast_period": True},
        {"fast_period": 2.5},
        {"fast_period": "10"},
        {"slow_period": None},
        {"allow_short": "yes"},
        {"allow_short": 1},
    ],
)
def test_bad_params_rejected(params: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        MACrossStrategy(params)
    with pytest.raises(ConfigError):
        create_strategy("ma_cross", params)


def test_unknown_param_message_names_key() -> None:
    with pytest.raises(ConfigError, match="foo"):
        MACrossStrategy({"foo": 1})


def test_params_normalized() -> None:
    s = MACrossStrategy({"fast_period": 5.0, "slow_period": np.int64(12), "ma_type": " sma ", "allow_short": np.bool_(False)})
    assert s.params == {"fast_period": 5, "slow_period": 12, "ma_type": "SMA", "allow_short": False}
    assert type(s.params["fast_period"]) is int and type(s.params["slow_period"]) is int
    assert type(s.params["allow_short"]) is bool


# ---------------------------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------------------------


def test_golden_cross_long_on_cross_bar_only(candle_factory) -> None:
    df = candle_factory(GOLDEN_CLOSES)
    strat = MACrossStrategy(SMALL_SMA)
    assert strat.warmup_bars == 5
    sigs = _all_signals(strat, df)
    longs = [i for i, s in enumerate(sigs) if s.action is SignalAction.LONG]
    assert longs == [CROSS_BAR]
    assert all(s.action is SignalAction.NONE for i, s in enumerate(sigs) if i != CROSS_BAR)
    cross = sigs[CROSS_BAR]
    assert cross.reason == "golden_cross"
    assert cross.bar_open_time == int(df["open_time"].iloc[CROSS_BAR])
    assert cross.price == 16.0
    assert cross.meta == {"ma_fast": pytest.approx(15.0), "ma_slow": pytest.approx(14.25)}
    assert [s.reason for s in sigs[:4]] == ["warmup"] * 4
    assert all(s.reason == "no_cross" for i, s in enumerate(sigs) if i >= 4 and i != CROSS_BAR)
    # Live path: generate() on growing closed-candle prefixes fires only when the cross bar is the last one.
    for k in range(1, len(df) + 1):
        sig = strat.generate(df.iloc[:k])
        assert (sig.action is SignalAction.LONG) == (k - 1 == CROSS_BAR)


def test_dead_cross_short(candle_factory) -> None:
    df = candle_factory(DEAD_CLOSES)
    sigs = _all_signals(MACrossStrategy(SMALL_SMA), df)
    assert [i for i, s in enumerate(sigs) if s.action is not SignalAction.NONE] == [CROSS_BAR]
    assert sigs[CROSS_BAR].action is SignalAction.SHORT
    assert sigs[CROSS_BAR].reason == "dead_cross"
    # the golden series produces no SHORT at all
    assert all(s.action is not SignalAction.SHORT for s in _all_signals(MACrossStrategy(SMALL_SMA), candle_factory(GOLDEN_CLOSES)))


def test_dead_cross_close_when_short_disabled(candle_factory) -> None:
    strat = MACrossStrategy({**SMALL_SMA, "allow_short": False})
    sigs = _all_signals(strat, candle_factory(DEAD_CLOSES))
    assert [i for i, s in enumerate(sigs) if s.action is not SignalAction.NONE] == [CROSS_BAR]
    assert sigs[CROSS_BAR].action is SignalAction.CLOSE
    assert sigs[CROSS_BAR].reason == "dead_cross_close_long"
    assert all(s.action is not SignalAction.SHORT for s in sigs)
    # golden crosses are unaffected by allow_short
    golden = _all_signals(strat, candle_factory(GOLDEN_CLOSES))
    assert golden[CROSS_BAR].action is SignalAction.LONG


def test_warmup_returns_none(candle_factory) -> None:
    df = candle_factory(GOLDEN_CLOSES)
    # EMA(2)/EMA(4): warmup 3*4+1 = 13 bars -> every bar of this 13-bar series except the last is warmup,
    # even though the SMA version crosses at bar 9.
    strat = MACrossStrategy({"fast_period": 2, "slow_period": 4, "ma_type": "EMA"})
    assert strat.warmup_bars == 13
    sigs = _all_signals(strat, df)
    assert all(s.action is SignalAction.NONE and s.reason == "warmup" for s in sigs[:12])
    assert sigs[12].reason != "warmup"
    # default EMA(20/50): 151 bars of warmup
    default = MACrossStrategy()
    assert default.warmup_bars == 151
    sig = default.generate(candle_factory(_synthetic_closes(150, seed=1)))
    assert sig.action is SignalAction.NONE and sig.reason == "warmup"
    # a single bar can never produce a signal (needs the previous bar)
    one = MACrossStrategy({"fast_period": 1, "slow_period": 2, "ma_type": "SMA"})
    assert one.generate(df.iloc[:1]).reason == "warmup"


def test_nan_values_return_warmup(candle_factory) -> None:
    df = candle_factory(GOLDEN_CLOSES)
    df.loc[7, "close"] = math.nan  # SMA(4) is NaN for bars 7..10, SMA(2) for 7..8
    sigs = _all_signals(MACrossStrategy(SMALL_SMA), df)
    for i in range(7, 12):  # needs rows i-1 and i
        assert sigs[i].action is SignalAction.NONE and sigs[i].reason == "warmup"


def test_no_lookahead_signal_prefix_equality(candle_factory) -> None:
    df = candle_factory(_synthetic_closes(400, seed=3))
    for params in (
        {"fast_period": 5, "slow_period": 20, "ma_type": "EMA"},
        {"fast_period": 5, "slow_period": 20, "ma_type": "SMA"},
    ):
        strat = MACrossStrategy(params)
        full = strat.prepare(df)
        n_signals = 0
        for i in range(len(df)):
            a = strat.signal_at(full, i)
            b = strat.signal_at(strat.prepare(df.iloc[: i + 1]), i)
            assert a.action == b.action, (params, i)
            n_signals += a.action is not SignalAction.NONE
        assert n_signals >= 3  # the series really crosses


def test_rolling_window_matches_full_history(candle_factory) -> None:
    df = candle_factory(_synthetic_closes(3000, seed=11))
    for ma_type in ("SMA", "EMA"):
        strat = MACrossStrategy({"fast_period": 20, "slow_period": 50, "ma_type": ma_type})
        window = max(500, 2 * strat.warmup_bars)
        full = strat.prepare(df)
        crosses = 0
        for i in range(window, len(df)):
            live = strat.generate(df.iloc[i - window + 1 : i + 1].reset_index(drop=True))
            backtest = strat.signal_at(full, i)
            assert live.action == backtest.action, (ma_type, i)
            assert live.bar_open_time == backtest.bar_open_time
            crosses += backtest.action is not SignalAction.NONE
        assert crosses >= 5, ma_type


def test_signal_bar_time_is_native_int(candle_factory) -> None:
    df = candle_factory(GOLDEN_CLOSES)
    assert df["open_time"].dtype == np.int64
    strat = MACrossStrategy(SMALL_SMA)
    for sig in (strat.generate(df.iloc[: CROSS_BAR + 1]), strat.signal_at(strat.prepare(df), 0), strat.generate(df)):
        assert type(sig.bar_open_time) is int
        assert type(sig.price) is float
        assert all(type(v) is float for v in sig.meta.values())
        assert set(sig.meta) == {"ma_fast", "ma_slow"}
    # a mixed-dtype frame (float open_time) still yields an int bar time
    mixed = df.astype({"open_time": "float64"})
    assert type(strat.generate(mixed).bar_open_time) is int


def test_prepare_does_not_mutate_input(candle_factory) -> None:
    df = candle_factory(_synthetic_closes(200, seed=5))
    before = df.copy()
    cols_before = list(df.columns)
    strat = MACrossStrategy({"fast_period": 5, "slow_period": 20})
    prepared = strat.prepare(df)
    pd.testing.assert_frame_equal(df, before)
    assert list(df.columns) == cols_before
    assert prepared is not df
    assert {"ma_fast", "ma_slow"} <= set(prepared.columns)
    pd.testing.assert_frame_equal(prepared[cols_before], before)
    strat.generate(df)
    pd.testing.assert_frame_equal(df, before)


def test_prepare_requires_columns() -> None:
    with pytest.raises(DataError):
        MACrossStrategy().prepare(pd.DataFrame({"close": [1.0, 2.0]}))


def test_generate_on_empty_frame_raises(candle_factory) -> None:
    with pytest.raises(DataError):
        MACrossStrategy().generate(candle_factory([1.0]).iloc[:0])


def test_signal_at_rejects_out_of_range_index(candle_factory) -> None:
    strat = MACrossStrategy(SMALL_SMA)
    prepared = strat.prepare(candle_factory(GOLDEN_CLOSES))
    for bad in (-1, len(prepared)):
        with pytest.raises(ValueError):
            strat.signal_at(prepared, bad)


def test_sma_and_ema_modes(candle_factory) -> None:
    df = candle_factory(_synthetic_closes(120, seed=9))
    s_sma = MACrossStrategy({"fast_period": 3, "slow_period": 7, "ma_type": "sma"})
    assert s_sma.params["ma_type"] == "SMA"
    assert s_sma.warmup_bars == 8
    p = s_sma.prepare(df)
    pd.testing.assert_series_equal(p["ma_fast"], sma(df["close"], 3), check_names=False)
    pd.testing.assert_series_equal(p["ma_slow"], sma(df["close"], 7), check_names=False)

    s_ema = MACrossStrategy({"fast_period": 3, "slow_period": 7, "ma_type": "EMA"})
    assert s_ema.warmup_bars == 22
    p = s_ema.prepare(df)
    pd.testing.assert_series_equal(p["ma_fast"], ema(df["close"], 3), check_names=False)
    pd.testing.assert_series_equal(p["ma_slow"], ema(df["close"], 7), check_names=False)
    assert s_ema.describe() == "ma_cross(fast_period=3, slow_period=7, ma_type=EMA, allow_short=True)"


# ---------------------------------------------------------------------------------------------
# User strategies
# ---------------------------------------------------------------------------------------------

_USER_MODULE = '''
from __future__ import annotations

from typing import Any

import pandas as pd

from bot.errors import ConfigError
from bot.models import Signal, SignalAction
from bot.strategy import Strategy, register


@register
class MyMomentum(Strategy):
    name = "{name}"

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {{"lookback": 3}}

    def validate_params(self) -> None:
        if not isinstance(self.params["lookback"], int) or self.params["lookback"] < 1:
            raise ConfigError("lookback must be an int >= 1")

    @property
    def warmup_bars(self) -> int:
        return self.params["lookback"] + 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["mom"] = out["close"] - out["close"].shift(self.params["lookback"])
        return out

    def signal_at(self, prepared: pd.DataFrame, i: int) -> Signal:
        t = int(prepared["open_time"].iloc[i])
        c = float(prepared["close"].iloc[i])
        m = float(prepared["mom"].iloc[i])
        if i < self.warmup_bars - 1 or m != m:
            return Signal(SignalAction.NONE, t, c, "warmup")
        return Signal(SignalAction.LONG if m > 0 else SignalAction.SHORT, t, c, "momentum", {{"mom": m}})
'''


def test_custom_strategy_registration_and_load_modules(
    tmp_path, monkeypatch: pytest.MonkeyPatch, isolated_registry, candle_factory
) -> None:
    pkg = f"user_strategies_{uuid.uuid4().hex[:8]}"
    strat_name = f"my_momentum_{uuid.uuid4().hex[:6]}"
    (tmp_path / pkg).mkdir()
    (tmp_path / pkg / "my_mom.py").write_text(textwrap.dedent(_USER_MODULE.format(name=strat_name)), encoding="utf-8")
    (tmp_path / pkg / "broken.py").write_text("import u3_module_that_does_not_exist_xyz\n", encoding="utf-8")
    (tmp_path / pkg / "typo.py").write_text("def oops(:\n    pass\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))  # what the CLI does with cfg.base_dir
    module = f"{pkg}.my_mom"
    try:
        assert strat_name not in available_strategies()
        load_strategy_modules([module])
        assert strat_name in available_strategies()
        load_strategy_modules((module,))  # already imported: no duplicate-registration error

        strat = create_strategy(strat_name, {"lookback": 2})
        assert isinstance(strat, Strategy)
        assert strat.describe() == f"{strat_name}(lookback=2)"
        sig = strat.generate(candle_factory([10, 11, 12, 13]))
        assert sig.action is SignalAction.LONG and sig.reason == "momentum"
        with pytest.raises(ConfigError):
            create_strategy(strat_name, {"lookback": 0})

        with pytest.raises(ConfigError, match="u3_no_such_module_xyz"):
            load_strategy_modules(["u3_no_such_module_xyz"])
        with pytest.raises(ConfigError):
            load_strategy_modules([f"{pkg}.broken"])  # ImportError raised inside the module
        with pytest.raises(ConfigError, match="syntax error"):
            load_strategy_modules([f"{pkg}.typo"])  # a user file that does not compile
        with pytest.raises(ConfigError):
            load_strategy_modules([""])
    finally:
        for key in [k for k in sys.modules if k == pkg or k.startswith(pkg + ".")]:
            sys.modules.pop(key, None)
    load_strategy_modules([])  # no-op
