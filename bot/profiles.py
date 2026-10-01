"""Risk presets (안정형 / 기본형 / 공격형 / 초공격형) and a comment-preserving editor for ``config.yaml``.

# SPEC-GAP: not part of SPEC v1. Added for the beginner launcher and the ``compare`` command. A preset only changes
# a few ``risk`` values; every value still goes through the normal config validation (load_config / with_overrides).

How the presets differ: the bot sizes every position so that a stop-loss costs ``risk_per_trade_pct`` of equity,
so that number (not the leverage) sets how big wins and losses are. Leverage only decides how much margin a
position locks and how close liquidation is; the daily loss limit stops new entries for the rest of the UTC day.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml

from bot.config import AppConfig, RiskConfig, load_config, with_overrides
from bot.errors import ConfigError
from bot.fsutil import atomic_write_text, tmp_path_for


@dataclass(frozen=True, slots=True)
class RiskProfile:
    key: str
    label: str
    summary: str
    risk: Mapping[str, Any]


def _profile(key: str, label: str, summary: str, **risk: Any) -> RiskProfile:
    return RiskProfile(key, label, summary, MappingProxyType(risk))


PROFILES: Final[tuple[RiskProfile, ...]] = (
    _profile(
        "conservative",
        "안정형",
        "레버리지 2배 · 손절 1회에 자산의 0.5% · 하루 -3%면 그날 진입 중지 · 익절 2R",
        leverage=2,
        risk_per_trade_pct=0.5,
        max_daily_loss_pct=3.0,
        take_profit_r=2.0,
    ),
    _profile(
        "standard",
        "기본형",
        "레버리지 3배 · 손절 1회에 자산의 1% · 하루 -5%면 그날 진입 중지 · 익절 2R",
        leverage=3,
        risk_per_trade_pct=1.0,
        max_daily_loss_pct=5.0,
        take_profit_r=2.0,
    ),
    _profile(
        "aggressive",
        "공격형",
        "레버리지 5배 · 손절 1회에 자산의 2% · 하루 -8%면 그날 진입 중지 · 익절 3R",
        leverage=5,
        risk_per_trade_pct=2.0,
        max_daily_loss_pct=8.0,
        take_profit_r=3.0,
    ),
    _profile(
        "very_aggressive",
        "초공격형",
        "레버리지 10배 · 손절 1회에 자산의 3% · 하루 -12%면 그날 진입 중지 · 익절 없이 추세 끝까지",
        leverage=10,
        risk_per_trade_pct=3.0,
        max_daily_loss_pct=12.0,
        take_profit_r=None,
    ),
)
PROFILE_KEYS: Final[tuple[str, ...]] = tuple(p.key for p in PROFILES)


def get_profile(key: str) -> RiskProfile:
    for profile in PROFILES:
        if profile.key == key:
            return profile
    raise ConfigError(f"unknown risk profile {key!r}; choose one of: {', '.join(PROFILE_KEYS)}")


def risk_overrides(profile: RiskProfile, risk: RiskConfig) -> dict[str, Any]:
    """The preset's ``risk`` values, raising ``max_leverage`` when the preset needs more than the config allows."""
    overrides = dict(profile.risk)
    if int(overrides["leverage"]) > risk.max_leverage:
        overrides["max_leverage"] = int(overrides["leverage"])
    return overrides


def apply_profile(cfg: AppConfig, profile: RiskProfile) -> AppConfig:
    return with_overrides(cfg, risk_overrides=risk_overrides(profile, cfg.risk))


def matching_profile(risk: RiskConfig) -> RiskProfile | None:
    """The preset whose values ``risk`` has exactly (None when the config was customised)."""
    for profile in PROFILES:
        if all(getattr(risk, name) == value for name, value in profile.risk.items()):
            return profile
    return None


# ---------------------------------------------------------------------------------------------
# config.yaml editing (keeps comments, order and alignment; touches only the targeted value)
# ---------------------------------------------------------------------------------------------

_KEY_LINE_RE: Final = re.compile(r"^(?P<indent>[ ]*)(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<rest>.*)$")
_VALUE_RE: Final = re.compile(r"^(?P<lead>[ \t]*)(?P<value>[^#]*?)(?P<trail>[ \t]*(?:#.*)?)$")
_PLAIN_WORD_RE: Final = re.compile(r"^[A-Za-z0-9_.-]+$")


def format_yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str) and _PLAIN_WORD_RE.fullmatch(value):
        return value
    raise ValueError(f"unsupported config value {value!r}")


