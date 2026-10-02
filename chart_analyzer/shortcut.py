"""윈도우 바탕화면에 '차트 분석기' 바로가기를 만듭니다."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .settings import PROJECT_ROOT

SHORTCUT_NAME = "차트 분석기"

# 경로·이름은 환경변수로 넘겨서 공백이나 한글이 있어도 따옴표 문제가 없게 합니다.
_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$desktop = [Environment]::GetFolderPath('Desktop'); "
    "$lnk = Join-Path $desktop ($env:CA_NAME + '.lnk'); "
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk); "
    "$s.TargetPath = $env:CA_TARGET; "
    "$s.Arguments = '-m chart_analyzer'; "
    "$s.WorkingDirectory = $env:CA_WORKDIR; "
    "$s.Description = $env:CA_DESC; "
    "$s.Save()"
)


def shortcut_target() -> Path:
    """콘솔 창 없이 뜨는 pythonw.exe 가 있으면 그것을 씁니다."""
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    return pythonw if pythonw.exists() else executable


def create_desktop_shortcut(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
    if sys.platform != "win32":
        raise RuntimeError("바탕화면 바로가기는 윈도우에서만 만들 수 있습니다.")
    env = {
        **os.environ,
        "CA_NAME": SHORTCUT_NAME,
        "CA_TARGET": str(shortcut_target()),
        "CA_WORKDIR": str(PROJECT_ROOT),
        "CA_DESC": "차트 캡처 분석기",
    }
    result = run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "알 수 없는 오류").strip())
