"""Tests for bot/fsutil.py (SPEC §4.4, §14.2 U1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bot import fsutil
from bot.errors import DataError
from bot.fsutil import atomic_replace, atomic_write_text, tmp_path_for
from tests.conftest import FakeClock


def test_atomic_write_text_utf8(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "result.json"
    text = '{"label": "총 수익률", "x": 1}\nline2\r\nline3'
    atomic_write_text(target, text)
    assert target.read_bytes() == text.encode("utf-8")  # newline="" keeps line endings untranslated
    assert not tmp_path_for(target).exists()
    # overwrite
    atomic_write_text(target, "킬스위치")
    assert target.read_text(encoding="utf-8") == "킬스위치"
    assert sorted(p.name for p in target.parent.iterdir()) == ["result.json"]
    assert tmp_path_for(target) == Path(f"{target}.tmp")


def test_atomic_replace_retries_then_data_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixed_clock
) -> None:
    clock: FakeClock = fixed_clock()
    tmp = tmp_path / "equity.csv.tmp"
    target = tmp_path / "equity.csv"
    tmp.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    calls: list[tuple[Path, Path]] = []

    def locked_replace(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        calls.append((Path(src), Path(dst)))
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(os, "replace", locked_replace)
    with pytest.raises(DataError) as excinfo:
        atomic_replace(tmp, target, sleep=clock.sleep)
    assert clock.sleeps == [0.2] * 5
    assert len(calls) == 6  # first attempt + 5 retries
    message = str(excinfo.value)
    assert str(target) in message
    assert "file is locked by another program" in message
    assert "엑셀" in message
    assert isinstance(excinfo.value.__cause__, PermissionError)
    assert not tmp.exists()  # tmp deleted
    assert target.read_text(encoding="utf-8") == "old"  # target untouched


def test_atomic_replace_succeeds_after_transient_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixed_clock) -> None:
    clock: FakeClock = fixed_clock()
    tmp = tmp_path / "a.tmp"
    target = tmp_path / "a.txt"
    tmp.write_text("fresh", encoding="utf-8")
    real_replace = os.replace
    failures = {"left": 2}

    def flaky_replace(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise PermissionError(13, "locked")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    atomic_replace(tmp, target, retries=5, delay_sec=0.5, sleep=clock.sleep)
    assert clock.sleeps == [0.5, 0.5]
    assert target.read_text(encoding="utf-8") == "fresh"
    assert not tmp.exists()


def test_atomic_replace_other_errors_propagate(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        atomic_replace(tmp_path / "missing.tmp", tmp_path / "x.txt", sleep=lambda s: None)
    assert fsutil.atomic_replace is atomic_replace