def _is_structural(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find_key(lines: list[str], path: tuple[str, ...]) -> int:
    """Index of the line holding ``path`` (top-level key or one level below a top-level section)."""
    where = ".".join(path)
    if len(path) == 1:
        for i, line in enumerate(lines):
            m = _KEY_LINE_RE.match(line)
            if m and m["indent"] == "" and m["key"] == path[0]:
                return i
        raise ConfigError(f"config.yaml 에서 '{where}' 항목을 찾지 못했습니다. 설정 파일을 직접 고쳐 주세요.")
    if len(path) != 2:
        raise ValueError(f"unsupported config path {path!r}")
    section = next(
        (i for i, line in enumerate(lines) if (m := _KEY_LINE_RE.match(line)) and m["indent"] == "" and m["key"] == path[0]),
        None,
    )
    if section is None:
        raise ConfigError(f"config.yaml 에서 '{path[0]}:' 섹션을 찾지 못했습니다. 설정 파일을 직접 고쳐 주세요.")
    child_indent: int | None = None
    for i in range(section + 1, len(lines)):
        line = lines[i]
        if not _is_structural(line):
            continue
        indent = _indent(line)
        if indent == 0:
            break  # next top-level key: end of the section
        if child_indent is None:
            child_indent = indent
        m = _KEY_LINE_RE.match(line)
        if m and indent == child_indent and m["key"] == path[1]:
            return i
    raise ConfigError(f"config.yaml 에서 '{where}' 항목을 찾지 못했습니다. 설정 파일을 직접 고쳐 주세요.")


def update_config_text(text: str, updates: Mapping[tuple[str, ...], Any]) -> str:
    """``text`` with the scalar value of each ``path`` replaced; comments, spacing and line endings are kept."""
    raw_lines = text.splitlines(keepends=True)
    bodies = [line.rstrip("\r\n") for line in raw_lines]
    endings = [line[len(body) :] for line, body in zip(raw_lines, bodies)]
    for path, value in updates.items():
        i = _find_key(bodies, tuple(path))
        m = _KEY_LINE_RE.match(bodies[i])
        assert m is not None
        v = _VALUE_RE.match(m["rest"])
        if v is None or not v["value"].strip():
            raise ConfigError(f"config.yaml 의 '{'.'.join(path)}' 는 단일 값이 아니라서 바꿀 수 없습니다.")
        new_value = format_yaml_scalar(value)
        trail = v["trail"]
        if trail.lstrip(" \t").startswith("#"):
            # keep the comment column: shrink/grow the gap by the length difference (at least one space)
            gap = len(trail) - len(trail.lstrip(" \t"))
            gap = max(1, gap + len(v["value"]) - len(new_value))
            trail = " " * gap + trail.lstrip(" \t")
        lead = v["lead"] or " "
        bodies[i] = f"{m['indent']}{m['key']}:{lead}{new_value}{trail}"
    return "".join(body + ending for body, ending in zip(bodies, endings))


def _value_at(data: Any, path: tuple[str, ...]) -> Any:
    for part in path:
        if not isinstance(data, Mapping) or part not in data:
            return _MISSING
        data = data[part]
    return data


_MISSING: Final = object()


def update_config_file(path: Path, updates: Mapping[tuple[str, ...], Any]) -> AppConfig:
    """Apply ``updates`` to the config file in place, only if the result loads and validates; returns it loaded."""
    path = Path(path)
    original = path.read_bytes().decode("utf-8-sig")  # not read_text: keep CRLF line endings as they are
    new_text = update_config_text(original, updates)
    parsed = yaml.safe_load(new_text)
    for key_path, value in updates.items():
        if _value_at(parsed, tuple(key_path)) != value:
            raise ConfigError(f"config.yaml 의 '{'.'.join(key_path)}' 값을 바꾸지 못했습니다. 설정 파일을 직접 고쳐 주세요.")
    check = tmp_path_for(path.with_name(path.name + ".check"))
    try:
        atomic_write_text(check, new_text)
        cfg = load_config(check)  # same folder: identical base_dir and relative paths
    finally:
        check.unlink(missing_ok=True)
    if new_text != original:
        atomic_write_text(path, new_text)
    return cfg


def profile_updates(profile: RiskProfile, risk: RiskConfig) -> dict[tuple[str, ...], Any]:
    return {("risk", name): value for name, value in risk_overrides(profile, risk).items()}
