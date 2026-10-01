"""Atomic file helpers (SPEC §4.4).

Never call a bare ``os.replace`` for cache/result files: on Windows it raises ``PermissionError`` while
another program (e.g. Excel) holds the target open. ``atomic_replace`` retries and then raises a
``DataError`` with a Korean hint.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from bot.errors import DataError

logger = logging.getLogger(__name__)


def tmp_path_for(path: str | Path) -> Path:
    """The temporary sibling used for atomic writes: ``<path>.tmp``."""
    return Path(f"{path}.tmp")


def atomic_replace(
    tmp: Path,
    target: Path,
    *,
    retries: int = 5,
    delay_sec: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """``os.replace(tmp, target)``; on ``PermissionError`` retry up to ``retries`` times, sleeping ``delay_sec``.

    Still failing -> delete ``tmp`` (errors ignored) and raise ``DataError``.
    """
    tmp_p = Path(tmp)
    target_p = Path(target)
    attempt = 0
    while True:
        try:
            os.replace(tmp_p, target_p)
            return
        except PermissionError as exc:
            if attempt >= retries:
                last_exc = exc
                break
            attempt += 1
            logger.debug("replace of %s failed (attempt %d/%d); retrying", target_p, attempt, retries)
            sleep(delay_sec)
    try:
        tmp_p.unlink(missing_ok=True)
    except OSError:
        pass
    raise DataError(
        f"file is locked by another program: {target_p} "
        "(엑셀 등에서 파일을 열어두었다면 닫고 다시 실행하세요)"
    ) from last_exc


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Create parent dirs, write ``<path>.tmp`` (``newline=""``) and atomically replace ``path``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(target)
    try:
        with open(tmp, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    atomic_replace(tmp, target)
