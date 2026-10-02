from __future__ import annotations

import os

from chart_analyzer import settings
from chart_analyzer.shortcut import create_desktop_shortcut


def test_save_api_key_adds_line_and_keeps_others(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=abc\n# 메모\n", encoding="utf-8")

    settings.save_api_key("  sk-ant-api03-new  ", env)

    assert env.read_text(encoding="utf-8") == "BINANCE_API_KEY=abc\n# 메모\nANTHROPIC_API_KEY=sk-ant-api03-new\n"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-api03-new"
    assert settings.has_api_key()


def test_save_api_key_replaces_existing_and_duplicates(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=\nX=1\nexport ANTHROPIC_API_KEY=old\n", encoding="utf-8")

    settings.save_api_key("sk-ant-api03-new", env)

    assert env.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=sk-ant-api03-new\nX=1\n"


def test_save_api_key_creates_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    settings.save_api_key("sk-ant-api03-new", env)
    assert env.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=sk-ant-api03-new\n"


def test_load_env_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")
    settings.load_env(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "from-shell"


def test_has_api_key_ignores_blank(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert not settings.has_api_key()


def test_looks_like_api_key():
    assert settings.looks_like_api_key("sk-ant-api03-abcdefghijklmnop")
    assert not settings.looks_like_api_key("sk-ant-")
    assert not settings.looks_like_api_key("sk-ant-api03 abcdefghijklmnop")
    assert not settings.looks_like_api_key("hello world")


def test_shortcut_passes_paths_through_environment(monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stdout = stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Done()

    monkeypatch.setattr("chart_analyzer.shortcut.sys.platform", "win32")
    create_desktop_shortcut(run=fake_run)

    cmd, kwargs = calls[0]
    assert cmd[0] == "powershell" and "CreateShortcut" in cmd[-1]
    assert kwargs["env"]["CA_NAME"] == "차트 분석기"
    assert kwargs["env"]["CA_WORKDIR"] == str(settings.PROJECT_ROOT)
