"""Risk presets and the comment-preserving config.yaml editor (bot/profiles.py)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bot import profiles
from bot.config import AppConfig, load_config, with_overrides
from bot.errors import ConfigError
from tests.conftest import EXAMPLE_CONFIG


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG, path)
    return path


def test_presets_are_valid_and_ordered_by_risk(app_config: AppConfig) -> None:
    assert profiles.PROFILE_KEYS == ("conservative", "standard", "aggressive", "very_aggressive")
    risks = [p.risk["risk_per_trade_pct"] for p in profiles.PROFILES]
    assert risks == sorted(risks)
    for profile in profiles.PROFILES:
        cfg = profiles.apply_profile(app_config, profile)
        for name, value in profile.risk.items():
            assert getattr(cfg.risk, name) == value
        assert profiles.matching_profile(cfg.risk) is profile
    # the example config IS the standard preset
    assert profiles.matching_profile(app_config.risk) is profiles.get_profile("standard")
    with pytest.raises(ConfigError, match="unknown risk profile"):
        profiles.get_profile("yolo")


def test_apply_profile_raises_max_leverage_when_needed(app_config: AppConfig) -> None:
    import dataclasses

    low_cap = dataclasses.replace(app_config, risk=dataclasses.replace(app_config.risk, leverage=2, max_leverage=3))
    cfg = profiles.apply_profile(low_cap, profiles.get_profile("very_aggressive"))
    assert (cfg.risk.leverage, cfg.risk.max_leverage) == (10, 10)
    assert profiles.apply_profile(low_cap, profiles.get_profile("conservative")).risk.max_leverage == 3


def test_with_overrides_risk_validation(app_config: AppConfig) -> None:
    with pytest.raises(ConfigError, match="risk.levrage"):
        with_overrides(app_config, risk_overrides={"levrage": 5})
    with pytest.raises(ConfigError):
        with_overrides(app_config, risk_overrides={"risk_per_trade_pct": 6.0})  # > 5 % is never allowed
    with pytest.raises(ConfigError):
        with_overrides(app_config, risk_overrides={"leverage": 25})


def test_update_config_text_keeps_comments_and_alignment() -> None:
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    out = profiles.update_config_text(
        text, {("mode",): "testnet", ("risk", "leverage"): 10, ("risk", "take_profit_r"): None}
    )
    changed = [(a, b) for a, b in zip(text.splitlines(), out.splitlines()) if a != b]
    assert changed == [
        ("mode: paper", "mode: testnet"),
        (
            "  leverage: 3                     # 레버리지 (max_leverage 이하)",
            "  leverage: 10                    # 레버리지 (max_leverage 이하)",
        ),
        (
            "  take_profit_r: 2.0              # 익절가 = 손절거리 x R (null 이면 익절 주문 없음)",
            "  take_profit_r: null             # 익절가 = 손절거리 x R (null 이면 익절 주문 없음)",
        ),
    ]
    assert len(out.splitlines()) == len(text.splitlines())


def test_update_config_text_only_touches_the_named_section() -> None:
    text = "a:\n  leverage: 1\nrisk:\n  # comment\n  leverage: 3\n  stop_loss:\n    mode: atr\nleverage: 7\n"
    out = profiles.update_config_text(text, {("risk", "leverage"): 5, ("leverage",): 9})
    assert out == "a:\n  leverage: 1\nrisk:\n  # comment\n  leverage: 5\n  stop_loss:\n    mode: atr\nleverage: 9\n"
    with pytest.raises(ConfigError, match="risk.nothing"):
        profiles.update_config_text(text, {("risk", "nothing"): 1})
    with pytest.raises(ConfigError, match="missing"):
        profiles.update_config_text(text, {("missing", "x"): 1})
    with pytest.raises(ConfigError, match="단일 값"):
        profiles.update_config_text(text, {("risk", "stop_loss"): 1})  # a block, not a scalar
    with pytest.raises(ValueError):
        profiles.update_config_text(text, {("risk", "leverage"): "two words"})


def test_update_config_file_validates_before_writing(config_file: Path) -> None:
    before = config_file.read_bytes()
    with pytest.raises(ConfigError):
        profiles.update_config_file(config_file, {("risk", "risk_per_trade_pct"): 9.0})  # > 5: invalid
    with pytest.raises(ConfigError):
        profiles.update_config_file(config_file, {("interval",): "7m"})
    assert config_file.read_bytes() == before
    assert sorted(p.name for p in config_file.parent.iterdir()) == ["config.yaml"]  # no temp files left

    cfg = profiles.update_config_file(
        config_file, profiles.profile_updates(profiles.get_profile("aggressive"), load_config(config_file).risk)
    )
    assert cfg.base_dir == config_file.parent
    reloaded = load_config(config_file)
    assert profiles.matching_profile(reloaded.risk) is profiles.get_profile("aggressive")
    assert (reloaded.risk.leverage, reloaded.risk.risk_per_trade_pct) == (5, 2.0)


def test_update_config_file_keeps_crlf_and_bom(config_file: Path) -> None:
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    config_file.write_bytes("\ufeff".encode() + text.replace("\n", "\r\n").encode("utf-8"))  # Notepad-style file
    profiles.update_config_file(config_file, {("interval",): "4h"})
    data = config_file.read_bytes()
    assert data.count(b"\r\n") == text.count("\n") and data.replace(b"\r\n", b"").count(b"\n") == 0
    assert load_config(config_file).interval == "4h"
