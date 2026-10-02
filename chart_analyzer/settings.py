"""프로젝트 경로와 API 키(.env) 관리."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
REPORTS_DIR = PROJECT_ROOT / "chart_reports"

API_KEY_NAME = "ANTHROPIC_API_KEY"
API_KEY_PAGE = "https://platform.claude.com"


def load_env(path: Path = ENV_PATH) -> None:
    """.env 의 값을 환경변수로 읽어 옵니다. 이미 설정된 환경변수는 덮어쓰지 않습니다."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path)


def has_api_key() -> bool:
    return bool(os.environ.get(API_KEY_NAME, "").strip() or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip())


def looks_like_api_key(key: str) -> bool:
    key = key.strip()
    return key.startswith("sk-ant-") and len(key) > 20 and not any(ch.isspace() for ch in key)


def save_api_key(key: str, path: Path = ENV_PATH) -> None:
    """.env 의 ANTHROPIC_API_KEY 줄을 바꾸거나 추가합니다. 다른 줄(바이낸스 키 등)은 그대로 둡니다."""
    key = key.strip()
    old_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new_line = f"{API_KEY_NAME}={key}"
    lines: list[str] = []
    replaced = False
    for line in old_lines:
        name = line.split("=", 1)[0].strip().removeprefix("export ").strip()
        if name == API_KEY_NAME:
            if not replaced:
                lines.append(new_line)
                replaced = True
            continue  # 같은 키가 여러 줄이면 하나만 남깁니다
        lines.append(line)
    if not replaced:
        lines.append(new_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[API_KEY_NAME] = key
